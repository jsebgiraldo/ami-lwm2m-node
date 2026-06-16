"""Delivery-ratio watch: % of boards delivering telemetry each minute.

After the minimal-profile change (5 resources, pmax=60), every live board
must push voltage/activeEnergy/etc at least every 60 s. This script samples
each board's freshest telemetry timestamp once per minute and reports:

  fresh   = boards whose newest telemetry point is < 90 s old (60s pmax + margin)
  ratio   = fresh / total — the per-minute delivery SLA number
  per-board hit-rate accumulates over the run -> the 24h SLA evidence

CSV: logs/delivery_ratio.csv (ts, fresh, total, ratio, missing_list)

Usage: python tools/delivery_ratio_watch.py --duration 86400
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import os
import time
import urllib.request

TB = "http://192.168.8.111:8090"
KEYS = "voltage,activePower,powerFactor,activeEnergy,frequency"
GHOSTS = {"ami-esp32c6-cc2c", "ami-esp32c6-cc6c"}


def login():
    body = json.dumps({"username": "tenant@thingsboard.org", "password": "tenant"}).encode()
    req = urllib.request.Request(TB + "/api/auth/login", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=15).read())["token"]


def get(tok, path):
    req = urllib.request.Request(TB + path, headers={"X-Authorization": f"Bearer {tok}"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=86400)
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--fresh-s", type=int, default=90)
    ap.add_argument("--out", default="logs/delivery_ratio.csv")
    args = ap.parse_args()

    tok = login()
    devs = get(tok, "/api/tenant/devices?pageSize=300&page=0")["data"]
    fleet = {d["name"]: d["id"]["id"] for d in devs
             if d["name"].startswith("ami-esp32c6-") and d["name"] not in GHOSTS}
    print(f"# delivery_ratio_watch: {len(fleet)} boards, fresh={args.fresh_s}s", flush=True)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    new = not os.path.exists(args.out)
    f = open(args.out, "a", newline="", encoding="utf-8", buffering=1)
    w = csv.writer(f)
    if new:
        w.writerow(["ts", "fresh", "total", "ratio_pct", "missing"])

    hits = {n: 0 for n in fleet}
    tick = 0
    deadline = time.time() + args.duration
    while time.time() < deadline:
        tick += 1
        if tick % 30 == 0:
            try:
                tok = login()
            except Exception:
                pass
        nowms = int(time.time() * 1000)
        ts_now = datetime.datetime.now().isoformat(timespec="seconds")
        fresh = 0
        missing = []
        for name, did in fleet.items():
            short = name.removeprefix("ami-esp32c6-")
            try:
                ts = get(tok, f"/api/plugins/telemetry/DEVICE/{did}/values/timeseries?keys={KEYS}")
                newest = max((v[0]["ts"] for v in ts.values() if v), default=0)
                if (nowms - newest) / 1000 < args.fresh_s:
                    fresh += 1
                    hits[name] += 1
                else:
                    missing.append(short)
            except Exception:
                missing.append(short + "?ERR")
        ratio = 100.0 * fresh / max(1, len(fleet))
        w.writerow([ts_now, fresh, len(fleet), f"{ratio:.1f}", ";".join(missing)])
        line = f"[t{tick:04d} {ts_now}] fresh={fresh}/{len(fleet)} ({ratio:.0f}%)"
        if missing and len(missing) <= 8:
            line += f" missing: {','.join(missing)}"
        if ratio < 90 or tick % 10 == 0:
            print(line, flush=True)
        time.sleep(args.interval)

    print("# per-board hit-rate (%): " +
          ", ".join(f"{n.removeprefix('ami-esp32c6-')}={100*h/max(1,tick):.1f}"
                    for n, h in sorted(hits.items())), flush=True)
    f.close()


if __name__ == "__main__":
    main()
