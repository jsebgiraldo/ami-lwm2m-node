"""Verify the whole bench chain end to end and print one PASS/FAIL verdict.

The bench has six independent places to fail, and five of them fail SILENTLY -
the node just never appears in ThingsBoard. This walks the chain in dependency
order and stops guessing:

  1  TB up ................ /api/auth/login returns a token
  2  LwM2M transport ...... UDP 5683 bound in the WSL netns (host networking),
                            container log shows "Started endpoint at coap://"
                            and no BindException (the CoAP transport grabs 5683
                            first unless COAP_BIND_PORT is moved - documented
                            landmine, already burned once on this project)
  3  SRP record .......... _lwm2m._udp.default.service.arpa. published, port
                            5683, host thingsboard-edge.*, advertised on an
                            OMR (non mesh-local) address. THIS is the gate: the
                            firmware has no static server IP since v0.6.65 and
                            resolves the server only via DNS-SD
                            (src/lwm2m_discover.c:23-26).
  4  device provisioned ... device exists, LWM2M_CREDENTIALS / NO_SEC, and the
                            profile actually observes /33000_1.0/0/10 with a
                            non-zero pmax
  5  REGISTERED ........... server-scope lastActivityTime is fresh
  6  telemetry ............ Object 33000 keys present and fresh; with --strict,
                            uptime_s must still be INCREASING over a sampling
                            window (a value that never moves is the classic
                            "frozen telemetry" = observed path missing from the
                            profile, not a dead node)
  7  inbound RPC .......... two-way Read /3/0/3. REG_UPDATE only proves the
                            node->server half; a stuck node keeps registering
                            while every inbound observe/RPC is black-holed.

Exit code: 0 all checks passed, 1 at least one FAIL, 2 the bench TB is not
reachable at all (nothing else could be evaluated).

USAGE
  python tools/lab_tb/lab_tb_check.py
  python tools/lab_tb/lab_tb_check.py --strict           # gate before a soak
  python tools/lab_tb/lab_tb_check.py --wait 300 --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lab_tb_common as L  # noqa: E402

import tb_edge_provision as prov            # noqa: E402
import tb_edge_monitoring_setup as mon      # noqa: E402

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"
_MARK = {PASS: L.OK, FAIL: L.NO, WARN: L.WR, SKIP: L.SK}

# Fallback set of Object-33000 telemetry keys, from the same maps the
# provisioner writes into the profile - never hardcoded here. The LIVE profile
# is preferred (expected_keys below): the two maps disagree on two keys
# (KEY_NAME says last_error_uptime_s / storm_backoff_applied, OBSERVE_ADDITIONS
# overwrites them with last_error_uptime / storm_backoff), so their union
# over-counts what TB will actually emit.
KEYS_33000 = sorted(
    {k for p, k in prov.KEY_NAME.items() if p.startswith("/33000_")} |
    {v[0] for p, v in mon.OBSERVE_ADDITIONS.items() if p.startswith("/33000_")}
)
UPTIME_PATH = "/33000_1.0/0/10"
UPTIME_KEY = "uptime_s"
# src/lwm2m_obj_thread_diag.h / prj.conf:304 - CONFIG_LWM2M_ENGINE_MAX_OBSERVER
FW_MAX_OBSERVER = 36


def expected_keys(prof: dict | None) -> list[str]:
    """Object-33000 telemetry keys TB is actually configured to receive: the
    profile's keyName entries whose path is in the `observe` list."""
    if not prof:
        return KEYS_33000
    oa = prof["profileData"]["transportConfiguration"].get("observeAttr", {})
    obs = set(oa.get("observe", []))
    keys = sorted({k for p, k in (oa.get("keyName") or {}).items()
                   if p.startswith("/33000_") and p in obs})
    return keys or KEYS_33000


