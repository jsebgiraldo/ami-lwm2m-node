"""Provision the BENCH ThingsBoard so the lab node can actually REGISTER.

WHY
---
A bench without an LwM2M server is not a small production system, it is a
different one: the node never gets a server-ACKed REGISTER, so overlays/lab.conf
must disable CONFIG_AMI_BOOT_REGISTER_DEADLINE_S and stretch the HW-watchdog
grace to keep it alive - which means every registration / observe / RPC / OTA
path stays untested and the watchdog policy under test is not the one we ship.
This script gives the bench a real server with the EXACT production shape, so
those two lines can go back to their prj.conf values and the bench starts
reproducing (instead of manufacturing) fleet behaviour.

Everything that defines "the production shape" is IMPORTED, not re-authored:
  tb_edge_provision.build_profile_body()  - LWM2M profile, NoSec, OTA settings
  tb_edge_provision.provision_device()    - device + LWM2M_CREDENTIALS/NO_SEC
  tb_edge_upload_models.xml_*()           - the object-model XMLs
  tb_edge_monitoring_setup.OBSERVE_ADDITIONS - the authoritative observe +
                                               pmin/pmax superset
The only bench-specific policy is spelled out under "BENCH TUNING" below.

ORDER OF OPERATIONS (each step exists because skipping it fails SILENTLY)
  1. wait for TB           - a cold tb-postgres answers the port long before
                             it answers /api/auth/login
  2. upload object models  - without model 33000/10242 TB accepts the
                             REGISTER and then maps ZERO telemetry keys, with
                             no log line for experimental object IDs
  3. RESTART the container - Leshan loads models into LwM2mModelProvider at
                             startup only; models uploaded after a device
                             registered do NOT retroactively fix dropped
                             observes
  4. device profile        - observeAttr + pmax (pmax is the only
                             protocol-ENFORCED cadence guarantee; a path in
                             keyName/telemetry but missing from `observe` is
                             the repo's #1 recurring "frozen telemetry" bug)
  5. device + credentials  - provisionType is DISABLED, so an unknown
                             endpoint is rejected with LwM2MAuthException and
                             the node reboot-loops
  6. bench inactivity      - TB flips `active` after 600 s of silence; LwM2M
                             traffic this sparse makes the flag flap

Steps 7-8 are NOT this script's job and are printed as next actions:
publish the SRP record (the node discovers the server ONLY via DNS-SD:
_lwm2m._udp.default.service.arpa.) and power-cycle the node - a profile change
only reaches a device on REGISTER.

USAGE
  python tools/lab_tb/lab_tb_provision.py
  python tools/lab_tb/lab_tb_provision.py --dry-run
  python tools/lab_tb/lab_tb_provision.py --endpoint ami-esp32c6-3bb0 \
      --host localhost --port 8080 --uptime-pmax 60
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lab_tb_common as L  # noqa: E402

import tb_edge_provision as prov            # noqa: E402
import tb_edge_upload_models as models      # noqa: E402
import tb_edge_monitoring_setup as mon      # noqa: E402


# ── artefacts ──────────────────────────────────────────────────────────
# (title registered in TB, filename, generator or None).
# Titles/filenames match production exactly (tb_edge_upload_models.py:342-348
# + upload_33001_and_test.py:19) so a bench dump is diffable against an Edge.
ARTEFACTS = [
    ("Object 3 v1.0 - Device",                  "3.xml",     "xml_3"),
    ("Object 5 v1.0 - Firmware Update",         "5.xml",     "xml_5"),
    ("Object 10242 v1.0 - 3-Phase Power Meter", "10242.xml", "xml_10242"),
    ("Object 33000 v2.2 - Thread + LwM2M Diag", "33000.xml", "xml_33000"),
    ("Object 3303 v1.1 - IPSO Temperature",     "3303.xml",  "xml_3303"),
    # 33001 has no generator - it is a hand-written model, file only. Needed
    # for /33001/0/x Execute RPCs (become_child etc.); without it TB answers
    # "unable to find obj: 33001". Not observed, so no observeAttr entries.
    ("Object 33001 v1.0 - Thread Role Control", "33001.xml", None),
]

# RIDs 23..37 are implemented in firmware (src/lwm2m_obj_thread_diag.h:78-121)
# but absent from the shipped 33000 model, which advertises only 0..22 + 38.
# tb_edge_monitoring_setup.OBSERVE_ADDITIONS nevertheless observes 23..36, so
# on production those paths cannot be typed. --full-33000 appends them for the
# bench (ObjectVersion stays "1.0": bumping it is the trap that made TB
# silently drop the WHOLE object - see tb_edge_upload_models.py:184-193).
EXTRA_33000_ITEMS = [
    (23, "Hang Uptime",            "R", "Optional", "Integer", "Uptime at the saved hang snapshot.", "s"),
    (24, "Hang Heap Free",         "R", "Optional", "Integer", "Free heap bytes at snapshot.", "B"),
    (25, "Hang Heap Min Free",     "R", "Optional", "Integer", "Lifetime min free heap up to snapshot.", "B"),
    (26, "Hang Reg Age",           "R", "Optional", "Integer", "Seconds since last REG_UPDATE_COMPLETE at snapshot.", "s"),
    (27, "Hang LwM2M State",       "R", "Optional", "Integer", "PM_LWM2M_STATE_* bitmap at snapshot."),
    (28, "Hang Thread Role",       "R", "Optional", "Integer", "OpenThread role at snapshot."),
    (29, "Keepalive Emit",         "R", "Optional", "Integer", "CoAP keepalive emissions."),
    (30, "Keepalive Consec Fail",  "R", "Optional", "Integer", "Consecutive keepalive failures; >0 precedes an engine wedge."),
    (31, "Last Emit Uptime",       "R", "Optional", "Integer", "Uptime of the last emitted notification.", "s"),
    (32, "NoReg Boots",            "R", "Optional", "Integer", "Boots that never reached REGISTER."),
    (33, "In Recovery",            "R", "Optional", "Integer", "1 while lwm2m_recover_work is in flight."),
    (34, "Boot Burst",             "R", "Optional", "Integer", "Consecutive unstable boots (NVS-wear guard)."),
    (35, "Detached Total",         "R", "Optional", "Integer", "Cumulative Thread-DETACHED seconds.", "s"),
    (36, "Heap Min Free Live",     "R", "Optional", "Integer", "Live lifetime minimum free heap.", "B"),
    (37, "Last Reboot Code",       "R", "Optional", "Integer", "ami_reboot_get_last_code() - which code path rebooted the node."),
]
EXTRA_33000_OBSERVE = {"/33000_1.0/0/37": ("last_reboot_code", 0, 3600)}

# prj.conf:304 CONFIG_LWM2M_ENGINE_MAX_OBSERVER - client-side observe table size.
FW_MAX_OBSERVER = 36


# ── model helpers ──────────────────────────────────────────────────────
def load_xml(fname: str, generator: str | None) -> str | None:
    """models/<fname> is the version-controlled source of truth; fall back to
    the generator in tb_edge_upload_models and materialise the file."""
    path = L.MODELS_DIR / fname
    if path.exists():
        return path.read_text(encoding="utf-8")
    if generator is None:
        return None
    xml = getattr(models, generator)()
    L.MODELS_DIR.mkdir(exist_ok=True)
    path.write_text(xml, encoding="utf-8")
    print(f"  [gen ] {fname} was missing - regenerated from "
          f"tb_edge_upload_models.{generator}()")
    return xml


def extend_33000(xml: str) -> str:
    """Insert RIDs 23..37 in numeric order (before the existing Item 38)."""
    have = set(model_rids(xml))
    items = "".join(models._item(*it) for it in EXTRA_33000_ITEMS
                    if it[0] not in have)
    if not items:
        return xml
    anchor = '      <Item ID="38">'
    if anchor in xml:
        return xml.replace(anchor, items + anchor, 1)
    return xml.replace("    </Resources>", items + "    </Resources>", 1)


def model_rids(xml: str) -> list[int]:
    return sorted(int(m) for m in re.findall(r'<Item ID="(\d+)"', xml))


def resource_index(tb) -> dict:
    """title/fileName -> resource dict, one page fetch."""
    idx = {}
    try:
        data = tb.get("/api/resource", {"pageSize": 200, "page": 0}).get("data", [])
    except Exception:
        return idx
    for r in data:
        for k in (r.get("title"), r.get("fileName")):
            if k:
                idx[k] = r
    return idx


def remote_data(tb, res: dict) -> str | None:
    """Best-effort base64 of an already-uploaded model, for change detection.
    Returns None when the API does not expose it (then we re-upload)."""
    rid = (res.get("id") or {}).get("id")
    if not rid:
        return None
    try:
        full = tb.get(f"/api/resource/{rid}")
    except Exception:
        return None
    return full.get("data")


def upload_models(tb, base: str, full_33000: bool, force: bool,
                  dry: bool) -> tuple[list[str], str]:
    """Returns (changed titles, the 33000 XML actually in play)."""
    idx = resource_index(tb)
    changed: list[str] = []
    xml_33000 = ""
    for title, fname, gen in ARTEFACTS:
        xml = load_xml(fname, gen)
        if xml is None:
            print(f"  [WARN] {fname} not in models/ and has no generator - "
                  f"SKIPPED. /{fname.split('.')[0]}/... RPCs will fail with "
                  f"'unable to find obj'.")
            continue
        if fname == "33000.xml":
            if full_33000:
                xml = extend_33000(xml)
            xml_33000 = xml
        res = idx.get(title) or idx.get(fname)
        want = base64.b64encode(xml.encode("utf-8")).decode("ascii")
        if res and not force and remote_data(tb, res) == want:
            print(f"  [same] {title}")
            continue
        if dry:
            print(f"  [dry ] would upload {title}  ({len(xml)} B, "
                  f"RIDs {len(model_rids(xml))})")
            changed.append(title)
            continue
        models.upload_model(tb.s, base, title, fname, xml)
        changed.append(title)
    return changed, xml_33000


# ── profile ────────────────────────────────────────────────────────────
def observed_33000_rids(prof: dict) -> list[int]:
    oa = prof["profileData"]["transportConfiguration"]["observeAttr"]
    out = []
    for p in oa.get("observe", []):
        m = re.match(r"^/33000_[\d.]+/\d+/(\d+)$", p)
        if m:
            out.append(int(m.group(1)))
    return sorted(out)


def patch_bench_attributes(tb, uptime_pmax: int, pmax_floor: int,
                           full_33000: bool, dry: bool) -> None:
    """BENCH TUNING - the only place this script deviates from production.

    1. Every OBSERVED path gets a non-zero pmax. pmax is the protocol-enforced
       heartbeat: with pmax=0 a resource is only reported when it changes, so
       a steady bench load leaves voltage/current/activePower looking frozen
       and there is no way to distinguish "steady" from "dead". The production
       baseline (tb_edge_provision.ATTRIBUTE_LWM2M) leaves /10242_1.0/0/4,5,6
       at pmax=0; OBSERVE_ADDITIONS never overrides them. Worth backporting.
    2. uptime_s (/33000_1.0/0/10) is the liveness anchor the checker times
       against; --uptime-pmax lets the bench tighten it (60 s) for fast
       iteration and put it back to the production 300 s for a real soak.
    3. --full-33000 also observes RID 37 (last reboot code), which is
       implemented in firmware but in neither the profile nor OBSERVE_ADDITIONS.
    """
    prof = L.get_profile(tb)
    if not prof:
        raise SystemExit(f"device profile '{L.PROFILE_NAME}' vanished")
    oa = prof["profileData"]["transportConfiguration"]["observeAttr"]
    observe = oa.setdefault("observe", [])
    telemetry = oa.setdefault("telemetry", [])
    key_name = oa.setdefault("keyName", {})
    attrs = oa.setdefault("attributeLwm2m", {})
    edits: list[str] = []

    if full_33000:
        for path, (key, pmin, pmax) in EXTRA_33000_OBSERVE.items():
            if path not in observe:
                observe.append(path)
                telemetry.append(path)
                key_name[path] = key
                attrs[path] = {"pmin": pmin, "pmax": pmax}
                edits.append(f"+observe {path} -> {key}")

    for path in observe:
        a = attrs.setdefault(path, {"pmin": 0, "pmax": 0})
        if not a.get("pmax"):
            a["pmax"] = pmax_floor
            edits.append(f"pmax {path} 0 -> {pmax_floor}s")

    up = "/33000_1.0/0/10"
    if up in observe and attrs.get(up, {}).get("pmax") != uptime_pmax:
        old = attrs.get(up, {}).get("pmax")
        attrs[up] = {"pmin": min(attrs.get(up, {}).get("pmin", 60), uptime_pmax),
                     "pmax": uptime_pmax}
        edits.append(f"pmax {up} (uptime_s) {old} -> {uptime_pmax}s")

    print(f"[bench]  {len(edits)} attribute edit(s)")
    for e in edits:
        print(f"         {e}")
    if not edits or dry:
        return
    tb.post("/api/deviceProfile", prof)
    print("[bench]  profile saved")


def coverage_report(prof: dict, xml_33000: str) -> None:
    """Cross-check: an observed RID that the uploaded model does not define
    cannot be typed by Leshan, so its telemetry never appears. Nothing in the
    repo checks this today - it is how RIDs 23..36 ended up observed-but-dead."""
    if not xml_33000:
        return
    have = set(model_rids(xml_33000))
    want = observed_33000_rids(prof)
    missing = [r for r in want if r not in have]
    print(f"[models] object 33000 model defines {len(have)} RIDs; profile "
          f"observes {len(want)}")
    if missing:
        print(f"[models] WARN: observed but NOT in the 33000 model: {missing}")
        print("         -> those keys will never appear in telemetry. "
              "Re-run with --full-33000 to add them (bench-only divergence).")


# ── device ─────────────────────────────────────────────────────────────
def set_inactivity_timeout(tb, device_id: str, minutes: int, dry: bool) -> None:
    """TB flips `active` after the profile's inactivityTimeout (600 s default)
    and the 'Node Offline' alarm keys straight off that flag. A single bench
    node on sparse LwM2M traffic flaps constantly; the per-device SERVER_SCOPE
    override quiets it without touching production defaults."""
    ms = minutes * 60 * 1000
    print(f"[bench]  inactivityTimeout = {minutes} min ({ms} ms)")
    if dry:
        return
    try:
        tb.post(f"/api/plugins/telemetry/DEVICE/{device_id}/SERVER_SCOPE",
                {"inactivityTimeout": ms})
    except Exception as e:
        print(f"[bench]  WARN: could not set inactivityTimeout ({e}); "
              "expect `active` to flap every 10 min")


# ── main ───────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=None,
                    help="bench TB host (default: probe localhost, 127.0.0.1, WSL IP)")
    ap.add_argument("--port", type=int, default=L.DEFAULT_TB_PORT)
    ap.add_argument("--user", default=L.fc.EDGE_TENANT_USER)
    ap.add_argument("--password", default=L.fc.EDGE_TENANT_PASS)
    ap.add_argument("--endpoint", default=L.BENCH_ENDPOINT,
                    help=f"LwM2M endpoint (default: {L.BENCH_ENDPOINT})")
    ap.add_argument("--mac", default=None,
                    help="derive the endpoint from a MAC instead")
    ap.add_argument("--wait", type=int, default=600,
                    help="seconds to wait for TB to come up (default 600)")
    ap.add_argument("--distro", default=L.DEFAULT_DISTRO)
    ap.add_argument("--uptime-pmax", type=int, default=300,
                    help="pmax for /33000_1.0/0/10 uptime_s. 300 = production, "
                         "60 = fast bench verification (default 300)")
    ap.add_argument("--pmax-floor", type=int, default=900,
                    help="pmax applied to observed paths that have none (default 900)")
    ap.add_argument("--inactivity-min", type=int, default=20,
                    help="per-device TB inactivity timeout in minutes (default 20)")
    ap.add_argument("--full-33000", action="store_true",
                    help="upload a 33000 model extended with RIDs 23..37 "
                         "(bench-only divergence; decodes post-mortem + reboot code)")
    ap.add_argument("--force-models", action="store_true",
                    help="re-upload models even if the stored copy is identical")
    ap.add_argument("--restart", choices=["auto", "always", "never"], default="auto",
                    help="restart the TB container so Leshan reloads models "
                         "(auto = only when a model changed)")
    ap.add_argument("--monitoring", action="store_true",
                    help="also apply the profile alarm rules + root rule-chain node")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    endpoint = L.fc.mac_to_endpoint(args.mac) if args.mac else args.endpoint
    dry = args.dry_run

    print("=" * 68)
    print("  BENCH ThingsBoard provisioning")
    print(f"  endpoint : {endpoint}")
    print(f"  profile  : {L.PROFILE_NAME}")
    print(f"  mode     : {'DRY RUN' if dry else 'APPLY'}")
    print("=" * 68)

    # 1 ── wait for TB ---------------------------------------------------
    try:
        base, tb = L.wait_for_tb(args.host, args.port, args.user, args.password,
                                 timeout_s=args.wait, distro=args.distro)
    except L.TBUnavailable as e:
        print(f"FAILED: {e}")
        return 2

    # 2 ── object models -------------------------------------------------
    print("\n-- object models --------------------------------------------")
    changed, xml_33000 = upload_models(tb, base, args.full_33000,
                                       args.force_models, dry)
    print(f"[models] {len(changed)} uploaded/updated, "
          f"{len(ARTEFACTS) - len(changed)} unchanged")

    # 3 ── restart so Leshan reloads the model provider -------------------
    want_restart = (args.restart == "always" or
                    (args.restart == "auto" and bool(changed)))
    if want_restart and not dry:
        name = L.tb_container(args.distro)
        if not name:
            print("[restart] WARN: no ThingsBoard container found via "
                  f"`wsl -d {args.distro} -- docker ps`.")
            print("[restart] Leshan loads models at STARTUP ONLY - restart TB "
                  "by hand before the node registers, or observes are dropped.")
        else:
            print(f"[restart] docker restart {name}")
            rc, out = L.wsl_run(["docker", "restart", name],
                                distro=args.distro, timeout=180)
            if rc != 0:
                print(f"[restart] WARN: rc={rc} {out[:200]}")
            else:
                time.sleep(5)
                base, tb = L.wait_for_tb(args.host, args.port, args.user,
                                         args.password, timeout_s=args.wait,
                                         distro=args.distro)
    elif want_restart:
        print("[restart] (dry-run) would restart the TB container")
    else:
        print("[restart] skipped (no model changed)")

    # 4 ── device profile ------------------------------------------------
    print("\n-- device profile -------------------------------------------")
    try:
        pid = prov.apply_profile(tb, dry)                 # production shape
        mon.apply_observe(tb, dry)                        # observe + pmin/pmax superset
        patch_bench_attributes(tb, args.uptime_pmax, args.pmax_floor,
                               args.full_33000, dry)
        prof = L.get_profile(tb)
        if prof:
            tc = prof["profileData"]["transportConfiguration"]
            oa = tc["observeAttr"]
            ver = tc.get("clientLwM2mSettings", {}).get("defaultObjectIDVer")
            n_obs = len(oa.get("observe", []))
            print(f"[profile] {n_obs} observed paths, "
                  f"{len(oa.get('telemetry', []))} telemetry keys, "
                  f"defaultObjectIDVer={ver!r}")
            if n_obs > FW_MAX_OBSERVER:
                print(f"[profile] WARN: {n_obs} observes vs the firmware's "
                      f"CONFIG_LWM2M_ENGINE_MAX_OBSERVER={FW_MAX_OBSERVER} "
                      "(prj.conf:304) - the client silently refuses the "
                      "surplus. On a single bench node this is the hard "
                      "ceiling to watch.")
            coverage_report(prof, xml_33000)
        if args.monitoring:
            mon.apply_alarms(tb, dry)
            mon.apply_rulechain(tb, dry)
    except SystemExit:
        raise
    except Exception as e:
        print(f"FAILED (profile): {e}")
        return 1

    # 5 ── device + NoSec credentials -------------------------------------
    print("\n-- device ---------------------------------------------------")
    try:
        prov.provision_device(tb, endpoint, pid, dry)
    except Exception as e:
        print(f"FAILED (device): {e}")
        return 1

    dev = L.find_device(tb, endpoint)
    if dev and not dry:
        did = dev["id"]["id"]
        print(f"[device] id={did}")
        # 6 ── bench inactivity override ---------------------------------
        set_inactivity_timeout(tb, did, args.inactivity_min, dry)
        creds = tb.get(f"/api/device/{did}/credentials")
        cv = creds.get("credentialsValue") or "{}"
        mode = ""
        try:
            mode = json.loads(cv).get("client", {}).get("securityConfigClientMode", "")
        except Exception:
            pass
        print(f"[device] credentials: {creds.get('credentialsType')} / "
              f"{mode or '?'} / id={creds.get('credentialsId')}")

    # ── next actions ------------------------------------------------------
    print("\n" + "=" * 68)
    print("  PROVISIONED. Two things this script cannot do for you:")
    print(f"  1) PUBLISH THE SRP RECORD - the firmware has no static server IP")
    print(f"     (removed v0.6.65); it resolves {L.SRV_TYPE_DOMAIN}")
    print(f"     via the OTBR SRP server, and falls back to an A/AAAA lookup of")
    print(f"     {L.HOST_FQDN} with port hardcoded to {L.LWM2M_PORT}.")
    print( "     Verify:  wsl -d %s -u root -- ot-ctl srp server service" % args.distro)
    print( "  2) POWER-CYCLE THE NODE - a profile change reaches a device only")
    print( "     on REGISTER (or via a Reboot RPC to an already-registered node).")
    print( "  Then:  python tools/lab_tb/lab_tb_check.py --strict")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
