#!/usr/bin/env python3
"""Parallel FULL-ERASE reflash of all connected ESP32-C6 boards (esptool path).

Why this and not bulk_flash_minimal: bulk_flash uses JTAG `program_esp` which
writes mcuboot@0x0 + app@0x20000 but does NOT erase NVS. Boards with CORRUPTED
NVS (from heavy reboot churn) report "already-flashed" yet stay wedged (can't
attach). Only a full `esptool erase_flash` clears the corruption. This wraps
flash_one.py (which does erase_flash + write) and runs it across all connected
COM ports in parallel (default 5-wide to avoid USB-hub contention).

Usage:  python tools/flash_erase_parallel.py [--jobs 5] [--build-dir build_prod]
        python tools/flash_erase_parallel.py --only COM19 COM33
"""
import argparse, csv, re, subprocess, sys, pathlib
from concurrent.futures import ThreadPoolExecutor
import serial.tools.list_ports as lp

REPO = pathlib.Path(__file__).resolve().parent.parent
FLEET_MAP = REPO / "tools" / "fleet_map.csv"


def labels():
    out = {}
    if FLEET_MAP.exists():
        with FLEET_MAP.open() as f:
            for r in csv.DictReader(f):
                mac = (r.get("mac") or "").strip().upper()
                if mac:
                    out[mac] = r.get("label")
    return out


def connected():
    rows = []
    for p in lp.comports():
        if "303A" in (p.hwid or "").upper():
            rows.append((p.device, (p.serial_number or "").upper()))
    return sorted(rows, key=lambda x: int("".join(c for c in x[0] if c.isdigit()) or 0))


def flash(com, build_dir, wait):
    cmd = [sys.executable, str(REPO / "tools" / "flash_one.py"), "--com", com,
           "--build-dir", build_dir, "--skip-provision", "--no-wait-tb",
           "--post-flash-wait", str(wait)]
    try:
        r = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True, timeout=180)
        out = r.stdout + r.stderr
        if "RESULT: OK" in out:
            return "OK"
        m = re.search(r"(device attached to the system is not functioning|Cannot configure port|Write timeout|No serial data|Failed to connect)", out)
        return "USB-WEDGED" if m else "FAIL"
    except subprocess.TimeoutExpired:
        return "TIMEOUT"
    except Exception as e:
        return f"ERR:{type(e).__name__}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=5)
    ap.add_argument("--build-dir", default="build_prod")
    ap.add_argument("--only", nargs="*", default=[])
    a = ap.parse_args()
    L = labels()
    boards = connected()
    if a.only:
        sel = {c.upper() for c in a.only}
        boards = [b for b in boards if b[0].upper() in sel]
    if not boards:
        print("No ESP32-C6 boards connected."); return 1
    print(f"FULL-ERASE reflash {len(boards)} boards, {a.jobs}-wide ({a.build_dir}):")
    for com, ser in boards:
        print(f"  {com:7} Lab {L.get(ser,'?'):>3}  {ser}")
    print(flush=True)

    def work(b):
        com, ser = b
        res = flash(com, a.build_dir, 3)
        print(f"  {com:7} Lab {L.get(ser,'?'):>3}  {ser}  -> {res}", flush=True)
        return res

    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        results = list(ex.map(work, boards))
    ok = sum(1 for r in results if r == "OK")
    wedged = [boards[i][0] for i, r in enumerate(results) if r == "USB-WEDGED"]
    print(f"\nResult: {ok}/{len(boards)} OK")
    if wedged:
        print(f"USB-wedged (replug into a different port + retry): {wedged}")
    return 0 if ok == len(boards) else 2


if __name__ == "__main__":
    sys.exit(main())
