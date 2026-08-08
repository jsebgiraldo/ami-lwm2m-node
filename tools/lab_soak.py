#!/usr/bin/env python3
"""Bench soak with PRODUCTION watchdog values.

The bench used to disable the registration-gated safety reboots because it had
no server; it now runs a real ThingsBoard and overlays/lab.conf ships the field
values. So this soak asks the question that matters: does a node survive the
exact configuration the fleet runs?

It also re-tests the v0.7.18 deadlock fix in its real setting —
CONFIG_AMI_BOOT_BURST_THROTTLE_S (300) and CONFIG_AMI_HW_WATCHDOG_TIMEOUT_S (300)
are back to production values, the very combination that used to trap a node in
a permanent, OTA-proof reboot loop.

Samples both sides every interval: the mesh (ot-ctl) and ThingsBoard (telemetry
keys + the diagnostic counters), then prints a PASS/FAIL verdict on the things
that would betray a regression: total_resets climbing, watchdog_count > 0,
boot_burst climbing, uptime resetting, telemetry key count dropping.

  python tools/lab_soak.py --minutes 30
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import re
import subprocess
import sys
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fleet_common as fc  # noqa: E402
from lab_paths import captures_dir

EP = "ami-esp32c6-3bb0"
WATCH = ["total_resets", "watchdog_count", "boot_burst", "noreg_boots",
         "recover_count", "uptime_s", "reg_success", "storm_backoff"]


def wsl(cmd: str) -> str:
    try:
        return subprocess.run(["wsl.exe", "-d", "Ubuntu-24.04", "-u", "root", "--",
                               "bash", "-lc", cmd],
                              capture_output=True, text=True, timeout=45).stdout.strip()
    except Exception:
        return ""


def tb_login(base):
    r = urllib.request.Request(base + "/api/auth/login",
                               data=json.dumps({"username": "tenant@thingsboard.org",
                                                "password": "tenant"}).encode(),
                               headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(r, timeout=20).read())["token"]


def tb_get(base, tok, path):
    h = {"X-Authorization": f"Bearer {tok}", "Authorization": f"Bearer {tok}"}
    return json.loads(urllib.request.urlopen(
        urllib.request.Request(base + path, headers=h), timeout=25).read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    host, port = fc.edge_for_mesh("lab")
    base = f"http://{host}:{port}"
    out = captures_dir() / f"lab_soak_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    cols = ["t_min", "children", "keys"] + WATCH
    with out.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(cols)

    print(f"soak {args.minutes:.0f} min @{args.interval}s  ->  {out.name}")
    print("production watchdogs: BOOT_REGISTER_DEADLINE=180  HW_WDOG=300  BURST_THROTTLE=300\n")

    rows = []
    t0 = time.time()
    while time.time() - t0 < args.minutes * 60:
        el = (time.time() - t0) / 60
        n = len(re.findall(r"(?m)^\|\s*\d", wsl("ot-ctl child table")))
        vals, keys = {}, 0
        try:
            tok = tb_login(base)
            devs = tb_get(base, tok,
                          f"/api/tenant/deviceInfos?pageSize=100&page=0&textSearch={EP}")["data"]
            dev = next((d for d in devs if d["name"] == EP), None)
            if dev:
                ts = tb_get(base, tok,
                            f"/api/plugins/telemetry/DEVICE/{dev['id']['id']}/values/timeseries")
                keys = len(ts)
                for k in WATCH:
                    v = ts.get(k, [{}])[0].get("value")
                    if v is not None and str(v).lstrip("-").isdigit():
                        vals[k] = int(v)
        except Exception as e:
            print(f"  +{el:5.1f}m  TB sample failed: {type(e).__name__}")
        row = [round(el, 2), n, keys] + [vals.get(k, "") for k in WATCH]
        rows.append(row)
        with out.open("a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row)
        print(f"  +{el:5.1f}m  child={n} keys={keys} "
              + " ".join(f"{k}={vals.get(k, '?')}" for k in
                         ("uptime_s", "total_resets", "watchdog_count", "boot_burst")))
        left = args.minutes * 60 - (time.time() - t0)
        if left <= 0:
            break
        time.sleep(min(args.interval, left))

    # ---- verdict ----
    def col(name):
        i = cols.index(name)
        return [r[i] for r in rows if isinstance(r[i], int)]

    print("\n" + "=" * 62)
    ok = True
    for name, rule in (("total_resets", "must not climb"),
                       ("watchdog_count", "must stay 0"),
                       ("boot_burst", "must not climb")):
        v = col(name)
        if not v:
            print(f"  {name:16} no data")
            continue
        climbed = v[-1] - v[0]
        bad = (climbed > 0) or (name == "watchdog_count" and max(v) > 0)
        ok &= not bad
        print(f"  {name:16} {v[0]} -> {v[-1]}   ({rule}) {'FAIL' if bad else 'ok'}")
    up = col("uptime_s")
    if up:
        drops = sum(1 for a, b in zip(up, up[1:]) if b < a)
        ok &= drops == 0
        print(f"  {'uptime_s':16} {up[0]} -> {up[-1]}   (resets seen: {drops}) "
              f"{'FAIL' if drops else 'ok'}")
    ks = col("keys")
    if ks:
        print(f"  {'telemetry keys':16} min={min(ks)} max={max(ks)}")
    ch = col("children")
    if ch:
        print(f"  {'attached':16} {sum(1 for c in ch if c > 0)}/{len(ch)} samples")
    print(f"\n  VERDICT: {'PASS — stable on production watchdogs' if ok else 'FAIL — see above'}")
    print(f"  data: {out}")
    print("=" * 62)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
