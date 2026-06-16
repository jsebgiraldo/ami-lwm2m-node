"""Bulk-flash the v0.7.0-minimal production firmware over USB-JTAG.

Role assignment by MAC OUI (overridable with --ftd / --sed lists):
  10:51:DB:*  -> SuperMini  -> build_minimal      (SED, 60s poll, block 64)
  everything else (XIAO 98:A3:16, WROOM 58:8C:81, ...) -> build_minimal_ftd

Per board: program MCUboot @0x0 + signed app @0x20000, reset, log result.
Boards must be USB-connected to this PC (PSU has no data lines — flash in
batches on the PC/hub, then move to the PSU for the soak).

Usage:
    python tools/bulk_flash_minimal.py                # flash all enumerated
    python tools/bulk_flash_minimal.py --dry-run      # show plan only
    python tools/bulk_flash_minimal.py --ftd AA:BB:.. # force a MAC to FTD
    python tools/bulk_flash_minimal.py --only 10:51:DB:1C:14:94

Experiment mode (2026-06-10 SED-vs-FTD on SuperMini at block 256):
    python tools/bulk_flash_minimal.py --alternate \
        --sed-build build_sed256 --ftd-build build_ftd256
  --alternate ignores the OUI rule and assigns SED/FTD by the board's index
  in the SORTED list of all fleet_map.csv MACs (even index -> SED, odd ->
  FTD). Deterministic, stable across batches/re-runs, and splits the fleet
  evenly (16/16 over 32) while interleaving any manufacturing-batch
  correlation along MAC order. Boards not present in fleet_map.csv fall
  back to the OUI rule.

Gotchas handled:
  * adapter serial must be UPPERCASE (libusb compares case-sensitively)
  * unique OpenOCD ports per attempt so a hung previous instance can't block
  * 2 retries per board (USB-JTAG endpoints wedge transiently)
  * results CSV appended to tools/bulk_flash_results.csv
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import pathlib
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
OPENOCD = REPO / "tools/openocd-esp32/openocd-esp32/bin/openocd.exe"
SCRIPTS = REPO / "tools/openocd-esp32/openocd-esp32/share/openocd/scripts"
ZP = pathlib.Path.home() / "Documents" / "ESP32" / "zephyrproject"

BUILD_SED = ZP / "build_minimal"
BUILD_FTD = ZP / "build_minimal_ftd"

RESULTS_CSV = REPO / "tools" / "bulk_flash_results.csv"
FLEET_MAP = REPO / "tools" / "fleet_map.csv"

SUPERMINI_OUI = "10:51:DB"


def load_alternate_assignment() -> dict[str, str]:
    """SED/FTD per MAC: sorted fleet_map MACs, even index -> SED, odd -> FTD."""
    if not FLEET_MAP.exists():
        return {}
    macs = []
    with FLEET_MAP.open("r", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            mac = (row.get("mac") or "").strip().upper()
            if mac and len(mac.split(":")) == 6:
                macs.append(mac)
    return {m: ("SED" if i % 2 == 0 else "FTD") for i, m in enumerate(sorted(set(macs)))}


def enumerate_boards() -> list[str]:
    import usb.core
    import usb.util
    macs = []
    for d in usb.core.find(idVendor=0x303A, find_all=True):
        try:
            s = usb.util.get_string(d, d.iSerialNumber)
            if s and len(s.split(":")) == 6:
                macs.append(s.upper())
        except Exception:
            pass
    return sorted(set(macs))


def flash_one(mac: str, build_dir: pathlib.Path, port_ofs: int) -> tuple[bool, str, float]:
    mcuboot = build_dir / "mcuboot" / "zephyr" / "zephyr.bin"
    app = build_dir / "ami-lwm2m-node" / "zephyr" / "zephyr.signed.bin"
    for p in (mcuboot, app):
        if not p.exists():
            return False, f"missing {p}", 0.0
    args = [
        str(OPENOCD), "-s", str(SCRIPTS),
        "-c", f"adapter serial {mac.upper()}",
        "-c", f"gdb port {20000 + port_ofs}",
        "-c", f"telnet port {21000 + port_ofs}",
        "-c", f"tcl port {22000 + port_ofs}",
        "-f", "board/esp32c6-builtin.cfg",
        "-c", "init",
        "-c", f"program_esp {mcuboot.as_posix()} 0x0",
        "-c", f"program_esp {app.as_posix()} 0x20000 reset",
        "-c", "shutdown",
    ]
    t0 = time.time()
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return False, "timeout", time.time() - t0
    text = r.stdout + r.stderr
    elapsed = time.time() - t0
    if text.count("Programming Finished") >= 2:
        return True, "OK", elapsed
    if "could not find or open device" in text:
        return False, "USB_FAIL", elapsed
    if "Could not identify target" in text:
        return False, "TARGET_FAIL", elapsed
    return False, "UNKNOWN", elapsed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ftd", nargs="*", default=[], help="force these MACs to the FTD build")
    ap.add_argument("--sed", nargs="*", default=[], help="force these MACs to the SED build")
    ap.add_argument("--only", nargs="*", default=[], help="flash only these MACs")
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--alternate", action="store_true",
                    help="experiment mode: assign SED/FTD by MAC last-byte parity "
                         "(even=SED, odd=FTD) instead of by OUI")
    ap.add_argument("--sed-build", default=None,
                    help="build dir name under zephyrproject for the SED image")
    ap.add_argument("--ftd-build", default=None,
                    help="build dir name under zephyrproject for the FTD image")
    args = ap.parse_args()

    build_sed = ZP / args.sed_build if args.sed_build else BUILD_SED
    build_ftd = ZP / args.ftd_build if args.ftd_build else BUILD_FTD

    force_ftd = {m.upper() for m in args.ftd}
    force_sed = {m.upper() for m in args.sed}
    only = {m.upper() for m in args.only}

    macs = enumerate_boards()
    if only:
        macs = [m for m in macs if m in only]
    if not macs:
        print("No ESP32-C6 boards enumerated on USB. Connect a batch and retry.")
        return 1

    alt_map = load_alternate_assignment() if args.alternate else {}
    plan = []
    for m in macs:
        if m in force_ftd:
            role = "FTD"
        elif m in force_sed:
            role = "SED"
        elif args.alternate and m in alt_map:
            role = alt_map[m]
        elif m.startswith(SUPERMINI_OUI):
            role = "SED"
        else:
            role = "FTD"
        plan.append((m, role))

    print(f"Plan ({len(plan)} boards):")
    for m, role in plan:
        print(f"  {m}  ->  {role}  ({build_sed.name if role == 'SED' else build_ftd.name})")
    if args.dry_run:
        return 0

    new_csv = not RESULTS_CSV.exists()
    fcsv = open(RESULTS_CSV, "a", newline="", encoding="utf-8")
    w = csv.writer(fcsv)
    if new_csv:
        w.writerow(["ts", "mac", "role", "result", "elapsed_s", "fw"])

    ok = 0
    for i, (mac, role) in enumerate(plan):
        build = build_sed if role == "SED" else build_ftd
        note = ""
        success = False
        for attempt in range(1 + args.retries):
            success, note, elapsed = flash_one(mac, build, port_ofs=i * 3 + attempt)
            if success:
                break
            time.sleep(5)
        status = "OK " if success else "FAIL"
        print(f"  [{i+1}/{len(plan)}] {mac} {role}: {status} ({note}) {elapsed:.1f}s")
        w.writerow([dt.datetime.now().isoformat(timespec="seconds"), mac, f"{role}:{build.name}",
                    note, f"{elapsed:.1f}", "0.7.0-exp256"])
        fcsv.flush()
        if success:
            ok += 1

    fcsv.close()
    print(f"\nResult: {ok}/{len(plan)} OK")
    return 0 if ok == len(plan) else 2


if __name__ == "__main__":
    sys.exit(main())
