"""Token-refresh-aware soak monitor for the 7-board playground.

Fixes the bug in soak_7_v0673.py where TB token silently expires
(~3h) and fetch_state's broad except swallows the 401, making every
board look inactive forever.

Alive criterion: uptime_s telemetry timestamp within 180s of now.
Emits a line only on (alive count change) OR (per-board DOWN/UP/RESET)
OR a heartbeat every 10 ticks.
"""
import datetime
import json
import time
import urllib.request

TB = "http://192.168.8.111:8090"
USER = "tenant@thingsboard.org"
PASS = "tenant"

BOARDS = {
    "L1": "ami-esp32c6-1494",
    "L2": "ami-esp32c6-f7b4",
    "L3": "ami-esp32c6-fbb8",
    "L4": "ami-esp32c6-14c8",
    "L5": "ami-esp32c6-f6c8",
    "L6": "ami-esp32c6-14bc",
    "L7": "ami-esp32c6-1498",
}
RR = {0: "POR", 1: "EXT", 2: "BRN", 3: "SW_LP", 8: "SW", 16: "WDOG"}


def login():
    body = json.dumps({"username": USER, "password": PASS}).encode()
    req = urllib.request.Request(
        TB + "/api/auth/login",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return json.loads(urllib.request.urlopen(req, timeout=15).read())["token"]


def get(tok, path):
    req = urllib.request.Request(
        TB + path, headers={"X-Authorization": f"Bearer {tok}"}
    )
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def main():
    tok = login()
    devs = get(tok, "/api/tenant/devices?pageSize=100&page=0")["data"]
    ids = {l: next((d["id"]["id"] for d in devs if d["name"] == n), None)
           for l, n in BOARDS.items()}
    prev = {l: None for l in BOARDS}
    prev_alive = -1
    tick = 0
    print(f"# soak-fixed t0={datetime.datetime.now().isoformat(timespec='seconds')}", flush=True)

    while True:
        tick += 1
        if tick % 30 == 0:
            try:
                tok = login()
            except Exception as ex:
                print(f"# relog FAIL: {ex}", flush=True)
        nowms = int(time.time() * 1000)
        alive = 0
        events = []
        for l in sorted(BOARDS):
            try:
                ts = get(tok, f"/api/plugins/telemetry/DEVICE/{ids[l]}"
                              "/values/timeseries?keys=uptime_s,total_resets,"
                              "last_reset_reason,boot_burst,current_role")
            except Exception:
                try:
                    tok = login()
                    ts = get(tok, f"/api/plugins/telemetry/DEVICE/{ids[l]}"
                                  "/values/timeseries?keys=uptime_s,total_resets,"
                                  "last_reset_reason,boot_burst,current_role")
                except Exception:
                    continue
            up = ts.get("uptime_s", [{}])[0]
            tr = ts.get("total_resets", [{}])[0]
            rr = ts.get("last_reset_reason", [{}])[0]
            age = (nowms - up.get("ts", 0)) // 1000 if up.get("ts") else 99999
            is_alive = age < 180
            if is_alive:
                alive += 1
            p = prev[l]
            if p:
                if p["alive"] and not is_alive:
                    events.append(f"{l}_DOWN(age={age}s)")
                elif (not p["alive"]) and is_alive:
                    events.append(f"{l}_UP(up={up.get('value')}s)")
                tr_p = p.get("TR")
                tr_n = tr.get("value")
                if isinstance(tr_p, int) and isinstance(tr_n, int) and tr_n > tr_p:
                    rv = rr.get("value")
                    rname = RR.get(int(rv) & 0xff if isinstance(rv, int) else 0, f"?{rv}")
                    events.append(f"{l}_RESET({tr_p}->{tr_n},{rname})")
            prev[l] = {"alive": is_alive, "TR": tr.get("value"), "age": age}
        ts_now = datetime.datetime.now().isoformat(timespec="seconds")
        line = f"[t{tick:03d} {ts_now}] alive={alive}/7"
        if alive != prev_alive or events:
            if events:
                line += " EVENT: " + " ".join(events)
            if alive <= 2:
                line += " [LOW]"
            print(line, flush=True)
        elif tick % 10 == 0:
            print(line + " (heartbeat)", flush=True)
        prev_alive = alive
        time.sleep(60)


if __name__ == "__main__":
    main()
