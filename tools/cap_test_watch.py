"""Watch capacitor-modified L5 vs the rest of the fleet.

L5 has a 100µF electrolytic between 5V and GND (added 2026-06-08).
L7 is on direct PC USB (no cap, no HUB).
L1-L4, L6 are on the HUB without cap.

Tracks TR climb delta per board every 5 min. Lower delta = supply
is dipping less = cap (or direct USB) is helping.

TB returns telemetry 'value' as strings — cast to int explicitly.
"""
import datetime
import json
import time
import urllib.request

TB = "http://192.168.8.111:8090"
BOARDS = {
    "L1": "ami-esp32c6-1494",
    "L2": "ami-esp32c6-f7b4",
    "L3": "ami-esp32c6-fbb8",
    "L4": "ami-esp32c6-14c8",
    "L5": "ami-esp32c6-f6c8",
    "L6": "ami-esp32c6-14bc",
    "L7": "ami-esp32c6-1498",
    "L8": "ami-esp32c6-27f0",
    "L9": "ami-esp32c6-9e70",
}


def login():
    body = json.dumps({"username": "tenant@thingsboard.org", "password": "tenant"}).encode()
    req = urllib.request.Request(
        TB + "/api/auth/login", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=15).read())["token"]


def get(tok, path):
    req = urllib.request.Request(TB + path, headers={"X-Authorization": f"Bearer {tok}"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def main():
    tok = login()
    devs = get(tok, "/api/tenant/devices?pageSize=100&page=0")["data"]
    ids = {l: next(d["id"]["id"] for d in devs if d["name"] == n) for l, n in BOARDS.items()}
    base = {}
    tick = 0
    while True:
        tick += 1
        if tick % 6 == 0:
            try:
                tok = login()
            except Exception:
                pass
        nowms = int(time.time() * 1000)
        ts_now = datetime.datetime.now().isoformat(timespec="seconds")
        line = f"[t{tick:03d} {ts_now}]"
        alive = 0
        for lbl in sorted(BOARDS):
            try:
                ts = get(tok, f"/api/plugins/telemetry/DEVICE/{ids[lbl]}"
                              "/values/timeseries?keys=uptime_s,total_resets,last_reset_reason")
                up = ts.get("uptime_s", [{}])[0]
                tr = as_int(ts.get("total_resets", [{}])[0].get("value"))
                rr = as_int(ts.get("last_reset_reason", [{}])[0].get("value"))
                age = (nowms - up.get("ts", 0)) // 1000 if up.get("ts") else 99999
                if age < 180:
                    alive += 1
                if lbl not in base and tr is not None and age < 300:
                    base[lbl] = tr
                if tr is None or lbl not in base:
                    d_str = "?"
                else:
                    d_int = tr - base[lbl]
                    # Mark stale data so a dead board can't masquerade as flat.
                    # SED queue mode polls every 60 s; >300 s of no telemetry
                    # means the board stopped publishing, NOT that it's stable.
                    d_str = f"{d_int}" if age < 300 else f"{d_int}!stale{age}s"
                if lbl == "L8":
                    tag = "XIAO"
                elif lbl == "L9":
                    tag = "WROM"
                else:
                    tag = "Sed "
                line += f" {lbl}{tag}={d_str}(rr={rr})"
            except Exception:
                line += f" {lbl}=ERR"
        line += f" | alive={alive}/9"
        print(line, flush=True)
        time.sleep(300)


if __name__ == "__main__":
    main()
