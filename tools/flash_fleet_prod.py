#!/usr/bin/env python3
"""Flash the whole fleet with the canonical fat-production firmware (build_prod).

build_prod = v0.7.5-prod: FTD + Object 33000 (RID 37) + CoAP block 64 + mesh
R1000 + Fix A (UPDATE_PERIOD=300) + heap stats. The validated 3-board config,
production-clean. Every connected board gets this exact build (both role slots
point at build_prod), so the OUI/alternate role logic is bypassed = all FTD.

Thin wrapper over tools/bulk_flash_minimal.py (proven: full MCUboot@0x0 +
app@0x20000 flash, UPPERCASE adapter serial, unique OpenOCD ports, 2 retries).

Usage:
    python tools/build_prod.py                      # (re)build first if needed
    python tools/flash_fleet_prod.py --dry-run      # show plan, flash nothing
    python tools/flash_fleet_prod.py                # flash all CONNECTED boards
    python tools/flash_fleet_prod.py --only 10:51:DB:1C:14:94   # one board

Workflow: connect boards to the PC/hub in batches (the PSU has no data lines),
flash, then move to the PSU for the soak. Run with all 30 connected, or in
batches — same result.

NOTE: the dry-run may label SuperMini as "SED" — cosmetic only; every board is
flashed build_prod (FTD/fat). Verify after with tools/verify_fleet.py.
"""
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
ZP = pathlib.Path.home() / "Documents" / "ESP32" / "zephyrproject"
APP = ZP / "build_prod" / "ami-lwm2m-node" / "zephyr" / "zephyr.signed.bin"

if not APP.exists():
    print(f"ERROR: build_prod artifact missing:\n  {APP}\n"
          f"Build it first:  python tools/build_prod.py")
    sys.exit(2)

cmd = [sys.executable, str(REPO / "tools" / "bulk_flash_minimal.py"),
       "--sed-build", "build_prod", "--ftd-build", "build_prod"] + sys.argv[1:]
print("=" * 64)
print(" Flashing fleet -> build_prod  (v0.7.5-prod, FAT production)")
print("   FTD | Object 33000 + RID 37 | block 64 | mesh R1000 | Fix A")
print("=" * 64, flush=True)
sys.exit(subprocess.call(cmd, cwd=str(REPO)))
