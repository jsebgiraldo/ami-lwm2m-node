#!/usr/bin/env python3
"""Flash a SUBSET of boards with the aggressive test build (build_aggr, v0.7.6-aggr).

For the aggressive-config experiment: flash a few boards (ideally the drip-prone
ones + a couple healthy), compare vs the v0.7.5-prod majority. Aggr boards report
fw=0.7.6-aggr in TB, so they're easy to single out in verify_fleet / TB.

Usage:
    python tools/flash_aggr.py 10:51:DB:1B:F7:88 10:51:DB:1B:F6:D4 ...   # by MAC
    python tools/flash_aggr.py --dry-run 10:51:DB:1B:F7:88
Boards must be USB-connected to the PC (move from PSU to flash, then back).
"""
import pathlib, subprocess, sys

REPO = pathlib.Path(__file__).resolve().parent.parent
ZP = pathlib.Path.home() / "Documents" / "ESP32" / "zephyrproject"
APP = ZP / "build_aggr" / "ami-lwm2m-node" / "zephyr" / "zephyr.signed.bin"

if not APP.exists():
    print(f"ERROR: build_aggr missing ({APP}). Run: python tools/build_aggr.py"); sys.exit(2)
args = sys.argv[1:]
macs = [a for a in args if not a.startswith('--')]
passthru = [a for a in args if a.startswith('--')]
if not macs:
    print("usage: python tools/flash_aggr.py [--dry-run] <mac> [mac ...]"); sys.exit(1)

cmd = [sys.executable, str(REPO/"tools"/"bulk_flash_minimal.py"),
       "--sed-build", "build_aggr", "--ftd-build", "build_aggr",
       "--only"] + macs + passthru
print("="*60)
print(f" Flashing {len(macs)} board(s) -> build_aggr (v0.7.6-aggr, AGGRESSIVE)")
print("="*60, flush=True)
sys.exit(subprocess.call(cmd, cwd=str(REPO)))
