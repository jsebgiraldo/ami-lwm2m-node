"""Poll TB Edge every 60s for total_resets on a set of endpoints.
Append a row to a CSV every check + emit a stderr alert if any node's
total_resets increments from the previous sample. Designed to be tailed live.

Usage: python tools/reset_watcher.py --out logs/reset_watch.csv --duration 1800
"""
import argparse, csv, datetime, os, sys, time, urllib.request, urllib.error, json

TB = "http://192.168.8.111:8090"
ENDPOINTS = ["f79c","f854","f7e8","f81c","f6e4","15d8","155c"]


def login():
    req = urllib.request.Request(f"{TB}/api/auth/login",
        data=json.dumps({"username":"tenant@thingsboard.org","password":"tenant"}).encode(),
        headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["token"]


def hdr(tok): return {"X-Authorization": f"Bearer {tok}"}


def get_did(tok, ep):
    req = urllib.request.Request(f"{TB}/api/tenant/devices?deviceName=ami-esp32c6-{ep}",
                                  headers=hdr(tok))
    return json.loads(urllib.request.urlopen(req, timeout=5).read())["id"]["id"]


def get_resets(tok, did):
    req = urllib.request.Request(
        f"{TB}/api/plugins/telemetry/DEVICE/{did}/values/timeseries?keys=total_resets,last_reset_reason",
        headers=hdr(tok))
    d = json.loads(urllib.request.urlopen(req, timeout=5).read())
    tr = d.get("total_resets",[{"value":"?","ts":0}])[0]
    rr = d.get("last_reset_reason",[{"value":"?","ts":0}])[0]
    return tr["value"], rr["value"], tr["ts"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--duration", type=int, default=1800)
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args()

    tok = login()
    dids = {ep: get_did(tok, ep) for ep in ENDPOINTS}
    prev = {ep: None for ep in ENDPOINTS}

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    new_file = not os.path.exists(args.out)
    f = open(args.out, "a", newline="", encoding="utf-8", buffering=1)
    w = csv.writer(f)
    if new_file:
        cols = ["ts"] + [f"{e}_tr" for e in ENDPOINTS] + [f"{e}_rr" for e in ENDPOINTS] + ["delta_alert"]
        w.writerow(cols)

    deadline = time.time() + args.duration
    print(f"watching resets every {args.interval}s for {args.duration}s -> {args.out}", flush=True)
    while time.time() < deadline:
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        trs, rrs = {}, {}
        delta = []
        try:
            for ep in ENDPOINTS:
                tr, rr, _ = get_resets(tok, dids[ep])
                trs[ep] = tr
                rrs[ep] = rr
                if prev[ep] is not None and tr != "?" and prev[ep] != "?" and int(tr) > int(prev[ep]):
                    delta.append(f"{ep}:{prev[ep]}->{tr}(rr={rr})")
                prev[ep] = tr
        except urllib.error.HTTPError as e:
            if e.code == 401:
                tok = login()
                continue
            print(f"[{ts}] HTTP error: {e}", file=sys.stderr, flush=True)
            time.sleep(args.interval); continue
        except Exception as e:
            print(f"[{ts}] error: {e}", file=sys.stderr, flush=True)
            time.sleep(args.interval); continue

        row = [ts] + [trs[e] for e in ENDPOINTS] + [rrs[e] for e in ENDPOINTS] + [" ".join(delta)]
        w.writerow(row)
        if delta:
            print(f"[{ts}] !!! RESET DETECTED: {' '.join(delta)}", flush=True)
        else:
            print(f"[{ts}] stable {dict(zip(ENDPOINTS,[trs[e] for e in ENDPOINTS]))}", flush=True)
        time.sleep(args.interval)
    f.close()


if __name__ == "__main__":
    main()