class Report:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, name: str, status: str, detail: str, hint: str = "") -> str:
        self.rows.append({"check": name, "status": status,
                          "detail": detail, "hint": hint})
        print(f"{_MARK[status]} {name:22s} {detail}")
        if hint and status in (FAIL, WARN):
            print(f"       -> {hint}")
        return status

    @property
    def failures(self) -> int:
        return sum(1 for r in self.rows if r["status"] == FAIL)

    @property
    def warnings(self) -> int:
        return sum(1 for r in self.rows if r["status"] == WARN)


# ── 2. LwM2M transport ─────────────────────────────────────────────────
def check_transport(rep: Report, distro: str, strict: bool) -> None:
    ports = L.udp_ports(distro)
    if not ports:
        rep.add("lwm2m transport", WARN,
                "could not read /proc/net/udp6 inside WSL",
                f"check `wsl -d {distro} -u root -- cat /proc/net/udp6`")
    elif L.LWM2M_PORT in ports:
        rep.add("lwm2m transport", PASS,
                f"UDP {L.LWM2M_PORT} bound in the WSL netns")
    else:
        rep.add("lwm2m transport", FAIL,
                f"nothing listening on UDP {L.LWM2M_PORT} "
                f"({len(ports)} other UDP ports bound)",
                "TB env must be LWM2M_ENABLED=true + LWM2M_BIND_PORT=5683 "
                "(NOT LWM2M_SERVER_PORT, which is silently ignored) and the "
                "CoAP transport moved off 5683: COAP_BIND_PORT=5690, "
                "COAP_ENABLED=false, COAP_SERVER_ENABLED=false. The container "
                "must also run with host networking or it has no route to wpan0.")

    name = L.tb_container(distro)
    if not name:
        rep.add("lwm2m bind log", SKIP, "no ThingsBoard container found")
        return
    logs = L.docker_logs(name, 3000, distro)
    if not logs:
        rep.add("lwm2m bind log", SKIP, f"no readable log for container {name}")
        return
    if "BindException" in logs or "Address already in use" in logs:
        line = next((l.strip() for l in logs.splitlines()
                     if "BindException" in l or "Address already in use" in l), "")
        rep.add("lwm2m bind log", FAIL, f"{name}: {line[:110]}",
                "another transport took 5683 first - move CoAP to 5690")
    elif "Started endpoint at coap://" in logs:
        line = next((l.strip() for l in logs.splitlines()
                     if "Started endpoint at coap://" in l), "")
        rep.add("lwm2m bind log", PASS, line[-70:] if line else name)
    else:
        rep.add("lwm2m bind log", FAIL if strict else WARN,
                f"{name}: no 'Started endpoint at coap://' line in the last 3000 lines",
                "the LwM2M transport probably never started; grep the full log")


