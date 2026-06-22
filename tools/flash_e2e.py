#!/usr/bin/env python3
"""E2E: single-shot flash ONE board + validate it boots and reports in TB Edge.

Flashes with `write-flash --erase-all` (the single-transaction anti-wedge
recipe), captures the BASE MAC from esptool, derives the endpoint, then polls
TB Edge until the board reports FRESH telemetry on the expected fw_version
(= it actually rebooted into the new image and re-registered).

Usage:
  python tools/flash_e2e.py --com COM17
  python tools/flash_e2e.py --com COM17 --build-dir build_audit
  python tools/flash_e2e.py --com COM17 --before no-reset   # board already in download mode
  python tools/flash_e2e.py --com COM17 --expect-fw 0.7.9-dlv2 --tb-timeout 180
"""
from __future__ import annotations
import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request

import fleet_common as fc

fc.bootstrap_venv()
env = fc.detect_env(verbose=False)

TB = "http://192.168.8.111:8090"


def tb_login():
    r = urllib.request.Request(
        TB + "/api/auth/login",
        data=json.dumps({"username": "tenant@thingsboard.org",
                         "password": "tenant"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(r, timeout=15).read())["token"]


def tb_get(tok, path):
    return json.loads(urllib.request.urlopen(urllib.request.Request(
        TB + path, headers={"X-Authorization": f"Bearer {tok}"}), timeout=15).read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--com", required=True)
    ap.add_argument("--build-dir", default="build_audit")
    ap.add_argument("--baud", default="460800")
    ap.add_argument("--before", default="default-reset",
                    choices=["default-reset", "no-reset"],
                    help="no-reset if the board is already in download mode")
    ap.add_argument("--expect-fw", default="0.7.9-dlv2")
    ap.add_argument("--tb-timeout", type=int, default=180)
    args = ap.parse_args()

    ws = env.west_workspace
    mcu = ws / args.build_dir / "mcuboot" / "zephyr" / "zephyr.bin"
    app = ws / args.build_dir / "ami-lwm2m-node" / "zephyr" / "zephyr.signed.bin"
    for p in (mcu, app):
        if not p.exists():
            print(f"FATAL: artifact missing: {p}")
            return 2

    # ---- 1) flash (single-shot, captures MAC) ----
    print(f"[e2e] {args.com}: single-shot write-flash --erase-all ({args.build_dir})")
    cmd = [str(env.venv_python), "-m", "esptool", "--chip", "esp32c6",
           "--port", args.com, "--baud", args.baud,
           "--before", args.before, "--after", "hard-reset",
           "write-flash", "--erase-all", "--flash-freq", "20m",
           "--flash-mode", "dout", "0x0", str(mcu), "0x20000", str(app)]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=240,
                         env=env.env_for_subprocess())
    out = res.stdout + res.stderr
    mac_m = re.search(r"BASE MAC:\s*([0-9a-fA-F:]{17})", out)
    verified = out.count("Hash of data verified")
    if res.returncode != 0 or verified < 2:
        tail = "\n".join(out.splitlines()[-8:])
        print(f"[e2e] FLASH FAILED (rc={res.returncode} verified={verified})\n{tail}")
        if "not functioning" in out:
            print("  -> USB WEDGE: power-cycle (or BOOT+power-cycle for download "
                  "mode) and retry; try --before no-reset.")
        return 1
    mac = mac_m.group(1).lower()
    endpoint = fc.mac_to_endpoint(mac)
    print(f"[e2e] FLASH OK  mac={mac}  endpoint={endpoint}  (hash verified x{verified})")
    print(f"[e2e] hard-reset issued. If the board is wedge-prone it may need a "
          f"PHYSICAL power-cycle to boot.")

    # ---- 2) validate in TB ----
    print(f"[e2e] validating in TB (expect fw={args.expect_fw}, "
          f"fresh telemetry, up to {args.tb_timeout}s)...")
    tok = tb_login()
    devs = {d["name"]: d["id"]["id"]
            for d in tb_get(tok, "/api/tenant/devices?pageSize=300&page=0")["data"]}
    did = devs.get(endpoint)
    if not did:
        print(f"[e2e] WARN: {endpoint} not found in TB (not provisioned?)")
        return 1
    deadline = time.time() + args.tb_timeout
    last = ""
    while time.time() < deadline:
        now = int(time.time() * 1000)
        ts = tb_get(tok, f"/api/plugins/telemetry/DEVICE/{did}"
                         f"/values/timeseries?keys=uptime_s")
        up = ts.get("uptime_s", [{}])[0]
        age = (now - up["ts"]) // 1000 if up.get("ts") else None
        fw = None
        for a in tb_get(tok, f"/api/plugins/telemetry/DEVICE/{did}/values/attributes"):
            if a["key"] == "fw_version":
                fw = a["value"]
        state = f"fw={fw} uptime={up.get('value')} age={age}s"
        if state != last:
            print(f"  [tb] {state}")
            last = state
        if fw == args.expect_fw and age is not None and age < 120:
            print(f"\n[e2e] PASS ✓  {endpoint} booted {fw}, streaming "
                  f"(uptime={up.get('value')}s, age={age}s)")
            return 0
        time.sleep(5)
    print(f"\n[e2e] FAIL: flashed OK but {endpoint} did not report fresh {args.expect_fw} "
          f"within {args.tb_timeout}s.\n  -> Likely needs a PHYSICAL power-cycle to "
          f"boot the new image (RTS hard-reset doesn't boot wedge-prone boards).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
