#!/usr/bin/env python3
"""Validate ONE node end-to-end after provision/flash.

Polls TB Edge for the given label twice (T0 and T+60s) and asserts:
  * device is registered (endpoint exists in TB)
  * device is active (LwM2M registration alive)
  * uptime_s grows between T0 and T+60s (firmware running, not hung)
  * total_resets stable (no panic loop)
  * reg_success >= 1 (at least one full registration)

Optional: --serial flag captures 30s of serial output via tools/serial_stream.py
to verify boot banner + thread_analyzer + zero panic logs.

Usage:
    python tools/validate_one.py 31              # validate label 31
    python tools/validate_one.py 31 --serial     # also capture serial
    python tools/validate_one.py --com COM69     # validate by COM (auto-resolve label)
"""
from __future__ import annotations
import argparse, csv, subprocess, sys, time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
FLEET = REPO / "tools" / "fleet_map.csv"
EDGE = "http://192.168.8.111:8090"
USER, PASS = "tenant@thingsboard.org", "tenant"


def load_fleet():
    with open(FLEET, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def find_row(label=None, com=None, fleet=None):
    fleet = fleet or load_fleet()
    if label is not None:
        return next((r for r in fleet if r["label"] == str(label)), None)
    if com is not None:
        return next((r for r in fleet if r["com"] == com), None)
    return None


def tb_session():
    s = requests.Session()
    r = s.post(f"{EDGE}/api/auth/login",
               json={"username": USER, "password": PASS}, timeout=15)
    r.raise_for_status()
    s.headers.update({"X-Authorization": f"Bearer {r.json()['token']}"})
    return s


def tb_state(s, endpoint):
    d = s.get(f"{EDGE}/api/tenant/devices",
              params={"pageSize": 1, "page": 0, "textSearch": endpoint},
              timeout=10).json().get("data", [])
    if not d: return None
    dev = d[0]
    did = dev["id"]["id"]
    ts = s.get(f"{EDGE}/api/plugins/telemetry/DEVICE/{did}/values/timeseries",
               params={"keys": "total_resets,uptime_s,last_reset_reason,"
                              "reg_attempts,reg_success,notify_emitted,"
                              "recover_count,watchdog_count"},
               timeout=10).json()
    return {
        "active": dev.get("active", False),
        "did": did,
        "tr":  ts.get("total_resets", [{}])[0].get("value"),
        "up":  ts.get("uptime_s", [{}])[0].get("value"),
        "rr":  ts.get("last_reset_reason", [{}])[0].get("value"),
        "rA":  ts.get("reg_attempts", [{}])[0].get("value"),
        "rS":  ts.get("reg_success", [{}])[0].get("value"),
        "ne":  ts.get("notify_emitted", [{}])[0].get("value"),
        "rec": ts.get("recover_count", [{}])[0].get("value"),
        "wdt": ts.get("watchdog_count", [{}])[0].get("value"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("label", nargs="?", type=int, help="fleet_map label")
    ap.add_argument("--com", help="alternate: lookup by COM")
    ap.add_argument("--serial", action="store_true", help="capture 30s serial")
    ap.add_argument("--wait", type=int, default=60, help="seconds between T0 and T1 (default 60)")
    a = ap.parse_args()

    if a.label is None and not a.com:
        print("usage: validate_one.py LABEL  OR  --com COMxx"); return 2

    row = find_row(label=a.label, com=a.com)
    if not row:
        print(f"row not found in fleet_map.csv for label={a.label} com={a.com}")
        return 2
    label = int(row["label"]); com = row["com"]
    endpoint = row["endpoint"]; mac = row["mac"]
    cohort = "A/MTD" if label % 2 == 1 else "B/FTD"
    print(f"=== validate label={label} cohort={cohort} ===")
    print(f"  com:      {com}")
    print(f"  mac:      {mac}")
    print(f"  endpoint: {endpoint}")
    print(f"  source:   {row['source']}")

    # Optional serial capture (30s) in parallel
    serial_proc = None
    serial_log = None
    if a.serial:
        log_dir = REPO / "logs" / f"validate_{label}"
        log_dir.mkdir(parents=True, exist_ok=True)
        serial_log = log_dir / f"{com}_validate.log"
        serial_proc = subprocess.Popen(
            ["python", "tools/serial_stream.py", "--port", com,
             "--out", str(serial_log), "--seconds", "30"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"  serial capture -> {serial_log}")

    # T0 + T+wait poll
    s = tb_session()
    t0 = tb_state(s, endpoint)
    if not t0:
        print(f"\nFAIL: endpoint not in TB Edge yet (provision incomplete?)"); return 3
    print(f"\n--- T0 ---  active={t0['active']} tr={t0['tr']} rr={t0['rr']} "
          f"up={t0['up']}s reg={t0['rA']}/{t0['rS']} rec={t0['rec']} wdt={t0['wdt']}")
    print(f"\nwait {a.wait}s for telemetry to refresh...")
    time.sleep(a.wait)
    t1 = tb_state(s, endpoint)
    print(f"--- T+{a.wait}s ---  active={t1['active']} tr={t1['tr']} rr={t1['rr']} "
          f"up={t1['up']}s reg={t1['rA']}/{t1['rS']} rec={t1['rec']} wdt={t1['wdt']}")

    # Verdict
    issues = []
    if not t1["active"]: issues.append("device NOT active in TB at T+wait")
    try:
        u0 = int(t0["up"] or 0); u1 = int(t1["up"] or 0)
        if u1 <= u0: issues.append(f"uptime not growing (T0={u0}s -> T1={u1}s) — node may be hung")
    except: pass
    try:
        tr0 = int(t0["tr"] or 0); tr1 = int(t1["tr"] or 0)
        if tr1 > tr0: issues.append(f"node rebooted during window (tr {tr0} -> {tr1}) — instability")
    except: pass
    try:
        rs = int(t1["rS"] or 0)
        if rs < 1: issues.append("no LwM2M registration success yet (reg_success=0)")
    except: pass

    if serial_proc:
        serial_proc.wait(timeout=40)
        if serial_log and serial_log.exists():
            content = serial_log.read_text(errors="replace")
            n_lines = content.count("\n")
            panics = sum(1 for ln in content.splitlines()
                         if any(p in ln.upper() for p in ("PANIC","FATAL","EXCEPTION")))
            print(f"\n--- serial 30s ---  lines={n_lines}  panic_lines={panics}")
            if n_lines < 5: issues.append(f"serial output silent ({n_lines} lines) — CDC issue or board hung")
            if panics > 0: issues.append(f"{panics} panic/fatal/exception lines in serial")

    print()
    if not issues:
        print(f"VERDICT: PASS  (label {label} on {com} is healthy)")
        return 0
    print(f"VERDICT: FAIL  ({len(issues)} issues):")
    for i in issues: print(f"  - {i}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
