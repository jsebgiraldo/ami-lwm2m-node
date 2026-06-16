"""v0.6.67 fleet-wide E2E soak via TB Edge.

Polls every interval seconds for `duration` seconds. Per tick:
- counts active devices (TB attribute `active`)
- per-device: total_resets, last_reset_reason, uptime_s, current_role, telemetry age
- emits stdout LINE on any state change (reset, active flip, role flip)
- writes one CSV row per tick to --out

Usage: python tools/soak_e2e_v067.py --out logs/soak_e2e_v067.csv --duration 7200 --interval 60
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
FLEET_MAP_CSV = "tools/fleet_map.csv"


def api(token, path, method="GET", data=None, timeout=10):
    h = {"Content-Type": "application/json"}
    if token:
        h["X-Authorization"] = f"Bearer {token}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(TB + path, data=body, headers=h, method=method)
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def login():
    return api(None, "/api/auth/login", "POST", {"username": USER, "password": PASS})["token"]


def load_fleet():
    fmap = {}
    with open(FLEET_MAP_CSV) as f:
        for r in csv.DictReader(f):
            lbl = r["label"]
            if lbl in ("31", "32"):
                continue
            fmap[r["endpoint"]] = lbl
    return fmap


def fetch_devices(tok, fmap):
    devs = api(tok, "/api/tenant/devices?pageSize=100&page=0")["data"]
    out = {}
    for d in devs:
        if d["name"] in fmap:
            out[fmap[d["name"]]] = (d["name"], d["id"]["id"])
    return out


def device_state(tok, did):
    """Return {active, total_resets, last_reset_reason, uptime_s, current_role, latest_ts}."""
    s = {"active": None, "total_resets": None, "last_reset_reason": None,
         "uptime_s": None, "current_role": None, "latest_ts": 0}
    try:
        attrs = api(tok, f"/api/plugins/telemetry/DEVICE/{did}/values/attributes/SERVER_SCOPE")
        am = {a["key"]: a["value"] for a in attrs}
        s["active"] = bool(am.get("active"))
    except Exception:
        pass
    try:
        ts = api(tok, f"/api/plugins/telemetry/DEVICE/{did}/values/timeseries"
                      "?keys=total_resets,last_reset_reason,uptime_s,current_role")
        for k in ("total_resets", "last_reset_reason", "uptime_s", "current_role"):
            v = ts.get(k)
            if v:
                s[k] = v[0]["value"]
                s["latest_ts"] = max(s["latest_ts"], v[0]["ts"])
    except Exception:
        pass
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--duration", type=int, default=7200)
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args()

    fmap = load_fleet()
    tok = login()
    devs = fetch_devices(tok, fmap)
    labels = sorted(devs.keys(), key=int)
    print(f"# tracking {len(labels)} boards: {labels}", flush=True)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    new = not os.path.exists(args.out)
    f = open(args.out, "a", newline="", encoding="utf-8", buffering=1)
    w = csv.writer(f)
    if new:
        cols = ["ts", "active_count"]
        for L in labels:
            cols += [f"L{L}_active", f"L{L}_tr", f"L{L}_rr",
                     f"L{L}_uptime", f"L{L}_role", f"L{L}_age_s"]
        w.writerow(cols)

    prev = {L: None for L in labels}
    deadline = time.time() + args.duration
    tick = 0
    print(f"# soak starts t0={datetime.datetime.now().isoformat(timespec='seconds')} "
          f"duration={args.duration}s interval={args.interval}s", flush=True)

    while time.time() < deadline:
        tick += 1
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        now_ms = int(time.time() * 1000)
        row = [ts]
        active_n = 0
        events = []

        for L in labels:
            ep, did = devs[L]
            try:
                s = device_state(tok, did)
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    tok = login()
                    s = device_state(tok, did)
                else:
                    s = {"active": None, "total_resets": None, "last_reset_reason": None,
                         "uptime_s": None, "current_role": None, "latest_ts": 0}
            except Exception:
                s = {"active": None, "total_resets": None, "last_reset_reason": None,
                     "uptime_s": None, "current_role": None, "latest_ts": 0}

            if s["active"]:
                active_n += 1
            age = int((now_ms - s["latest_ts"]) / 1000) if s["latest_ts"] else -1

            row += [int(bool(s["active"])), s["total_resets"], s["last_reset_reason"],
                    s["uptime_s"], s["current_role"], age]

            p = prev[L]
            if p is not None:
                if p["active"] and not s["active"]:
                    events.append(f"L{L}_DOWN")
                elif (not p["active"]) and s["active"]:
                    events.append(f"L{L}_UP")
                if (s["total_resets"] and p["total_resets"]
                        and str(s["total_resets"]).isdigit()
                        and str(p["total_resets"]).isdigit()
                        and int(s["total_resets"]) > int(p["total_resets"])):
                    events.append(f"L{L}_RESET({p['total_resets']}->{s['total_resets']},"
                                  f"rr={s['last_reset_reason']})")
                if s["current_role"] != p["current_role"] and s["current_role"] is not None:
                    events.append(f"L{L}_ROLE({p['current_role']}->{s['current_role']})")
            prev[L] = s

        row.insert(1, active_n)
        w.writerow(row)

        line = f"[t{tick:03d} {ts}] active={active_n}/{len(labels)}"
        if events:
            line += " EVENT: " + " ".join(events)
            print(line, flush=True)
        elif tick % 5 == 0:
            # heartbeat every 5 ticks
            print(line, flush=True)
        # always alert on active drop
        if active_n < len(labels):
            if not events:
                print(f"[t{tick:03d} {ts}] active={active_n}/{len(labels)} (no event delta)",
                      flush=True)

        sleep_s = max(1, args.interval - (time.time() % args.interval))
        time.sleep(min(sleep_s, args.interval))

    f.close()
    print(f"# soak ended t={datetime.datetime.now().isoformat(timespec='seconds')} ticks={tick}",
          flush=True)


if __name__ == "__main__":
    main()
