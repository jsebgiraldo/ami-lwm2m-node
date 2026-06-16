"""PSU soak watch for the minimal-AMI fleet (any number of boards).

Minimal builds don't expose uptime_s / total_resets (Object 33000 is off),
so health is judged from the LwM2M session itself:

  alive      = TB `active` attr AND lastActivityTime fresher than --alive-s
  cycle      = new "registered" entry in transportLog (a board that power-
               cycles re-REGISTERs; steady boards only send UPDATEs)

Emits one line per tick; prints EVENT lines when alive count changes or a
board accumulates a new registration. CSV log for post-analysis.

Covers every TB device named ami-esp32c6-* by default (use --prefix to
narrow). Token refreshed every 30 ticks (TB TTL ~3 h — learned the hard way).

Usage:
    python tools/psu_fleet_watch.py --duration 86400 --interval 60
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
USER = "tenant@thingsboard.org"
PASS = "tenant"


def login():
    body = json.dumps({"username": USER, "password": PASS}).encode()
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
    ap.add_argument("--alive-s", type=int, default=600,
                    help="lastActivity age threshold (s). SED queue-mode contact "
                         "period is ~6 min, so 600 s = 'missed one window'.")
    ap.add_argument("--prefix", default="ami-esp32c6-")
    ap.add_argument("--out", default="logs/psu_soak.csv")
    args = ap.parse_args()

    tok = login()
    devs = get(tok, "/api/tenant/devices?pageSize=200&page=0")["data"]
    fleet = {d["name"]: d["id"]["id"] for d in devs if d["name"].startswith(args.prefix)}
    print(f"# psu_fleet_watch: {len(fleet)} devices, alive_threshold={args.alive_s}s", flush=True)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    new = not os.path.exists(args.out)
    f = open(args.out, "a", newline="", encoding="utf-8", buffering=1)
    w = csv.writer(f)
    if new:
        w.writerow(["ts", "alive", "total", "down_list", "new_regs"])

    seen_reg_ts: dict[str, set] = {n: set() for n in fleet}
    reg_counts: dict[str, int] = {n: 0 for n in fleet}
    prev_alive = None
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
        alive = 0
        down = []
        new_regs = []
        for name, did in fleet.items():
            short = name.removeprefix(args.prefix)
            try:
                attrs = get(tok, f"/api/plugins/telemetry/DEVICE/{did}/values/attributes/SERVER_SCOPE")
                am = {a["key"]: a["value"] for a in attrs}
                la = am.get("lastActivityTime")
                la_age = (nowms - la) // 1000 if la else 10**9
                if am.get("active") and la_age < args.alive_s:
                    alive += 1
                else:
                    down.append(short)
                tl = get(tok, f"/api/plugins/telemetry/DEVICE/{did}/values/timeseries"
                              f"?keys=transportLog&startTs={nowms - args.interval*3000}&endTs={nowms}&limit=20")
                for e in tl.get("transportLog", []):
                    if "registered" in str(e.get("value", "")).lower() and e["ts"] not in seen_reg_ts[name]:
                        seen_reg_ts[name].add(e["ts"])
                        reg_counts[name] += 1
                        if tick > 1:  # don't alert on history backfill at start
                            new_regs.append(f"{short}(#{reg_counts[name]})")
            except Exception:
                down.append(short + "?ERR")

        w.writerow([ts_now, alive, len(fleet), ";".join(down), ";".join(new_regs)])

        line = f"[t{tick:04d} {ts_now}] alive={alive}/{len(fleet)}"
        events = []
        if new_regs:
            events.append("REREG: " + " ".join(new_regs))
        if prev_alive is not None and alive != prev_alive:
            events.append(f"ALIVE {prev_alive}->{alive}" + (f" down={','.join(down[:8])}" if down else ""))
        if events:
            print(line + " EVENT: " + " | ".join(events), flush=True)
        elif tick % 10 == 0:
            print(line + " (heartbeat)", flush=True)
        prev_alive = alive
        time.sleep(args.interval)

    f.close()
    print(f"# ended ticks={tick}. Final reg counts: " +
          ", ".join(f"{n.removeprefix(args.prefix)}={c}" for n, c in reg_counts.items() if c),
          flush=True)


if __name__ == "__main__":
    main()
