#!/usr/bin/env python3
"""Provision NEW boards (labels 31+) onto TB Edge + flash + append to fleet_map.

Workflow (per ronda):
  1. Detect visible COM ports (excluding known phantoms COM4/COM48/etc).
  2. For each COM in ascending order:
     a. Assign next available label starting at (max label in fleet_map + 1).
     b. Cohort by parity: odd=MTD/A, even=FTD/B.
     c. Pick build_mtd or build_ota_ftd accordingly.
     d. Call flash_one.py --com COMx --build-dir <build>.
        flash_one auto-discovers the chip MAC, provisions the endpoint on
        TB Edge, and cold-boots the node.
     e. On success: append a new row to fleet_map.csv:
            label,com,mac,endpoint,source
        with source = "v0.6.56-provision-new".
  3. After all COMs done: regenerate bench_assignment.csv with the full
     extended fleet (old labels 1-30 + new labels 31+).

Phantom COMs (system COMs not boards): COM4, COM48, COM55, COM64, COM68.
These are detected via `detect_com_phantoms()` and excluded automatically.

Usage:
    python tools/bench_provision_new.py            # process all visible new COMs
    python tools/bench_provision_new.py --dry-run  # preview without flashing
"""
from __future__ import annotations
import argparse, csv, subprocess, sys, time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
FLEET = REPO / "tools" / "fleet_map.csv"
RESULTS = REPO / "tools" / "bench_provision_new_results.csv"

BUILD_MTD = "build_mtd"
BUILD_FTD = "build_ota_ftd"
SOURCE_TAG = "v0.6.56-provision-new"
PHANTOM_COMS = {"COM4", "COM48", "COM55", "COM64", "COM68"}


def detect_coms() -> list[str]:
    """Returns list of CDC-like COM ports excluding phantoms."""
    import subprocess
    cmd = ['powershell', '-Command',
           "Get-CimInstance -ClassName Win32_PnPEntity | "
           "Where-Object { $_.Caption -match 'COM\\d+' -and $_.Caption -notmatch 'COM1\\b' } | "
           "Select-Object Caption | ForEach-Object "
           "{ if ($_.Caption -match '(COM\\d+)') { $matches[1] } } | "
           "Sort-Object { [int]($_ -replace 'COM') } | Get-Unique"]
    out = subprocess.check_output(cmd, text=True, timeout=10)
    coms = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("COM")]
    return [c for c in coms if c not in PHANTOM_COMS]


def load_fleet():
    if not FLEET.exists(): return []
    with open(FLEET, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def next_label(rows) -> int:
    labels = [int(r["label"]) for r in rows if r["label"].isdigit()]
    return (max(labels) if labels else 0) + 1


def append_fleet_row(label: int, com: str, mac: str, endpoint: str, source: str):
    """Append a row to fleet_map.csv (preserve existing CRLF line endings)."""
    line = f"{label},{com},{mac},{endpoint},{source}\n"
    with open(FLEET, "a", encoding="utf-8", newline="") as f:
        f.write(line)


def regenerate_bench_assignment():
    """Rewrite bench_assignment.csv from fleet_map with parity cohort split."""
    rows = load_fleet()
    out_path = REPO / "tools" / "bench_assignment.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["label","com","mac","endpoint","cohort","variant","binary"])
        for r in rows:
            if not r["label"].isdigit(): continue
            lab = int(r["label"])
            cohort = "A" if lab % 2 == 1 else "B"
            variant = "MTD" if cohort == "A" else "FTD"
            binp = ("C:/Users/jsgir/Documents/ESP32/zephyrproject/build_mtd/ami-lwm2m-node/zephyr/zephyr.bin"
                    if cohort == "A" else
                    "C:/Users/jsgir/Documents/ESP32/zephyrproject/build_ota_ftd/ami-lwm2m-node/zephyr/zephyr.bin")
            w.writerow([lab, r["com"], r["mac"], r["endpoint"], cohort, variant, binp])
    print(f"[regen] bench_assignment.csv refreshed ({len(rows)} entries)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    coms = detect_coms()
    print(f"[detect] {len(coms)} CDC COMs visible: {', '.join(coms)}")
    if not coms:
        print("[err] no COMs visible — connect boards to hub first"); return 1

    fleet = load_fleet()
    existing_macs = {r["mac"].lower() for r in fleet if r["mac"]}
    existing_coms = {r["com"] for r in fleet if r["com"]}
    start_lab = next_label(fleet)
    print(f"[plan] next label = {start_lab}, will assign {start_lab}..{start_lab+len(coms)-1}")

    new_csv = not RESULTS.exists()
    out = open(RESULTS, "a", newline="", encoding="utf-8")
    w = csv.writer(out)
    if new_csv:
        w.writerow(["ts","label","com","mac","endpoint","cohort","variant","status","note"])

    cur_lab = start_lab
    for com in coms:
        cohort = "A" if cur_lab % 2 == 1 else "B"
        variant = "MTD" if cohort == "A" else "FTD"
        bdir = BUILD_MTD if cohort == "A" else BUILD_FTD
        print(f"\n[plan] {com} -> label={cur_lab} cohort={cohort}/{variant} build={bdir}")
        if a.dry_run:
            print("  DRY-RUN: skipping flash")
            cur_lab += 1; continue

        cmd = ["python", "tools/flash_one.py", "--com", com,
               "--build-dir", bdir, "--no-wait-tb"]
        t0 = time.time()
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        dt = int(time.time() - t0)
        ok = proc.returncode == 0
        # Extract MAC + endpoint from flash_one stdout
        mac = ""
        endpoint = ""
        for ln in proc.stdout.splitlines():
            ln = ln.strip()
            if ln.startswith("MAC (chip)"): mac = ln.split(":",1)[1].strip()
            elif ln.startswith("endpoint :") or ln.startswith("endpoint      :"):
                endpoint = ln.split(":",1)[1].strip()
            elif "endpoint:" in ln.lower() and not endpoint:
                endpoint = ln.split(":",1)[1].strip()
        if not ok:
            note = (proc.stderr or proc.stdout).strip().splitlines()[-1] if (proc.stderr or proc.stdout) else "fail"
            print(f"  -> FAIL in {dt}s ({note[:120]})")
            w.writerow([time.strftime("%FT%T"), cur_lab, com, mac, endpoint, cohort, variant, "fail", note[:200]])
            out.flush()
            # don't increment label on fail; next COM gets same label
            continue

        if mac.lower() in existing_macs:
            print(f"  -> MAC {mac} already in fleet_map — skipping append")
            note = f"mac_exists: {mac}"
            w.writerow([time.strftime("%FT%T"), cur_lab, com, mac, endpoint, cohort, variant, "skip", note])
            out.flush()
            continue
        if com in existing_coms:
            print(f"  -> COM {com} already in fleet_map — replacing")

        append_fleet_row(cur_lab, com, mac, endpoint, SOURCE_TAG)
        existing_macs.add(mac.lower())
        existing_coms.add(com)
        print(f"  -> OK in {dt}s  label={cur_lab} mac={mac} ep={endpoint}")
        w.writerow([time.strftime("%FT%T"), cur_lab, com, mac, endpoint, cohort, variant, "ok", "OK"])
        out.flush()
        cur_lab += 1

    out.close()
    regenerate_bench_assignment()
    print(f"\n[done] results -> {RESULTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
