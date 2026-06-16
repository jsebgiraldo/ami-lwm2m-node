"""Fleet watch for MINIMAL-AMI builds (no Object 33000).

Minimal builds don't push uptime_s / total_resets, so liveness comes from:
  - TB SERVER_SCOPE attribute `lastActivityTime` (refreshed by LwM2M
    REGISTER / UPDATE / notify traffic)
  - powerFactor3P telemetry freshness (Power Meter pushes every poll)

Reset detection without Object 33000: count LwM2M re-REGISTERs. A board
that USB-cliffs reboots and re-registers, which TB logs in `transportLog`
as "Client registered". We sample transportLog each tick and count new
"registered" entries per board — that's our cycle indicator.

Usage: python tools/minimal_fleet_watch.py
"""
import datetime
import json
import time
import urllib.request

TB = "http://192.168.8.111:8090"
BOARDS = {
    "MINI_L1": "ami-esp32c6-1494",
    "XIAO_L1": "ami-esp32c6-27f0",
    "WROOM_L1": "ami-esp32c6-9e70",
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


def main():
    tok = login()
    devs = get(tok, "/api/tenant/devices?pageSize=100&page=0")["data"]
    ids = {l: next(d["id"]["id"] for d in devs if d["name"] == n) for l, n in BOARDS.items()}
    reg_counts = {l: 0 for l in BOARDS}
    seen_reg_ts = {l: set() for l in BOARDS}
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
        for lbl in BOARDS:
            try:
                attrs = get(tok, f"/api/plugins/telemetry/DEVICE/{ids[lbl]}"
                                 "/values/attributes/SERVER_SCOPE")
                am = {a["key"]: a["value"] for a in attrs}
                la = am.get("lastActivityTime")
                la_age = (nowms - la) // 1000 if la else 99999
                active = bool(am.get("active"))
                if active and la_age < 300:
                    alive += 1
                # count re-registrations via transportLog history (last hour)
                tl = get(tok, f"/api/plugins/telemetry/DEVICE/{ids[lbl]}"
                              f"/values/timeseries?keys=transportLog&startTs={nowms-3600_000}"
                              f"&endTs={nowms}&limit=50")
                for entry in tl.get("transportLog", []):
                    if "registered" in str(entry.get("value", "")).lower():
                        if entry["ts"] not in seen_reg_ts[lbl]:
                            seen_reg_ts[lbl].add(entry["ts"])
                            reg_counts[lbl] += 1
                line += f" {lbl}:act={int(active)},la={la_age}s,regs={reg_counts[lbl]}"
            except Exception:
                line += f" {lbl}:ERR"
        line += f" | alive={alive}/3"
        print(line, flush=True)
        time.sleep(300)


if __name__ == "__main__":
    main()
