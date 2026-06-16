"""Flash, provision, and record ONE board at a time.

Designed for the workflow "plug a board, run this, unplug, plug the next".
Auto-detects which COM is connected, looks up the label in fleet_map.csv,
flashes v0.6.46 build_ota_ftd, provisions it on TB Edge, optionally waits
for TB to report active=True, persists the result to the same CSV that
tools/bulk_flash_v046.py writes so you get one consolidated log.

Usage:
    # auto-pick the only connected ESP32-C6 COM:
    python tools/flash_one.py

    # explicit:
    python tools/flash_one.py --com COM27

    # don't wait for TB registration (when you're about to move boards):
    python tools/flash_one.py --com COM27 --no-wait-tb

    # only flash, no TB provision (board already registered):
    python tools/flash_one.py --com COM27 --skip-provision

Safety:
  * If fleet_map.csv lists this MAC with HW-DEFECTIVE-* in the source column,
    the script REFUSES to flash unless --force is passed. Stops you from
    redeploying known-bad boards.
  * If the chip MAC does not match the MAC fleet_map.csv has for this COM,
    you get a WARN_MAC and the script keeps going using the chip's actual
    endpoint (so we always provision the right device).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
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
SKIP_TAGS = ("HW-DEFECTIVE",)


def detect_coms() -> list[str]:
    """Return ['COM24', 'COM25', ...] for all USB serial ports right now."""
    ps = (
        "Get-WMIObject Win32_PnPEntity -Filter \"DeviceID like '%USB%'\" | "
        "Where-Object {$_.Name -match 'COM'} | Select Name"
    )
    out = subprocess.check_output(
        ["powershell", "-NoProfile", "-Command", ps], text=True, timeout=12)
    coms = sorted(set(re.findall(r"\(COM\d+\)", out)))
    return [c.strip("()") for c in coms]


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


def find_row_by_com(fleet: list[dict], com: str) -> dict | None:
    for r in fleet:
        if r["com"] == com:
            return r
    return None


def find_row_by_mac(fleet: list[dict], mac: str) -> dict | None:
    target = mac.lower()
    for r in fleet:
        if r["mac"].lower() == target:
            return r
    return None


def patch_fleet_map(label: int, com: str, mac: str, endpoint: str,
                    tag: str) -> None:
    """Update the row for `label` with current com/mac/endpoint and a tag.
    Preserves any pre-existing HW-DEFECTIVE-* tag (never overwritten)."""
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
                if not (len(r) >= 5 and r[4].startswith(SKIP_TAGS)):
                    r[4] = tag
        except (ValueError, IndexError):
            pass
        out.append(r)
    with FLEET_MAP_CSV.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(out)


def tb_login(host: str, port: int, user: str, password: str) -> str:
    req = urllib.request.Request(
        f"http://{host}:{port}/api/auth/login",
        data=json.dumps({"username": user, "password": password}).encode(),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())["token"]


def tb_active(host: str, port: int, token: str, endpoint: str) -> bool:
    try:
        req = urllib.request.Request(
            f"http://{host}:{port}/api/tenant/devices?deviceName={endpoint}",
            headers={"X-Authorization": f"Bearer {token}"})
        dev = json.loads(urllib.request.urlopen(req, timeout=10).read())
        did = dev["id"]["id"]
        req2 = urllib.request.Request(
            f"http://{host}:{port}/api/plugins/telemetry/DEVICE/{did}"
            f"/values/attributes/SERVER_SCOPE",
            headers={"X-Authorization": f"Bearer {token}"})
        attrs = json.loads(urllib.request.urlopen(req2, timeout=10).read())
        a = {x["key"]: x["value"] for x in attrs}
        return bool(a.get("active"))
    except Exception:
        return False


def persist(rec: dict) -> None:
    DETAIL_DIR.mkdir(parents=True, exist_ok=True)
    new_file = not RESULTS_CSV.exists()
    with RESULTS_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["ts", "label", "com", "expected_mac", "mac_read",
                        "expected_endpoint", "status", "detail"])
        w.writerow([rec["ts"], rec["label"], rec["com"], rec["expected_mac"],
                    rec["mac_read"], rec["expected_endpoint"],
                    rec["status"], rec["detail"]])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--com", default=None,
                    help="COM port (default: auto-pick if exactly one detected)")
    ap.add_argument("--mesh", default=fc.DEFAULT_MESH, choices=fc.MESH_TARGETS)
    ap.add_argument("--user", default=fc.EDGE_TENANT_USER)
    ap.add_argument("--password", default=fc.EDGE_TENANT_PASS)
    ap.add_argument("--profile", default=fc.EDGE_PROFILE)
    ap.add_argument("--build-dir", default="build_ota_ftd")
    ap.add_argument("--baud", default="460800")
    ap.add_argument("--flash-mode", default="dout")
    ap.add_argument("--flash-freq", default="20m")
    ap.add_argument("--skip-provision", action="store_true",
                    help="skip TB device provisioning")
    ap.add_argument("--no-wait-tb", action="store_true",
                    help="don't wait for TB active=True after flash")
    ap.add_argument("--post-flash-wait", type=int, default=120,
                    help="seconds to wait for TB active=True")
    ap.add_argument("--force", action="store_true",
                    help="flash even if fleet_map marks this board HW-DEFECTIVE")
    args = ap.parse_args()

    # 1. Resolve COM
    coms = detect_coms()
    if args.com:
        if args.com not in coms:
            print(f"ERROR: {args.com} not detected. Connected COMs: {coms}")
            return 2
        com = args.com
    else:
        if not coms:
            print("ERROR: no COMs detected.")
            return 2
        if len(coms) == 1:
            com = coms[0]
            print(f"  auto-picked single connected port: {com}")
        else:
            print(f"ERROR: multiple COMs connected, specify --com from: {coms}")
            return 2

    # 2. Read MAC and lookup
    env = fc.detect_env(verbose=False)
    try:
        mac = fc.read_mac(env, com)
    except Exception as e:
        print(f"ERROR reading MAC on {com}: {e}")
        return 2
    endpoint_from_chip = fc.mac_to_endpoint(mac)

    fleet = load_fleet_map()
    row_by_com = find_row_by_com(fleet, com)
    row_by_mac = find_row_by_mac(fleet, mac)

    row = row_by_mac or row_by_com  # prefer MAC match
    label = row["label"] if row else None
    expected_endpoint = row["endpoint"] if row else endpoint_from_chip
    expected_mac = row["mac"] if row else mac

    print(f"\n=== flash_one ===")
    print(f"  COM           : {com}")
    print(f"  MAC (chip)    : {mac}")
    print(f"  endpoint      : {endpoint_from_chip}")
    if row:
        print(f"  label (map)   : {label}")
        if row["mac"].lower() != mac.lower():
            print(f"  ! MAC mismatch: map says {row['mac']} for COM {row['com']}")
    else:
        print(f"  label (map)   : NOT FOUND for COM {com} or MAC {mac}")

    # 3. Refuse if HW-defective unless --force
    if row and any(row.get("source", "").startswith(t) for t in SKIP_TAGS):
        tag = row.get("source", "")
        if not args.force:
            print(f"\nREFUSING: this board is tagged {tag!r} in fleet_map.csv")
            print("  pass --force to override, but the bug is likely hardware.")
            rec = {"ts": dt.datetime.now().isoformat(timespec="seconds"),
                   "label": label, "com": com, "expected_mac": expected_mac,
                   "mac_read": mac, "expected_endpoint": expected_endpoint,
                   "status": "SKIP_DEFECTIVE", "detail": tag}
            persist(rec)
            return 1
        print(f"\n  --force in effect; flashing despite {tag!r}")

    # 4. Flash
    print("\n--- flash ---")
    try:
        flash_ota(env, com, args.baud, args.build_dir,
                  args.flash_mode, args.flash_freq)
    except Exception as e:
        rec = {"ts": dt.datetime.now().isoformat(timespec="seconds"),
               "label": label, "com": com, "expected_mac": expected_mac,
               "mac_read": mac, "expected_endpoint": expected_endpoint,
               "status": "FAIL", "detail": f"flash: {e}"}
        persist(rec)
        print(f"FAIL: {e}")
        return 2

    # 5. Provision
    host, port = fc.edge_for_mesh(args.mesh)
    if not args.skip_provision:
        print("\n--- provision ---")
        tb_legacy = TBClient(host, port, args.user, args.password)
        try:
            tb_legacy.login()
            provision_single(tb_legacy, endpoint_from_chip, args.profile,
                             dry_run=False)
        except Exception as e:
            rec = {"ts": dt.datetime.now().isoformat(timespec="seconds"),
                   "label": label, "com": com, "expected_mac": expected_mac,
                   "mac_read": mac, "expected_endpoint": expected_endpoint,
                   "status": "FAIL", "detail": f"provision: {e}"}
            persist(rec)
            print(f"FAIL: {e}")
            return 2

    # 6. Post-flash reset (best-effort)
    try:
        fc.hard_reset(com, label="post-flash-one")
    except Exception:
        pass
    time.sleep(6)

    # 7. Verify TB activity (optional)
    if args.no_wait_tb:
        rec = {"ts": dt.datetime.now().isoformat(timespec="seconds"),
               "label": label, "com": com, "expected_mac": expected_mac,
               "mac_read": mac, "expected_endpoint": endpoint_from_chip,
               "status": "OK_NO_WAIT", "detail": "flashed (TB verify skipped)"}
    else:
        print(f"\n--- waiting up to {args.post_flash_wait}s for TB active=True ---")
        token = tb_login(host, port, args.user, args.password)
        deadline = time.time() + args.post_flash_wait
        ok = False
        while time.time() < deadline:
            if tb_active(host, port, token, endpoint_from_chip):
                ok = True
                break
            time.sleep(5)
        if ok:
            rec = {"ts": dt.datetime.now().isoformat(timespec="seconds"),
                   "label": label, "com": com, "expected_mac": expected_mac,
                   "mac_read": mac, "expected_endpoint": endpoint_from_chip,
                   "status": "OK", "detail": "active in TB"}
        else:
            rec = {"ts": dt.datetime.now().isoformat(timespec="seconds"),
                   "label": label, "com": com, "expected_mac": expected_mac,
                   "mac_read": mac, "expected_endpoint": endpoint_from_chip,
                   "status": "WARN_NO_REG",
                   "detail": f"flashed but no TB activity within {args.post_flash_wait}s"}

    persist(rec)

    # 8. Update fleet_map.csv if we have a label
    if label is not None and rec["status"].startswith("OK"):
        patch_fleet_map(label, com, mac, endpoint_from_chip,
                        "v0.6.46-flash-one")

    print("\n" + "=" * 50)
    print(f"  RESULT: {rec['status']}  ({rec['detail']})")
    print(f"  endpoint: {endpoint_from_chip}")
    if label is not None:
        print(f"  label:    {label}")
    print("=" * 50)
    return 0 if rec["status"].startswith("OK") else 1


if __name__ == "__main__":
    sys.exit(main())
