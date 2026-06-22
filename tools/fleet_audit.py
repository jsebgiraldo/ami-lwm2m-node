#!/usr/bin/env python3
"""One-shot AUDITABLE fleet health snapshot (telemetry-based, no RPC).

Pulls the latest telemetry for every ami-esp32c6 board from TB Edge and prints
a single ranked table + a classification + a list of improvement candidates.
Telemetry-only (no RPC Read) so it never hangs on a flickering/rebooting board.

Classifies each board:
  HEALTHY  - fresh telemetry, long uptime, resets/recover frozen
  CHURN    - fresh but recover_count > 0 or short uptime (session flicker)
  REBOOT   - total_resets climbed recently OR last_reset_reason != clean
  STALE    - telemetry older than FRESH_S (not reporting right now)

Separates the A/B arms by fw_version (0.7.8-ka90 test vs 0.7.7-omr control).

Writes a timestamped CSV to logs/fleet_audit_<ts>.csv for the record.

Usage:  python tools/fleet_audit.py
"""
from __future__ import annotations
import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.request

TB = os.environ.get("AMI_TB", "http://192.168.8.111:8090")
USER = "tenant@thingsboard.org"
PASS = "tenant"
FRESH_S = 300  # telemetry newer than this = "reporting"
CHURN_RECOVER = 5  # recover_count >= this over the board's life = flag for watch

# Zephyr hwinfo reset-cause bitmask (esp32c6). 8=POR is the clean boot reason.
RST_NAMES = {0: "none", 1: "PIN", 2: "SW", 4: "BROWN", 8: "POR",
             16: "WDT", 32: "DEBUG", 256: "LOCKUP"}

# Telemetry timeseries keys we audit (NOT fw_version/active — those are attrs).
KEYS = [
    "uptime_s", "total_resets", "recover_count", "watchdog_count",
    "thread_role", "last_reset_reason", "last_error_code", "heap_min_free_live",
    "current_role",
]