# ── 3. SRP / DNS-SD ────────────────────────────────────────────────────
def check_srp(rep: Report, distro: str) -> None:
    rc, state = L.ot_ctl(["srp", "server", "state"], distro=distro)
    if rc != 0:
        rep.add("srp server", FAIL, f"ot-ctl unreachable: {state[:90]}",
                f"run `wsl -d {distro} -u root -- ot-ctl state` by hand")
        return
    if "running" not in state.lower():
        rep.add("srp server", FAIL, f"state = {state.splitlines()[0][:40]!r}",
                "wsl -d %s -u root -- ot-ctl srp server enable" % distro)
        return
    rep.add("srp server", PASS, "running")

    _, hosts = L.ot_ctl(["srp", "server", "host"], distro=distro)
    hit = next(((h, b) for h, b in L.srp_blocks(hosts)
                if h.lower().startswith(L.HOST_FQDN.split(".")[0])), None)
    if not hit or "deleted: false" not in hit[1]:
        rep.add("srp host", FAIL, f"{L.HOST_FQDN} missing or deleted",
                "publish it from the OTBR's own SRP client, or with "
                "avahi-publish over the Advertising Proxy")
    else:
        rep.add("srp host", PASS, f"{hit[0]} {hit[1][:60]}")

    _, svcs = L.ot_ctl(["srp", "server", "service"], distro=distro)
    blocks = [(h, b) for h, b in L.srp_blocks(svcs)
              if "_lwm2m._udp" in h.lower()]
    if not blocks:
        rep.add("srp service", FAIL, f"no {L.SRV_TYPE_DOMAIN} record published",
                "THIS is why the node cannot find the server. It queries "
                f"{L.SRV_FQDN} and reboots after "
                "CONFIG_AMI_LWM2M_DNS_SD_RETRY_MAX failures.")
        return
    # prefer the exact instance the firmware asks for, else grade whatever
    # _lwm2m._udp record is published
    header, body = next(((h, b) for h, b in blocks
                         if h.lower() == L.SRV_FQDN.lower()), blocks[0])
    if header.lower() != L.SRV_FQDN.lower():
        rep.add("srp service", WARN, f"instance is {header}, firmware wants {L.SRV_FQDN}",
                "the instance label is a compiled-in constant "
                "(src/lwm2m_discover.c:24) - republish with the exact label")
    port = re.search(r"port:\s*(\d+)", body)
    port_v = int(port.group(1)) if port else -1
    if "deleted: false" not in body:
        rep.add("srp service", FAIL, f"{header} is marked deleted",
                "re-register the service; SRP leases expire")
    elif port_v != L.LWM2M_PORT:
        rep.add("srp service", FAIL, f"port {port_v}, expected {L.LWM2M_PORT}",
                "strategy 2 of the firmware's discovery hardcodes 5683 "
                "(src/lwm2m_discover.c:20) - both strategies must agree")
    else:
        rep.add("srp service", PASS, f"{header} port {port_v}")

    ml = L.mesh_local_prefix(distro)
    addrs = L.addrs_in(body) or L.addrs_in(hosts)
    omr = [a for a in addrs if L.is_omr(a, ml)]
    if not addrs:
        rep.add("srp address", WARN, "record advertises no address",
                "the node resolves the SRV then needs an AAAA for the host")
    elif omr:
        rep.add("srp address", PASS, f"OMR {omr[0]}")
    else:
        rep.add("srp address", FAIL,
                f"only mesh-local address(es) advertised: {addrs}",
                "the BR answers from its OMR address, so a mesh-local "
                "destination breaks src/dst symmetry on the node's connected "
                "UDP socket (src/lwm2m_discover.c:44-56 prefers OMR)")


