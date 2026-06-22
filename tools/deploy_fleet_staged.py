#!/usr/bin/env python3
"""Staged fleet OTA deployer — the SAFE way to roll firmware across the fleet.

LESSON (2026-06-22, baked in): blasting N OTAs back-to-back congests the mesh —
each OTA is a reboot + a large inbound block-download — and collateral nodes
drop off (Lab1/Lab6 fell during a 5-node blast). This deploys ONE node at a
time and lets the mesh SETTLE between nodes, keeping churn low. Re-runnable:
nodes already on the target version are skipped, so an interrupted deploy just
resumes where it left off.

Only works on nodes that are ON THE MESH (reachable). Off-mesh nodes (RPC 504,
telemetry stale) CANNOT be OTA'd — they need a USB flash / power-cycle first;
the script flags them and moves on.

Requires the robust-OTA firmware (>= 0.7.14-otacfm, confirm-on-Thread-attach)
already running on the targets OR being deployed — otherwise OTA can revert on a
congested mesh (see project_ota_rollback_at_scale_confirm_on_attach memory).

Usage:
  # update every ami node in TB to the sweet-spot build:
  python tools/deploy_fleet_staged.py --version 0.7.14-otacfm \
      --bin <ws>/build_prod/ami-lwm2m-node/zephyr/zephyr.signed.bin --all

  # explicit subset + custom settle:
  python tools/deploy_fleet_staged.py --version 0.7.14-otacfm --bin <...> \
      --devices ami-esp32c6-1494,ami-esp32c6-f7b4 --settle 120

  # dry-run: classify only (current / to-update / unreachable), no OTA:
  python tools/deploy_fleet_staged.py --version 0.7.14-otacfm --all --dry-run
"""
from __future__ import annotations
import argparse
import json
import subprocess
import sys
import time
import urllib.request
import urllib.error

import fleet_common as fc

fc.bootstrap_venv()

TB = "http://192.168.8.111:8090"
HERE = __import__("pathlib").Path(__file__).resolve().parent


def login():
    r = urllib.request.Request(
        TB + "/api/auth/login",
        data=json.dumps({"username": "tenant@thingsboard.org", "password": "tenant"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(r, timeout=15).read())["token"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True, help="target fw version, e.g. 0.7.14-otacfm")
    ap.add_argument("--bin", help="signed.bin path (required unless --dry-run)")
    ap.add_argument("--devices", help="comma list of device names; omit with --all")
    ap.add_argument("--all", action="store_true", help="target every ami-esp32c6-* device in TB")
    ap.add_argument("--settle", type=int, default=90, help="seconds to let the mesh settle between nodes")
    ap.add_argument("--dry-run", action="store_true", help="classify only, no OTA")
    args = ap.parse_args()
    if not args.dry_run and not args.bin:
        print("FATAL: --bin required (or use --dry-run)"); return 2

    tok = login()

    def g(p):
        return json.loads(urllib.request.urlopen(
            urllib.request.Request(TB + p, headers={"X-Authorization": f"Bearer {tok}"}), timeout=15).read())

    devmap = {d["name"]: d["id"]["id"] for d in g("/api/tenant/devices?pageSize=300&page=0")["data"]
              if d["name"].startswith("ami-esp32c6-")}
    if args.all:
        targets = sorted(devmap)
    else:
        targets = [d.strip() for d in (args.devices or "").split(",") if d.strip()]
    if not targets:
        print("FATAL: no targets (use --all or --devices)"); return 2

    now = lambda: int(time.time() * 1000)

    def fw_of(did):
        return next((a["value"] for a in g(f"/api/plugins/telemetry/DEVICE/{did}/values/attributes")
                     if a["key"] == "fw_version"), None)

    def streaming(did):
        x = g(f"/api/plugins/telemetry/DEVICE/{did}/values/timeseries?keys=uptime_s").get("uptime_s", [{}])[0]
        return bool(x.get("ts")) and (now() - x["ts"]) // 1000 < 150

    # classify
    todo, skip_cur, unreachable, missing = [], [], [], []
    for name in targets:
        did = devmap.get(name)
        if not did:
            missing.append(name); continue
        fw = fw_of(did)
        if fw == args.version:
            skip_cur.append(name)
        elif not streaming(did):
            unreachable.append(name)
        else:
            todo.append((name, did, fw))

    print(f"\n=== staged deploy plan -> {args.version} ===")
    print(f"  already current : {len(skip_cur)}")
    print(f"  TO UPDATE       : {len(todo)}  {[t[0] for t in todo]}")
    print(f"  UNREACHABLE     : {len(unreachable)} (off-mesh -> USB/power-cycle, can't OTA) {unreachable}")
    if missing:
        print(f"  not in TB       : {missing}")
    if args.dry_run:
        print("\n[dry-run] no OTA performed."); return 0
    if not todo:
        print("\nNothing to update."); return 0

    ok, failed = [], []
    for i, (name, did, oldfw) in enumerate(todo, 1):
        print(f"\n===== [{i}/{len(todo)}] {name}  ({oldfw} -> {args.version}) =====", flush=True)
        cmd = [sys.executable, str(HERE / "ota_push_direct.py"),
               "--device", name, "--version", args.version, "--bin", args.bin]
        r = subprocess.run(cmd)
        # verify the node landed on the target + is streaming
        landed = (fw_of(did) == args.version) and streaming(did)
        if r.returncode == 0 and landed:
            print(f"  OK {name} on {args.version}")
            ok.append(name)
        else:
            print(f"  FAIL {name} (rc={r.returncode} landed={landed}) — leaving on mesh, continuing")
            failed.append(name)
        if i < len(todo):
            print(f"  ...settling {args.settle}s before next node (mesh churn control)", flush=True)
            time.sleep(args.settle)

    print("\n" + "=" * 56)
    print(f"DEPLOY DONE: {len(ok)} updated, {len(failed)} failed, "
          f"{len(skip_cur)} already-current, {len(unreachable)} unreachable")
    if failed:
        print(f"  retry failed (re-run resumes): {failed}")
    if unreachable:
        print(f"  unreachable need USB/power-cycle: {unreachable}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