def login() -> str:
    r = urllib.request.Request(
        TB + "/api/auth/login",
        data=json.dumps({"username": USER, "password": PASS}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(r, timeout=15).read())["token"]


def get(tok: str, path: str):
    return json.loads(urllib.request.urlopen(urllib.request.Request(
        TB + path, headers={"X-Authorization": f"Bearer {tok}"}), timeout=15).read())


def _int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


def classify(rec: dict) -> str:
    """reset_reason is HISTORICAL (boot-time, often hours ago) so it does NOT
    drive classification — only liveness + cumulative churn do."""
    if not rec["reporting"]:
        return "STALE"
    rec_ct = _int(rec.get("recover_count"))
    wd = _int(rec.get("watchdog_count"))
    up = _int(rec.get("uptime_s"))
    if rec_ct >= CHURN_RECOVER or wd > 0:
        return "WATCH"
    if up < 1800:
        return "YOUNG"
    return "HEALTHY"


def main() -> int:
    tok = login()
    devs = {d["name"]: d["id"]["id"]
            for d in get(tok, "/api/tenant/devices?pageSize=300&page=0")["data"]
            if d["name"].startswith("ami-esp32c6-")}
    now = int(time.time() * 1000)
    keys_q = ",".join(KEYS)
    rows = []
    for name in sorted(devs):
        short = name.split("-")[-1]
        did = devs[name]
        # active + fw_version live in ATTRIBUTES (all scopes), not timeseries.
        attrs = {}
        try:
            for a in get(tok, f"/api/plugins/telemetry/DEVICE/{did}/values/attributes"):
                attrs[a["key"]] = a["value"]
        except Exception:
            pass
        active = attrs.get("active")
        fw = attrs.get("fw_version") or attrs.get("firmware_version")
        try:
            ts = get(tok, f"/api/plugins/telemetry/DEVICE/{did}"
                          f"/values/timeseries?keys={keys_q}")
        except Exception:
            ts = {}

        def val(k):
            v = ts.get(k)
            return v[0]["value"] if v else None

        def age(k):
            v = ts.get(k)
            return (now - v[0]["ts"]) // 1000 if v else None

        fresh = age("uptime_s")
        rec = {"short": short, "name": name, "active": active, "fw_version": fw,
               "age_s": fresh, "reporting": fresh is not None and fresh < FRESH_S}
        for k in KEYS:
            rec[k] = val(k)
        rec["cls"] = classify(rec)
        rows.append(rec)

    # ---- print table ----
    def arm(fw):
        s = str(fw or "")
        if "ka90" in s:
            return "ka90"
        if "omr" in s or "0.7.7" in s:
            return "prod"
        return s[:10] or "?"

    order = {"STALE": 0, "WATCH": 1, "YOUNG": 2, "HEALTHY": 3}
    rows.sort(key=lambda r: (order.get(r["cls"], 9), -(int(r["uptime_s"]) if str(r.get("uptime_s") or "").isdigit() else 0)))

    print(f"\n=== FLEET AUDIT  {dt.datetime.now().isoformat(timespec='seconds')}  "
          f"({TB}) ===")
    print(f"{'board':>6} {'cls':<8} {'arm':<5} {'act':<4} {'age':>5} {'up_s':>8} "
          f"{'TR':>3} {'rec':>3} {'wd':>3} {'role':<14} {'rstReason':<10} {'err':>5} {'heapKB':>7}")
    print("-" * 104)
    for r in rows:
        role = str(r.get("current_role") or r.get("thread_role") or "")[:13]
        heap = r.get("heap_min_free_live")
        try:
            heap = f"{int(heap)//1024}" if heap is not None else ""
        except Exception:
            heap = str(heap)[:6]
        rstn = RST_NAMES.get(_int(r.get("last_reset_reason"), -1), str(r.get("last_reset_reason")))
        print(f"{r['short']:>6} {r['cls']:<8} {arm(r.get('fw_version')):<5} "
              f"{str(r['active']):<4} {str(r['age_s']):>5} {str(r.get('uptime_s')):>8} "
              f"{str(r.get('total_resets')):>3} {str(r.get('recover_count')):>3} "
              f"{str(r.get('watchdog_count')):>3} {role:<14} "
              f"{str(rstn)[:10]:<10} {str(r.get('last_error_code')):>5} {heap:>7}")

    # ---- summary ----
    tot = len(rows)
    by_cls = {}
    by_arm = {}
    for r in rows:
        by_cls[r["cls"]] = by_cls.get(r["cls"], 0) + 1
        a = arm(r.get("fw_version"))
        by_arm.setdefault(a, []).append(r)
    routers = sum(1 for r in rows if r["reporting"] and ("Router" in str(r.get("current_role") or r.get("thread_role") or "") or "Leader" in str(r.get("current_role") or r.get("thread_role") or "")))
    print("-" * 104)
    print(f"TOTAL={tot}  " + "  ".join(f"{k}={v}" for k, v in sorted(by_cls.items()))
          + f"  | routers={routers}")
    print("arms: " + "  ".join(f"{a}={len(v)}" for a, v in sorted(by_arm.items())))

    # ---- improvement candidates ----
    print("\n--- IMPROVEMENT CANDIDATES ---")
    cand = [r for r in rows if r["cls"] in ("STALE", "WATCH", "YOUNG")]
    if not cand:
        print("  none — fleet fully HEALTHY")
    for r in sorted(cand, key=lambda r: order.get(r["cls"], 9)):
        why = []
        if r["cls"] == "STALE":
            why.append(f"silent {r['age_s']}s (last up={r.get('uptime_s')}s)")
        rstn = RST_NAMES.get(_int(r.get("last_reset_reason"), -1), r.get("last_reset_reason"))
        if str(r.get("last_error_code") or "0") not in ("0", "None"):
            why.append(f"err={r.get('last_error_code')} lastRst={rstn}")
        if str(r.get("recover_count") or "0") not in ("0", "None"):
            why.append(f"recover={r.get('recover_count')}")
        if str(r.get("watchdog_count") or "0") not in ("0", "None"):
            why.append(f"wd={r.get('watchdog_count')}")
        up = r.get("uptime_s")
        if str(up or "").isdigit() and int(up) < 1800:
            why.append(f"young up={up}s")
        print(f"  Lab/{r['short']:<6} {r['cls']:<8} arm={arm(r.get('fw_version')):<5} :: " + ", ".join(why))

    # ---- write CSV ----
    os.makedirs("logs", exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = f"logs/fleet_audit_{stamp}.csv"
    cols = ["short", "name", "cls", "active", "fw_version", "age_s", "reporting"] + KEYS
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"\n[audit] snapshot -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