# ── 4. device provisioned ──────────────────────────────────────────────
def check_device(rep: Report, tb, endpoint: str) -> tuple[dict | None, dict | None]:
    prof = L.get_profile(tb)
    if not prof:
        rep.add("device profile", FAIL, f"'{L.PROFILE_NAME}' not found",
                "python tools/lab_tb/lab_tb_provision.py")
        return None, None
    tc = prof["profileData"]["transportConfiguration"]
    oa = tc.get("observeAttr", {})
    obs = oa.get("observe", [])
    pmax = (oa.get("attributeLwm2m", {}).get(UPTIME_PATH) or {}).get("pmax", 0)
    if prof.get("transportType") != "LWM2M":
        rep.add("device profile", FAIL,
                f"transportType={prof.get('transportType')}", "recreate the profile")
    elif UPTIME_PATH not in obs:
        rep.add("device profile", FAIL,
                f"{len(obs)} observed paths but {UPTIME_PATH} is NOT one",
                "a path in keyName/telemetry but missing from `observe` never "
                "gets an Observe installed -> frozen telemetry forever")
    elif not pmax:
        rep.add("device profile", WARN,
                f"{len(obs)} observed paths, uptime_s pmax=0 (no heartbeat)",
                "pmax is the only protocol-enforced cadence guarantee")
    else:
        rep.add("device profile", PASS,
                f"{len(obs)} observed paths, uptime_s pmin/pmax="
                f"{oa['attributeLwm2m'][UPTIME_PATH].get('pmin')}/{pmax}s")
    if len(obs) > FW_MAX_OBSERVER:
        rep.add("observer budget", WARN,
                f"{len(obs)} observed paths vs firmware ceiling {FW_MAX_OBSERVER}",
                "CONFIG_LWM2M_ENGINE_MAX_OBSERVER (prj.conf:304) caps the client's "
                "observe table; the paths past the cap are silently refused. "
                "Trim the observe list or raise the Kconfig and rebuild.")

    dev = L.find_device(tb, endpoint)
    if not dev:
        rep.add("device", FAIL, f"{endpoint} not in ThingsBoard",
                "provisionType is DISABLED, so an unknown endpoint is rejected "
                "with LwM2MAuthException and the node reboot-loops. Run "
                f"lab_tb_provision.py --endpoint {endpoint}")
        return None, prof
    did = dev["id"]["id"]
    try:
        creds = tb.get(f"/api/device/{did}/credentials")
    except Exception as e:
        rep.add("device", FAIL, f"cannot read credentials: {e}")
        return dev, prof
    mode = ""
    try:
        mode = json.loads(creds.get("credentialsValue") or "{}") \
                   .get("client", {}).get("securityConfigClientMode", "")
    except Exception:
        pass
    ok = (creds.get("credentialsType") == "LWM2M_CREDENTIALS"
          and creds.get("credentialsId") == endpoint and mode == "NO_SEC")
    rep.add("device", PASS if ok else FAIL,
            f"{endpoint} id={did[:8]} {creds.get('credentialsType')}/{mode or '?'}"
            f" credId={creds.get('credentialsId')}",
            "" if ok else "credentials must be LWM2M_CREDENTIALS, NO_SEC, "
                          "credentialsId == the endpoint the firmware sends")
    return dev, prof


# ── 5. registration freshness ──────────────────────────────────────────
def server_attrs(tb, did: str) -> dict:
    try:
        rows = tb.get(f"/api/plugins/telemetry/DEVICE/{did}/values/attributes/SERVER_SCOPE")
    except Exception:
        return {}
    return {r.get("key"): r.get("value") for r in rows if isinstance(r, dict)}


def check_registered(rep: Report, tb, did: str, max_age_s: int) -> None:
    a = server_attrs(tb, did)
    last = a.get("lastActivityTime") or 0
    if not last:
        rep.add("registered", FAIL, "no lastActivityTime - never registered",
                "the node has not reached the server. Check SRP above, then "
                "the node's own CoAP diag: python tools/diag_get.py --local "
                "--addr <node OMR addr>  (proves the node is alive on the mesh "
                "independently of ThingsBoard)")
        return
    age = (L.now_ms() - int(last)) / 1000.0
    detail = (f"lastActivity {age:.0f}s ago, active={a.get('active')}, "
              f"lastConnect={'yes' if a.get('lastConnectTime') else 'no'}")
    if age <= max_age_s:
        rep.add("registered", PASS, detail)
    else:
        rep.add("registered", FAIL, detail + f" (limit {max_age_s}s)",
                "registered at some point but has gone quiet - check the node "
                "console / diag; CONFIG_LWM2M_UPDATE_PERIOD=300 should refresh "
                "this every 5 min even with lifetime=86400")


# ── 6. telemetry ───────────────────────────────────────────────────────
def timeseries(tb, did: str, keys: list[str] | None = None) -> dict:
    params = {"keys": ",".join(keys)} if keys else None
    try:
        return tb.get(f"/api/plugins/telemetry/DEVICE/{did}/values/timeseries",
                      params)
    except Exception:
        return {}


