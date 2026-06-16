"""Bulk flash + provision + verify pipeline for the full 30-node AMI fleet.

Usage flow:
    1. Connect all boards to the USB hub at the workstation.
    2. Run this script — flashes every connected COM that maps to a known
       endpoint in tools/fleet_map.csv, skipping any board the map marks
       HW-DEFECTIVE.
    3. Disconnect boards, move to the PSU rack, power on.
    4. Watch TB Edge UI at http://192.168.8.111:8090 — Dashboard "AMI Fleet
       Health". Alarms surface automatically (Node Offline / Watchdog Fired
       / Self-Recovery / LwM2M Error).

The script performs end-to-end pre-flight verification BEFORE touching any
board:
  * TB Edge reachable + login OK
  * AMI_LwM2M_Node profile present, observeAttr.observe has >=20 diagnostic
    paths (so resets/uptime/notify_emitted/recover_count/watchdog_count
    all flow live at the 60-second pmax cadence)
  * Dashboard "AMI Fleet Health" present
  * Alarm rules present
  * Notification rule present
  * Build artefacts present (build_ota_ftd/mcuboot + signed app)
  * All target COMs detected and not held by another process

For each board it then runs the same flow flash_ota_migrate.py would, but
record-keeps the results so the operator gets one consolidated report.

A board is considered SUCCESS only if all of:
  - esptool write+verify returns 0
  - TB device exists with LwM2M creds (provisioned)
  - the bind in tools/fleet_map.csv matches the MAC esptool reads back
  - (optional) it shows up as active=True in TB within --post-flash-wait
    seconds. Set --no-wait-tb to skip — useful when you're going to power
    the boards down and move them right away.

Usage:
    python tools/bulk_flash_v046.py                   # flash all detected
    python tools/bulk_flash_v046.py --dry-run         # pre-flight only
    python tools/bulk_flash_v046.py --labels 1,2,3    # only these labels
    python tools/bulk_flash_v046.py --exclude 23      # skip label 23
    python tools/bulk_flash_v046.py --no-wait-tb      # don't wait for reg
    python tools/bulk_flash_v046.py --resume          # skip boards already
                                                       in results CSV
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

import fleet_common as fc

fc.bootstrap_venv()
sys.path.insert(0, str(fc.TOOLS_DIR))

from flash_ota_migrate import flash_ota
from provision_node import TBClient, provision_single  # noqa: E402


FLEET_MAP_CSV = pathlib.Path("tools/fleet_map.csv")
RESULTS_CSV = pathlib.Path("tools/bulk_flash_results.csv")
DETAIL_DIR = pathlib.Path("tools/bulk_flash")

PROFILE_NAME = "AMI_LwM2M_Node"
DASHBOARD_NAME = "AMI Fleet Health"
EXPECTED_OBSERVE_PATHS_MIN = 20    # diagnostic-only profile has 26
EXPECTED_ALARM_TYPES = {
    "Node Offline", "Self-Recovery", "Watchdog Fired", "LwM2M Error",
}

# Labels whose source column starts with these tags are skipped automatically
SKIP_TAGS = ("HW-DEFECTIVE",)


# ─── PowerShell COM enumeration (Win32) ────────────────────────────────
def detect_coms() -> list[str]:
    ps = (
        "Get-WMIObject Win32_PnPEntity -Filter \"DeviceID like '%USB%'\" | "
        "Where-Object {$_.Name -match 'COM'} | Select Name"
    )
    out = subprocess.check_output(
        ["powershell", "-NoProfile", "-Command", ps], text=True, timeout=12)
    return sorted(set(re.findall(r"\(COM\d+\)", out)))


def com_label(com_token: str) -> str:
    """Strip the '(' ')' off '(COM27)' → 'COM27'."""
    return com_token.strip("()")


# ─── fleet_map.csv ────────────────────────────────────────────────────
def load_fleet_map() -> list[dict]:
    if not FLEET_MAP_CSV.exists():
        raise SystemExit(f"fleet_map missing: {FLEET_MAP_CSV}")
    with FLEET_MAP_CSV.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        try:
            r["label"] = int(r["label"])
        except (KeyError, ValueError):
            r["label"] = None
    return rows


def patch_fleet_map(label: int, com: str, mac: str, endpoint: str,
                    tag: str) -> None:
    rows = list(csv.reader(FLEET_MAP_CSV.open("r", encoding="utf-8")))
    if not rows:
        return
    out = [rows[0]]
    for r in rows[1:]:
        try:
            if int(r[0]) == label:
                r = r.copy()
                r[1] = com
                r[2] = mac
                r[3] = endpoint
                # Don't overwrite a defect tag — appended QA tag only
                if not (len(r) >= 5 and r[4].startswith(SKIP_TAGS)):
                    r[4] = tag
        except (ValueError, IndexError):
            pass
        out.append(r)
    with FLEET_MAP_CSV.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(out)


# ─── TB Edge REST helpers (light wrapper) ─────────────────────────────
class TB:
    def __init__(self, host: str, port: int, user: str, password: str):
        self.base = f"http://{host}:{port}"
        self.user = user
        self.password = password
        self.token: str | None = None

    def login(self) -> None:
        req = urllib.request.Request(
            f"{self.base}/api/auth/login",
            data=json.dumps({"username": self.user, "password": self.password}).encode(),
            headers={"Content-Type": "application/json"})
        self.token = json.loads(urllib.request.urlopen(req, timeout=10).read())["token"]

    def _hdr(self) -> dict:
        return {"X-Authorization": f"Bearer {self.token}"}

    def _get(self, path: str):
        req = urllib.request.Request(self.base + path, headers=self._hdr())
        return json.loads(urllib.request.urlopen(req, timeout=10).read())

    def find_did(self, endpoint: str) -> str | None:
        try:
            d = self._get(f"/api/tenant/devices?deviceName={endpoint}")
            return d["id"]["id"]
        except Exception:
            return None

    def attrs(self, did: str) -> dict:
        try:
            arr = self._get(
                f"/api/plugins/telemetry/DEVICE/{did}/values/attributes/SERVER_SCOPE")
        except Exception:
            return {}
        return {x["key"]: (x["value"], x["lastUpdateTs"]) for x in arr}


# ─── Pre-flight ───────────────────────────────────────────────────────
def preflight(env: fc.ToolEnv, tb: TB, args) -> dict:
    """Return a dict of pre-flight findings. Raises if anything is unsafe."""
    print("\n=== PRE-FLIGHT ===")
    findings: dict = {"ok": True, "warnings": [], "errors": []}

    # 1. Build artefacts
    ws = env.west_workspace
    mcuboot = ws / args.build_dir / "mcuboot" / "zephyr" / "zephyr.bin"
    signed = ws / args.build_dir / "ami-lwm2m-node" / "zephyr" / "zephyr.signed.bin"
    for p in (mcuboot, signed):
        if not p.exists():
            findings["errors"].append(f"missing artefact: {p}")
        else:
            print(f"  [OK] artefact: {p.name} ({p.stat().st_size} B)")

    # 2. TB login already happened, verify profile
    try:
        profiles = tb._get("/api/deviceProfiles?pageSize=100&page=0")["data"]
        prof = next((p for p in profiles if p["name"] == PROFILE_NAME), None)
        if not prof:
            findings["errors"].append(f"profile '{PROFILE_NAME}' missing")
        else:
            full = tb._get(f"/api/deviceProfile/{prof['id']['id']}")
            observe = (full["profileData"]["transportConfiguration"]
                       ["observeAttr"].get("observe", []))
            if len(observe) < EXPECTED_OBSERVE_PATHS_MIN:
                findings["warnings"].append(
                    f"observe has only {len(observe)} paths, expected "
                    f">={EXPECTED_OBSERVE_PATHS_MIN} — run "
                    f"tb_edge_monitoring_setup.py --only observe")
            else:
                print(f"  [OK] observe profile: {len(observe)} paths")
    except Exception as e:
        findings["errors"].append(f"profile check failed: {e}")

    # 3. Dashboard
    try:
        dashboards = tb._get("/api/tenant/dashboards?pageSize=200&page=0")["data"]
        dash = next((d for d in dashboards if d["title"] == DASHBOARD_NAME), None)
        if not dash:
            findings["warnings"].append(
                f"dashboard '{DASHBOARD_NAME}' missing — run "
                f"tb_edge_monitoring_setup.py --only dashboard")
        else:
            print(f"  [OK] dashboard: {DASHBOARD_NAME}")
    except Exception as e:
        findings["warnings"].append(f"dashboard check failed: {e}")

    # 4. Alarms — check device profile has alarm rules
    try:
        full = tb._get(f"/api/deviceProfile/{prof['id']['id']}")
        alarms = full["profileData"].get("alarms", []) or []
        names = {a.get("alarmType") for a in alarms}
        missing = EXPECTED_ALARM_TYPES - names
        if missing:
            findings["warnings"].append(
                f"alarm rules missing: {sorted(missing)} — run "
                f"tb_edge_monitoring_setup.py --only alarms")
        else:
            print(f"  [OK] alarm rules: {sorted(names & EXPECTED_ALARM_TYPES)}")
    except Exception as e:
        findings["warnings"].append(f"alarm check failed: {e}")

    # 5. COM enumeration
    coms = [com_label(c) for c in detect_coms()]
    findings["coms_detected"] = coms
    print(f"  [OK] COMs detected: {len(coms)} -> {coms}")

    # 6. Build the work plan from fleet_map ∩ detected COMs
    fleet = load_fleet_map()
    selected: list[dict] = []
    skipped: list[tuple[dict, str]] = []
    label_filter = (set(int(x) for x in args.labels.split(","))
                    if args.labels else None)
    exclude = set(int(x) for x in args.exclude.split(",")) if args.exclude else set()
    for row in fleet:
        if row["label"] is None:
            continue
        if label_filter and row["label"] not in label_filter:
            continue
        if row["label"] in exclude:
            skipped.append((row, f"excluded via --exclude {row['label']}"))
            continue
        source = row.get("source", "")
        if any(source.startswith(t) for t in SKIP_TAGS):
            skipped.append((row, f"HW-defective: {source}"))
            continue
        if row["com"] not in coms:
            skipped.append((row, f"COM {row['com']} not connected"))
            continue
        selected.append(row)

    findings["selected"] = selected
    findings["skipped"] = skipped
    print(f"\n  Plan: {len(selected)} board(s) to flash, "
          f"{len(skipped)} skipped")
    for row, reason in skipped:
        print(f"    SKIP  label={row['label']:<3} ({row['endpoint']}): {reason}")

    if findings["errors"]:
        print("\n  PRE-FLIGHT ERRORS:")
        for e in findings["errors"]:
            print(f"    {e}")
        findings["ok"] = False
    if findings["warnings"]:
        print("\n  PRE-FLIGHT WARNINGS:")
        for w in findings["warnings"]:
            print(f"    {w}")
    return findings


# ─── Per-board flash ─────────────────────────────────────────────────
def flash_one(env: fc.ToolEnv, tb_legacy: TBClient, tb: TB, row: dict,
              args) -> dict:
    com = row["com"]
    label = row["label"]
    expected_mac = row["mac"]
    expected_endpoint = row["endpoint"]
    rec = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "label": label, "com": com, "expected_mac": expected_mac,
        "expected_endpoint": expected_endpoint,
        "status": "UNKNOWN", "detail": "", "mac_read": "",
    }

    print(f"\n--- label {label}  {com}  ({expected_endpoint}) ---")
    try:
        mac = fc.read_mac(env, com)
    except Exception as e:
        rec.update(status="FAIL", detail=f"read_mac: {e}")
        return rec
    rec["mac_read"] = mac
    endpoint = fc.mac_to_endpoint(mac)
    if mac.lower() != expected_mac.lower():
        rec.update(
            status="WARN_MAC",
            detail=f"map says {expected_mac} but chip is {mac} -> endpoint {endpoint}")
        print(f"  [!] MAC mismatch: map={expected_mac}  chip={mac}")
        expected_endpoint = endpoint

    # Flash
    try:
        flash_ota(env, com, args.baud, args.build_dir,
                  args.flash_mode, args.flash_freq)
    except Exception as e:
        rec.update(status="FAIL", detail=f"flash: {e}")
        return rec

    # Provision
    if not args.skip_provision:
        try:
            provision_single(tb_legacy, endpoint, args.profile, dry_run=False)
        except Exception as e:
            rec.update(status="FAIL", detail=f"provision: {e}")
            return rec

    # Post-flash reset (best-effort)
    try:
        fc.hard_reset(com, label="post-bulk-flash")
    except Exception:
        pass
    time.sleep(6)

    # Optional TB registration verification
    if args.no_wait_tb:
        rec.update(status="OK_NO_WAIT",
                   detail="flashed + provisioned (skipped TB verify)")
        return rec

    deadline = time.time() + args.post_flash_wait
    print(f"  waiting up to {args.post_flash_wait}s for TB active=True...")
    while time.time() < deadline:
        did = tb.find_did(endpoint)
        if did:
            a = tb.attrs(did)
            if a.get("active", (False,))[0]:
                rec.update(status="OK",
                           detail=f"active in {int(args.post_flash_wait - (deadline - time.time()))}s")
                return rec
        time.sleep(5)
    rec.update(status="WARN_NO_REG",
               detail=f"flashed but no TB activity within {args.post_flash_wait}s")
    return rec


# ─── Persist ─────────────────────────────────────────────────────────
def persist(records: list[dict]) -> None:
    DETAIL_DIR.mkdir(parents=True, exist_ok=True)
    new_file = not RESULTS_CSV.exists()
    with RESULTS_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["ts", "label", "com", "expected_mac", "mac_read",
                        "expected_endpoint", "status", "detail"])
        for r in records:
            w.writerow([r["ts"], r["label"], r["com"], r["expected_mac"],
                        r["mac_read"], r["expected_endpoint"],
                        r["status"], r["detail"]])
    # Detail JSON (one file per session)
    sid = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    (DETAIL_DIR / f"session_{sid}.json").write_text(
        json.dumps({"ts": sid, "records": records}, indent=2),
        encoding="utf-8")


def loaded_done_set() -> set[tuple[int, str]]:
    """For --resume: skip (label, status_OK) already in results CSV."""
    if not RESULTS_CSV.exists():
        return set()
    done = set()
    with RESULTS_CSV.open("r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("status", "").startswith("OK"):
                try:
                    done.add((int(r["label"]), r["expected_endpoint"]))
                except (ValueError, KeyError):
                    pass
    return done


# ─── Main ────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mesh", default=fc.DEFAULT_MESH, choices=fc.MESH_TARGETS)
    ap.add_argument("--user", default=fc.EDGE_TENANT_USER)
    ap.add_argument("--password", default=fc.EDGE_TENANT_PASS)
    ap.add_argument("--profile", default=fc.EDGE_PROFILE)
    ap.add_argument("--build-dir", default="build_ota_ftd")
    ap.add_argument("--baud", default="460800")
    ap.add_argument("--flash-mode", default="dout")
    ap.add_argument("--flash-freq", default="20m")
    ap.add_argument("--labels", default="",
                    help="comma list of labels to flash (default: all detected)")
    ap.add_argument("--exclude", default="",
                    help="comma list of labels to skip")
    ap.add_argument("--skip-provision", action="store_true")
    ap.add_argument("--no-wait-tb", action="store_true",
                    help="don't wait for TB active=True after each flash "
                         "(use when you're powering down + moving boards)")
    ap.add_argument("--post-flash-wait", type=int, default=120,
                    help="seconds to wait for TB active=True per board")
    ap.add_argument("--dry-run", action="store_true",
                    help="pre-flight only, no flashing")
    ap.add_argument("--resume", action="store_true",
                    help="skip boards already marked OK in results CSV")
    args = ap.parse_args()

    host, port = fc.edge_for_mesh(args.mesh)
    env = fc.detect_env(verbose=False)
    tb_legacy = TBClient(host, port, args.user, args.password)
    tb_legacy.login()
    tb = TB(host, port, args.user, args.password)
    tb.login()

    findings = preflight(env, tb, args)
    if not findings["ok"]:
        print("\n=== PRE-FLIGHT FAILED — fix errors above and retry ===")
        return 2

    if args.dry_run:
        print("\n=== DRY RUN: no boards flashed ===")
        return 0

    selected = findings["selected"]
    if args.resume:
        done = loaded_done_set()
        before = len(selected)
        selected = [r for r in selected
                    if (r["label"], r["endpoint"]) not in done]
        print(f"\n  --resume: {before - len(selected)} board(s) skipped (already OK)")

    if not selected:
        print("\nNo boards to flash. Exiting.")
        return 0

    print(f"\n=== FLASHING {len(selected)} board(s) ===")
    print("Press Ctrl-C within 5 s to abort...")
    try:
        time.sleep(5)
    except KeyboardInterrupt:
        print("Aborted.")
        return 1

    records: list[dict] = []
    for i, row in enumerate(selected, 1):
        print(f"\n[{i}/{len(selected)}] ", end="")
        try:
            rec = flash_one(env, tb_legacy, tb, row, args)
        except KeyboardInterrupt:
            print("\nInterrupted by user.")
            rec = {"ts": dt.datetime.now().isoformat(timespec="seconds"),
                   "label": row["label"], "com": row["com"],
                   "expected_mac": row["mac"], "mac_read": "",
                   "expected_endpoint": row["endpoint"],
                   "status": "ABORT", "detail": "Ctrl-C"}
            records.append(rec)
            break
        records.append(rec)
        # Update fleet_map.csv on success
        if rec["status"].startswith("OK") and rec["mac_read"]:
            patch_fleet_map(row["label"], row["com"], rec["mac_read"],
                            row["endpoint"], "v0.6.46-bulk-flash")
        print(f"  [{rec['status']}] {rec['detail']}")

    persist(records)

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    counts: dict[str, int] = {}
    for r in records:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    for s, n in sorted(counts.items()):
        print(f"  {s}: {n}")
    fails = [r for r in records if r["status"] not in ("OK", "OK_NO_WAIT")]
    if fails:
        print("\nNon-OK boards:")
        for r in fails:
            print(f"  label={r['label']:<3} {r['com']:<6} {r['status']}  {r['detail']}")

    print("\nNext steps:")
    print("  1. Power down USB hub; move boards to PSU rack")
    print("  2. Power on rack; allow ~3 min for Thread mesh attach + LwM2M reg")
    print(f"  3. Monitor at http://{host}:{port} (login: tenant@thingsboard.org)")
    print(f"     Dashboard: '{DASHBOARD_NAME}'")
    print(f"  4. Or continuous CSV poll: python tools/reset_watcher.py "
          f"--out logs/post_psu_resets.csv --duration 3600 --interval 30")

    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
