"""Bulk-flash ALL currently-connected ESP32-C6 boards with v0.6.64 FTD build.

Workflow:
  1. Plug N boards into the hub (typically 5-7 at a time)
  2. python tools/bulk_flash_ftd_v064.py
  3. Script enumerates each connected COM, looks up label from fleet_map.csv,
     and calls flash_one.py to flash + provision on TB Edge sequentially.
  4. Unplug, plug next batch, re-run. CSV results accumulate.

Idempotent: skip-version logic is in flash_one.py itself. Always uses
build_ota_ftd (FTD = full Thread router, what fleet runs).

Output: tools/bulk_flash_v064_results.csv with one row per attempt.
"""
from __future__ import annotations

import csv
import datetime as dt
import pathlib
import subprocess
import sys
import time

import fleet_common as fc

fc.bootstrap_venv()

REPO = pathlib.Path(__file__).resolve().parent.parent
FLEET = REPO / "tools" / "fleet_map.csv"
RESULTS = REPO / "tools" / "bulk_flash_v064_results.csv"
FLASH_ONE = REPO / "tools" / "flash_one.py"


def load_fleet_map() -> dict[str, dict]:
    """Returns {com: {label, mac, endpoint}}"""
    if not FLEET.exists():
        return {}
    out = {}
    with open(FLEET, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[row["com"].upper()] = row
    return out


def main() -> int:
    fleet = load_fleet_map()
    ports = fc.list_esp32c6_ports()
    if not ports:
        print("No ESP32-C6 ports detected. Plug boards and retry.")
        return 1

    print(f"[bulk] {len(ports)} board(s) detected:")
    for p in ports:
        meta = fleet.get(p.com.upper(), {})
        lbl = meta.get("label", "?")
        ep = meta.get("endpoint", "unknown")
        print(f"  {p.com:8s}  label={lbl:>3s}  endpoint={ep}")

    new_csv = not RESULTS.exists()
    with open(RESULTS, "a", newline="", encoding="utf-8") as out:
        w = csv.writer(out)
        if new_csv:
            w.writerow(["ts", "com", "label", "endpoint", "status", "duration_s", "note"])
        ok_count = fail_count = 0
        for p in ports:
            meta = fleet.get(p.com.upper(), {})
            lbl = meta.get("label", "?")
            ep = meta.get("endpoint", "")
            print(f"\n[bulk] flashing {p.com} (label {lbl}) ...")
            cmd = [
                sys.executable,
                str(FLASH_ONE),
                "--com", p.com,
                "--build-dir", "build_ota_ftd",
                "--no-wait-tb",
                "--force",
            ]
            t0 = time.time()
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                dur = time.time() - t0
                ok = proc.returncode == 0
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()
                note = tail[-1] if tail else ""
            except subprocess.TimeoutExpired:
                dur = time.time() - t0
                ok = False
                note = "timeout 300s"
            except Exception as e:
                dur = time.time() - t0
                ok = False
                note = f"err: {e}"

            if ok:
                ok_count += 1
                print(f"  -> OK in {dur:.0f}s")
            else:
                fail_count += 1
                print(f"  -> FAIL in {dur:.0f}s :: {note[:160]}")

            w.writerow([
                dt.datetime.now().isoformat(timespec="seconds"),
                p.com, lbl, ep,
                "ok" if ok else "fail",
                f"{dur:.0f}",
                note[:200],
            ])
            out.flush()

    print(f"\n[bulk] done. {ok_count} OK, {fail_count} FAIL. Results -> {RESULTS}")
    return 0 if fail_count == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
