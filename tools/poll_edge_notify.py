"""Poll TB Edge: active devices + sum_notify across fleet.

Verifies whether profile change (Observe checkbox ON, strategy=Single) wired
the OBSERVE pipeline. After save + board re-register we should see
sum_notify grow over time. Frozen = boards have not re-registered yet OR
profile change did not take effect.
"""
import json
import sys
import time
import urllib.request
import urllib.error

BASE = "http://192.168.8.111:8090"
USER = "tenant@thingsboard.org"
PASS = "tenant"


def api(token, path, method="GET", data=None):
    url = BASE + path
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Authorization"] = f"Bearer {token}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"HTTP {e.code} {path}: {e.read().decode()[:200]}\n")
        return None
    except Exception as e:
        sys.stderr.write(f"ERR {path}: {e}\n")
        return None


def login():
    r = api(None, "/api/auth/login", "POST", {"username": USER, "password": PASS})
    return r["token"] if r else None


def list_devices(token):
    r = api(token, "/api/tenant/devices?pageSize=100&page=0")
    return r.get("data", []) if r else []


def device_telem(token, dev_id):
    return api(token, f"/api/plugins/telemetry/DEVICE/{dev_id}/values/timeseries") or {}


def main(ticks=10, interval=30):
    tok = login()
    if not tok:
        print("login failed")
        return
    print(f"logged in, polling {ticks} ticks @ {interval}s")
    print(f"{'tick':<5}{'active':<8}{'min_up':<8}{'max_up':<8}{'sum_tr':<8}{'sum_ntfy':<10}{'fresh<60s':<10}")
    for i in range(ticks):
        devs = list_devices(tok)
        now_ms = int(time.time() * 1000)
        active = 0
        ups = []
        sum_tr = 0
        sum_ntfy = 0
        fresh = 0
        for d in devs:
            did = d["id"]["id"]
            telem = device_telem(tok, did)
            if not telem:
                continue
            # heuristics
            latest_ts = max((v[0]["ts"] for v in telem.values() if v), default=0)
            if latest_ts == 0:
                continue
            age_s = (now_ms - latest_ts) / 1000.0
            if age_s < 600:
                active += 1
                ups.append(age_s)
            if age_s < 60:
                fresh += 1
            # rough proxy for traffic/notify volume
            for k, vs in telem.items():
                sum_ntfy += len(vs)
                if "tx" in k.lower() or "tr_" in k.lower():
                    sum_tr += len(vs)
        if ups:
            print(f"{i:<5}{active:<8}{int(min(ups)):<8}{int(max(ups)):<8}{sum_tr:<8}{sum_ntfy:<10}{fresh:<10}")
        else:
            print(f"{i:<5}{active:<8}{'-':<8}{'-':<8}{sum_tr:<8}{sum_ntfy:<10}{fresh:<10}")
        if i < ticks - 1:
            time.sleep(interval)


if __name__ == "__main__":
    ticks = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    interval = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    main(ticks, interval)