def check_telemetry(rep: Report, tb, did: str, keys: list[str], min_keys: int,
                    max_age_s: int, delta_wait: int) -> None:
    ts = timeseries(tb, did)
    present = [k for k in keys if k in ts]
    if not ts:
        rep.add("telemetry", FAIL, "no timeseries keys at all",
                "registration without telemetry = the object models were "
                "missing when the device registered. Upload models, RESTART "
                "TB, then power-cycle the node.")
        return
    if len(present) < min_keys:
        rep.add("telemetry", FAIL,
                f"only {len(present)}/{len(keys)} Object-33000 keys "
                f"(need {min_keys}); {len(ts)} keys total",
                "TB drops observes for an experimental object whose model it "
                "cannot match exactly - keep 33000 at wire version 1.0")
        return

    entry = ts.get(UPTIME_KEY) or []
    if not entry:
        rep.add("telemetry", FAIL,
                f"{len(present)} Object-33000 keys but no {UPTIME_KEY}",
                f"{UPTIME_PATH} must be in the profile's `observe` list")
        return
    v0 = entry[0]
    age = (L.now_ms() - int(v0.get("ts", 0))) / 1000.0
    detail = (f"{len(present)}/{len(keys)} 33000 keys, {len(ts)} total, "
              f"{UPTIME_KEY}={v0.get('value')} ({age:.0f}s old)")
    if age > max_age_s:
        rep.add("telemetry", FAIL, detail + f" (limit {max_age_s}s)",
                "stale beyond the pmax heartbeat: either the Observe was never "
                "installed or the node stopped notifying")
        return
    rep.add("telemetry", PASS, detail)

    if delta_wait <= 0:
        rep.add("telemetry live", SKIP, "delta sampling disabled (--delta-wait 0)")
        return
    print(f"       sampling {UPTIME_KEY} again in {delta_wait}s "
          "(proves live notifies, not a frozen registration payload)...")
    time.sleep(delta_wait)
    ts2 = timeseries(tb, did, [UPTIME_KEY])
    e2 = (ts2.get(UPTIME_KEY) or [{}])[0]
    try:
        a, b = int(v0.get("value")), int(e2.get("value"))
    except (TypeError, ValueError):
        rep.add("telemetry live", WARN, f"non-numeric {UPTIME_KEY}: {e2}")
        return
    if b > a:
        rep.add("telemetry live", PASS,
                f"{UPTIME_KEY} {a} -> {b} (+{b - a}s in {delta_wait}s)")
    else:
        rep.add("telemetry live", FAIL,
                f"{UPTIME_KEY} frozen at {a} over {delta_wait}s",
                "the classic frozen-telemetry bug: the value shown is the "
                "Registration payload, no Observe is installed. Re-save the "
                "profile and power-cycle the node.")


# ── 7. inbound RPC ─────────────────────────────────────────────────────
def check_rpc(rep: Report, tb, did: str, strict: bool) -> None:
    t0 = time.time()
    try:
        r = L.rpc(tb, did, "Read", {"id": "/3/0/3"}, timeout_ms=15000)
    except Exception as e:
        rep.add("inbound rpc", FAIL if strict else WARN, f"Read /3/0/3 raised {e}")
        return
    dt = time.time() - t0
    val = str(r.get("value", ""))
    if val and "error" not in r:
        rep.add("inbound rpc", PASS, f"Read /3/0/3 -> {val[:60]} ({dt:.1f}s)")
    else:
        rep.add("inbound rpc", FAIL if strict else WARN,
                f"Read /3/0/3 -> {json.dumps(r)[:110]} ({dt:.1f}s)",
                "outbound REGISTER works but the server->node direction does "
                "not. That is the state where a node looks alive forever and "
                "delivers nothing - check the OTBR address cache "
                f"(ot-ctl eidcache) and that the server address is OMR.")


