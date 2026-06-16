"""Soak monitor for the 7-board v0.6.73 playground.

Polls TB Edge every 60s for L1-L7. Emits a line ONLY when something
changes vs prior tick:
  - active flip (UP / DOWN)
  - total_resets delta (RESET with rr)
  - role change (ROLE A->B)
  - cmTick freeze (CMSTUCK if uptime grows but cmTick doesn't — the
    zombie pattern that v0.6.73's watchdog should prevent)
  - boot_burst climb (BURST n)

Emits a heartbeat every 5 ticks (5 min). Designed for Monitor — each
emitted line becomes a notification.

Usage: python tools/soak_7_v0673.py --duration 7200 --interval 60
"""
import argparse
import csv
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request

TB = "http://192.168.8.111:8090"
USER = "tenant@thingsboard.org"
PASS = "tenant"

PLAYGROUND = {
    "1": "ami-esp32c6-1494",
    "2": "ami-esp32c6-f7b4",
    "3": "ami-esp32c6-fbb8",
    "4": "ami-esp32c6-14c8",
    "5": "ami-esp32c6-f6c8",
    "6": "ami-esp32c6-14bc",
    "7": "ami-esp32c6-1498",
}

RR_NAME = {0: "POR", 1: "EXT", 2: "BRN", 3: "SW_LP", 8: "SW", 16: "WDOG"}


def api(token, path, method="GET", data=None, timeout=10):
    h = {"Content-Type": "application/json"}
    if token:
        h["X-Authorization"] = f"Bearer {token}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(TB + path, data=body, headers=h, method=method)
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def login():
    return api(None, "/api/auth/login", "POST",
               {"username": USER, "password": PASS})["token"]


def fetch_state(tok, dev_id):
    s = {"active": None, "uptime_s": None, "total_resets": None,
         "last_reset_reason": None, "current_role": None,
         "boot_burst": None}
    try:
        attrs = api(tok, f"/api/plugins/telemetry/DEVICE/{dev_id}/values/attributes/SERVER_SCOPE")
        am = {a["key"]: a["value"] for a in attrs}
        s["active"] = bool(am.get("active"))
    except Exception:
        pass
    try:
        ts = api(tok, f"/api/plugins/telemetry/DEVICE/{dev_id}/values/timeseries"
                      "?keys=uptime_s,total_resets,last_reset_reason,current_role,"
                      "boot_burst,keepalive_emit,recover_count,watchdog_count")
        for k in ("uptime_s", "total_resets", "last_reset_reason",
                  "current_role", "boot_burst", "keepalive_emit",
                  "recover_count", "watchdog_count"):
            v = ts.get(k)
            if v:
                s[k] = v[0]["value"]
                if k == "uptime_s":
                    s["uptime_ts"] = v[0]["ts"]
    except Exception:
        pass
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=7200)
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--out", default="logs/soak_7_v0673.csv")
    args = ap.parse_args()

    tok = login()
    devs = api(tok, "/api/tenant/devices?pageSize=100&page=0")["data"]
    ids = {lbl: next((d["id"]["id"] for d in devs if d["name"] == name), None)
           for lbl, name in PLAYGROUND.items()}

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    new = not os.path.exists(args.out)
    f = open(args.out, "a", newline="", encoding="utf-8", buffering=1)
    w = csv.writer(f)
    if new:
        cols = ["ts", "active_count"]
        for L in sorted(PLAYGROUND.keys(), key=int):
            cols += [f"L{L}_active", f"L{L}_uptime", f"L{L}_TR", f"L{L}_uptime_ts"]
        w.writerow(cols)

    prev = {lbl: None for lbl in PLAYGROUND}
    deadline = time.time() + args.duration
    tick = 0
    print(f"# soak7 starts t0={datetime.datetime.now().isoformat(timespec='seconds')}", flush=True)

    while time.time() < deadline:
        tick += 1
        ts_now = datetime.datetime.now().isoformat(timespec="seconds")
        events = []
        active_n = 0
        row = [ts_now]
        states = {}
        for L in sorted(PLAYGROUND.keys(), key=int):
            try:
                s = fetch_state(tok, ids[L])
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    tok = login()
                    s = fetch_state(tok, ids[L])
                else:
                    s = {"active": None}
            states[L] = s
            if s.get("active"):
                active_n += 1
            p = prev[L]
            if p is not None:
                # active flip
                if p.get("active") and not s.get("active"):
                    events.append(f"L{L}_DOWN")
                elif (not p.get("active")) and s.get("active"):
                    events.append(f"L{L}_UP")
                # reset
                tr_p = p.get("total_resets")
                tr_n = s.get("total_resets")
                if isinstance(tr_p, int) and isinstance(tr_n, int) and tr_n > tr_p:
                    rr = s.get("last_reset_reason", 0)
                    rrname = RR_NAME.get(int(rr) & 0xff if isinstance(rr, int) else 0, f"?{rr}")
                    events.append(f"L{L}_RESET({tr_p}->{tr_n},{rrname})")
                # role flip
                if (s.get("current_role") and p.get("current_role")
                        and s["current_role"] != p["current_role"]):
                    events.append(f"L{L}_ROLE({p['current_role']}->{s['current_role']})")
                # cmTick freeze: uptime_ts NOT advancing more than 2x interval = zombie
                up_ts_p = p.get("uptime_ts", 0)
                up_ts_n = s.get("uptime_ts", 0)
                if up_ts_p and up_ts_n and up_ts_n == up_ts_p:
                    # only flag after 3 consecutive frozen ticks (avoid noise)
                    frozen = p.get("_frozen_count", 0) + 1
                    s["_frozen_count"] = frozen
                    if frozen == 3:
                        events.append(f"L{L}_CMSTUCK")
                # boot_burst climb
                bb_p = p.get("boot_burst")
                bb_n = s.get("boot_burst")
                if isinstance(bb_p, int) and isinstance(bb_n, int) and bb_n > bb_p:
                    events.append(f"L{L}_BURST({bb_p}->{bb_n})")
            prev[L] = s
            row += [int(bool(s.get("active"))), s.get("uptime_s"),
                    s.get("total_resets"), s.get("uptime_ts")]

        row.insert(1, active_n)
        w.writerow(row)

        line = f"[t{tick:03d} {ts_now}] active={active_n}/7"
        if events:
            line += " EVENT: " + " ".join(events)
            print(line, flush=True)
        elif tick % 5 == 0:
            print(line + " (heartbeat)", flush=True)
        if active_n < 7 and not events:
            print(line + " (active drop, no delta)", flush=True)

        time.sleep(args.interval)

    f.close()
    print(f"# soak7 ended t={datetime.datetime.now().isoformat(timespec='seconds')} ticks={tick}",
          flush=True)


if __name__ == "__main__":
    main()
