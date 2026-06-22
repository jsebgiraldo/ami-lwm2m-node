#!/usr/bin/env python3
"""Sequential single-shot fleet flasher (anti-wedge).

Flashes each connected ESP32-C6 ONE AT A TIME with `write-flash --erase-all`
(the single-transaction recipe that beats the USB 'device not functioning'
wedge). Sequential BY DESIGN: parallel flashing cascades the wedge across the
hub. Captures the BASE MAC from esptool output and maps it to the fleet_map
label so the summary is human-readable.

Usage:
  python tools/flash_fleet_seq.py --coms COM17,COM18,...        # explicit list
  python tools/flash_fleet_seq.py --coms COM17,COM18 --build-dir build_prod
Default build dir = build_audit (THREAD_ANALYZER + verified v0.7.9-dlv2 fix).
"""
from __future__ import annotations
import argparse
import csv
import pathlib
import re
import subprocess
import sys

import fleet_common as fc

fc.bootstrap_venv()
env = fc.detect_env(verbose=False)

FLEET_MAP = pathlib.Path("tools/fleet_map.csv")


def load_mac_to_label() -> dict:
    m = {}
    if FLEET_MAP.exists():
        with FLEET_MAP.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                m[r["mac"].lower()] = r["label"]
    return m


def flash_one(py: str, com: str, mcu: str, app: str, baud: str) -> tuple[bool, str]:
    cmd = [py, "-m", "esptool", "--chip", "esp32c6", "--port", com,
           "--baud", baud, "--before", "default-reset", "--after", "hard-reset",
           "write-flash", "--erase-all", "--flash-freq", "20m",
           "--flash-mode", "dout", "0x0", mcu, "0x20000", app]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                             env=env.env_for_subprocess())
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"
    out = res.stdout + res.stderr
    mac_m = re.search(r"BASE MAC:\s*([0-9a-fA-F:]{17})", out)
    mac = mac_m.group(1).lower() if mac_m else "?"
    verified = out.count("Hash of data verified")
    ok = (res.returncode == 0 and verified >= 2)
    if not ok:
        # short reason
        if "not functioning" in out:
            return False, f"WEDGE (mac={mac})"
        if "Could not open" in out or "FileNotFoundError" in out:
            return False, f"PORT-GONE (mac={mac})"
        if "No serial data" in out or "Failed to connect" in out:
            return False, f"NO-CONNECT (download-mode? mac={mac})"
        return False, f"rc={res.returncode} verified={verified} (mac={mac})"
    return True, mac


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coms", required=True, help="comma list, e.g. COM17,COM18")
    ap.add_argument("--build-dir", default="build_audit")
    ap.add_argument("--baud", default="460800")
    args = ap.parse_args()

    ws = env.west_workspace
    mcu = ws / args.build_dir / "mcuboot" / "zephyr" / "zephyr.bin"
    app = ws / args.build_dir / "ami-lwm2m-node" / "zephyr" / "zephyr.signed.bin"
    for p in (mcu, app):
        if not p.exists():
            print(f"FATAL: artifact missing: {p}")
            return 2

    coms = [c.strip() for c in args.coms.split(",") if c.strip()]
    mac2label = load_mac_to_label()
    print(f"[fleet-flash] build={args.build_dir}  boards={len(coms)}  SEQUENTIAL")
    print(f"  mcuboot={mcu.stat().st_size}B  app={app.stat().st_size}B\n")

    results = []
    for i, com in enumerate(coms, 1):
        print(f"=== [{i}/{len(coms)}] {com} ===", flush=True)
        ok, info = flash_one(str(env.venv_python), com, str(mcu), str(app), args.baud)
        if ok:
            label = mac2label.get(info, "?")
            print(f"  OK  Lab{label}  mac={info}", flush=True)
            results.append((com, f"Lab{label}", info, "OK"))
        else:
            print(f"  FAIL  {info}", flush=True)
            results.append((com, "?", "", f"FAIL {info}"))

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    okc = sum(1 for r in results if r[3] == "OK")
    for com, lab, mac, st in results:
        print(f"  {com:6} {lab:7} {mac:18} {st}")
    print(f"\n{okc}/{len(results)} flashed OK")
    fails = [r[0] for r in results if r[3] != "OK"]
    if fails:
        print(f"FAILED (retry: download-mode + rerun): {', '.join(fails)}")
    return 0 if okc == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