# ── main ───────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=L.DEFAULT_TB_PORT)
    ap.add_argument("--user", default=L.fc.EDGE_TENANT_USER)
    ap.add_argument("--password", default=L.fc.EDGE_TENANT_PASS)
    ap.add_argument("--endpoint", default=L.BENCH_ENDPOINT)
    ap.add_argument("--distro", default=L.DEFAULT_DISTRO)
    ap.add_argument("--wait", type=int, default=30,
                    help="seconds to wait for TB before failing (default 30)")
    ap.add_argument("--max-age", type=int, default=360,
                    help="max lastActivityTime / telemetry age in seconds "
                         "(default 360 = the 300s uptime_s pmax + 20%% slack)")
    ap.add_argument("--min-keys", type=int, default=8,
                    help="minimum Object-33000 telemetry keys (default 8)")
    ap.add_argument("--delta-wait", type=int, default=0,
                    help="re-sample uptime_s after N seconds to prove live "
                         "notifies; 0 disables (--strict sets 120)")
    ap.add_argument("--no-rpc", action="store_true",
                    help="skip the inbound two-way Read /3/0/3")
    ap.add_argument("--strict", action="store_true",
                    help="gate mode: delta-wait 120 and WARN counts as FAIL")
    ap.add_argument("--json", action="store_true",
                    help="print a machine-readable summary as the last line")
    args = ap.parse_args()

    if args.strict and not args.delta_wait:
        args.delta_wait = 120

    rep = Report()
    print("=" * 68)
    print(f"  BENCH CHAIN CHECK   endpoint={args.endpoint}   "
          f"{'STRICT' if args.strict else 'normal'}")
    print("=" * 68)

    # 1 ── TB up ---------------------------------------------------------
    t0 = time.time()
    try:
        base, tb = L.wait_for_tb(args.host, args.port, args.user, args.password,
                                 timeout_s=args.wait, distro=args.distro,
                                 quiet=True)
    except L.TBUnavailable as e:
        rep.add("thingsboard", FAIL, str(e)[:110],
                "start the bench stack, then re-run")
        print("\nVERDICT: FAIL (bench TB unreachable - nothing else evaluated)")
        if args.json:
            print("JSON " + json.dumps({"verdict": "FAIL", "rows": rep.rows}))
        return 2
    rep.add("thingsboard", PASS, f"{base} login OK ({time.time() - t0:.1f}s)")

    # 2 ── transport -----------------------------------------------------
    check_transport(rep, args.distro, args.strict)

    # 3 ── SRP -----------------------------------------------------------
    check_srp(rep, args.distro)

    # 4 ── device --------------------------------------------------------
    dev, prof = check_device(rep, tb, args.endpoint)

    # 5-7 ── node-side ---------------------------------------------------
    if dev:
        did = dev["id"]["id"]
        check_registered(rep, tb, did, args.max_age)
        check_telemetry(rep, tb, did, expected_keys(prof), args.min_keys,
                        args.max_age, args.delta_wait)
        if args.no_rpc:
            rep.add("inbound rpc", SKIP, "--no-rpc")
        else:
            check_rpc(rep, tb, did, args.strict)
    else:
        for n in ("registered", "telemetry", "inbound rpc"):
            rep.add(n, SKIP, "device missing")

    fails = rep.failures + (rep.warnings if args.strict else 0)
    print("\n" + "=" * 68)
    print(f"  {len(rep.rows)} checks | {rep.failures} FAIL | {rep.warnings} WARN")
    if fails == 0:
        print("  VERDICT: PASS - the bench is representative: the node is "
              "registered\n           and streaming, so overlays/lab.conf can "
              "go back to the\n           production watchdog values "
              "(CONFIG_AMI_BOOT_REGISTER_DEADLINE_S,\n           "
              "CONFIG_AMI_HW_WATCHDOG_BOOT_GRACE_HARD_S) and be rebuilt.")
    else:
        first = next(r for r in rep.rows
                     if r["status"] == FAIL or (args.strict and r["status"] == WARN))
        print(f"  VERDICT: FAIL - fix '{first['check']}' first: {first['detail'][:80]}")
    print("=" * 68)
    if args.json:
        print("JSON " + json.dumps({
            "verdict": "PASS" if fails == 0 else "FAIL",
            "endpoint": args.endpoint, "base": base,
            "fails": rep.failures, "warns": rep.warnings, "rows": rep.rows}))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
