#!/usr/bin/env python3
"""AMI network-capacity analyzer for a connected sub-fleet.

Gathers — in ONE sequential pass (single TB token + single OTBR SSH session, to
avoid saturating TB Edge's Postgres pool) — per-node delivery + stability +
bandwidth metrics, plus mesh health, and prints an analysis-ready table + JSON.

Metrics per node:
  - fw / role / uptime / streaming freshness
  - reboots in the window (uptime_s resets) + recover/watchdog counts
  - delivery: inbound RPC round-trip latency; outbound telemetry cadence
    (median inter-arrival of activePower)
  - bandwidth: exact LwM2M bytes/min from RID 38 (lwm2m_tx_bytes) delta
Mesh: OTBR state, router/child/neighbor counts, eidcache health, RxTotal.

Usage:  python tools/net_capacity.py [--mins 60]
"""
from __future__ import annotations
import argparse
import json
import statistics
import time
import urllib.request
import urllib.error

import fleet_common as fc
fc.bootstrap_venv()
import paramiko  # noqa: E402

TB = "http://192.168.8.111:8090"
def _fleet_labels():
    """label -> device short id (last 4 of MAC), loaded from fleet_map.csv so
    the tool covers the whole fleet (1..N), not a hardcoded subset."""
    import csv as _csv
    import pathlib as _pl
    m = {}
    fp = _pl.Path(__file__).resolve().parent / "fleet_map.csv"
    try:
        for r in _csv.DictReader(open(fp, encoding="utf-8")):
            if r.get("label", "").isdigit():
                m[f"Lab{int(r['label'])}"] = r["mac"].replace(":", "")[-4:].lower()
    except Exception:
        pass
    return dict(sorted(m.items(), key=lambda kv: int(kv[0][3:])))

LABELS = _fleet_labels() or {"Lab1": "1494", "Lab2": "f7b4"}


def login():
    r = urllib.request.Request(
        TB + "/api/auth/login",
        data=json.dumps({"username": "tenant@thingsboard.org", "password": "tenant"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(r, timeout=15).read())["token"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mins", type=int, default=60)
    args = ap.parse_args()
    win_ms = args.mins * 60 * 1000
    tok = login()

    def g(p):
        return json.loads(urllib.request.urlopen(
            urllib.request.Request(TB + p, headers={"X-Authorization": f"Bearer {tok}"}),
            timeout=20).read())

    def series(did, key, now):
        p = (f"/api/plugins/telemetry/DEVICE/{did}/values/timeseries?keys={key}"
             f"&startTs={now-win_ms}&endTs={now}&limit=5000")
        try:
            return g(p).get(key, [])
        except Exception:
            return []

    def rpc_lat(did):
        body = json.dumps({"method": "Read", "params": {"id": "/3/0/3"}, "timeout": 9000}).encode()
        req = urllib.request.Request(TB + f"/api/rpc/twoway/{did}", data=body,
                                     headers={"X-Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                                     method="POST")
        t = time.time()
        try:
            urllib.request.urlopen(req, timeout=14).read()
            return round((time.time() - t) * 1000)
        except urllib.error.HTTPError as e:
            return f"HTTP{e.code}"
        except Exception:
            return "timeout"

    devs = {d["name"]: d["id"]["id"] for d in g("/api/tenant/devices?pageSize=300&page=0")["data"]}
    now = int(time.time() * 1000)
    rows = []
    for lab, short in LABELS.items():
        did = devs.get(f"ami-esp32c6-{short}")
        if not did:
            rows.append({"lab": lab, "present": False})
            continue
        fw = next((a["value"] for a in g(f"/api/plugins/telemetry/DEVICE/{did}/values/attributes")
                   if a["key"] == "fw_version"), "?")
        up = series(did, "uptime_s", now)
        ap_s = series(did, "activePower", now)
        txb = series(did, "lwm2m_tx_bytes", now)
        cur = {k: g(f"/api/plugins/telemetry/DEVICE/{did}/values/timeseries?keys={k}").get(k, [{}])[0]
               for k in ("uptime_s", "thread_role", "total_resets", "recover_count", "watchdog_count")}
        last_up_ts = cur["uptime_s"].get("ts")
        age = (now - last_up_ts) // 1000 if last_up_ts else None
        streaming = age is not None and age < 150
        # reboots in window: count uptime_s decreases. MUST sort by ts ASC first —
        # the TB API returns newest-first, which would make every step look like a
        # decrease (false reboots).
        ups = sorted([(int(x["ts"]), float(x["value"])) for x in up if x.get("value") not in (None, "")],
                     key=lambda t: t[0])
        reboots = sum(1 for a, b in zip(ups, ups[1:]) if b[1] < a[1] - 5)
        # cadence: median inter-arrival of telemetry heartbeat (uptime_s is the
        # reliably-present key; activePower can be sparse). seconds between pushes.
        ats = sorted(int(x["ts"]) for x in up if x.get("ts"))
        if len(ats) < 2:
            ats = sorted(int(x["ts"]) for x in ap_s if x.get("ts"))
        gaps = [(b - a) / 1000 for a, b in zip(ats, ats[1:])]
        cadence = round(statistics.median(gaps), 1) if gaps else None
        # bandwidth: exact bytes/min from tx_bytes. The counter resets to 0 on each
        # reboot, so SUM ONLY POSITIVE consecutive deltas (a negative step = a reset,
        # not real traffic) over the observed span.
        tvs = sorted([(int(x["ts"]), int(x["value"])) for x in txb if str(x.get("value", "")).lstrip("-").isdigit()],
                     key=lambda t: t[0])
        bw = None
        if len(tvs) >= 2:
            total = sum(max(0, b[1] - a[1]) for a, b in zip(tvs, tvs[1:]))
            dt_min = (tvs[-1][0] - tvs[0][0]) / 60000
            if dt_min > 0:
                bw = round(total / dt_min, 1)
        rows.append({
            "lab": lab, "present": True, "fw": fw, "role": cur["thread_role"].get("value"),
            "uptime": cur["uptime_s"].get("value"), "age": age, "streaming": streaming,
            "resets": cur["total_resets"].get("value"), "recover": cur["recover_count"].get("value"),
            "wd": cur["watchdog_count"].get("value"), "reboots_win": reboots,
            "cadence_s": cadence, "bw_Bpm": bw, "tx_pts": len(tvs),
            "rpc_ms": rpc_lat(did) if streaming else "-",
        })

    # OTBR mesh (single SSH session)
    mesh = {}
    try:
        c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect("192.168.8.111", username="root", password="root", timeout=15)
        def sh(cmd, t=25):
            _i, o, e = c.exec_command(cmd, timeout=t); return (o.read().decode() + e.read().decode()).strip()
        mesh["state"] = sh("ot-ctl state").split("\n")[0]
        for t in ("router table", "child table", "neighbor table"):
            n = sum(l.strip().startswith("|") for l in sh(f"ot-ctl {t}").splitlines()) - 1
            mesh[t.split()[0]] = max(n, 0)
        ec = sh("ot-ctl eidcache").splitlines()
        mesh["eid_resolved"] = sum("cache" in l and "retry" not in l for l in ec)
        mesh["eid_retry"] = sum("retry" in l for l in ec)
        mesh["rxtotal"] = next((l.split(":")[1].strip() for l in sh("ot-ctl counters mac").splitlines() if "RxTotal" in l), "?")
        c.close()
    except Exception as ex:
        mesh["error"] = str(ex)

    # ---- print ----
    print(f"\n=== AMI {len([r for r in rows if r.get('present')])}-node capacity (window {args.mins}min) ===\n")
    hdr = f"{'Lab':5}{'fw':12}{'role':8}{'up':>7}{'strm':>6}{'rst':>4}{'rcv':>4}{'wd':>3}{'rbt':>4}{'cad_s':>7}{'B/min':>8}{'RPC_ms':>8}"
    print(hdr); print("-" * len(hdr))
    for r in rows:
        if not r.get("present"):
            print(f"{r['lab']:5}{'(no en TB)':12}"); continue
        print(f"{r['lab']:5}{str(r['fw'])[:11]:12}{str(r['role'])[:7]:8}{str(r['uptime']):>7}"
              f"{('si' if r['streaming'] else 'no'):>6}{str(r['resets']):>4}{str(r['recover']):>4}"
              f"{str(r['wd']):>3}{str(r['reboots_win']):>4}{str(r['cadence_s']):>7}{str(r['bw_Bpm']):>8}{str(r['rpc_ms']):>8}")
    print(f"\nMESH: {mesh}")
    # summary
    up_nodes = [r for r in rows if r.get("streaming")]
    lats = [r["rpc_ms"] for r in up_nodes if isinstance(r["rpc_ms"], int)]
    bws = [r["bw_Bpm"] for r in up_nodes if isinstance(r["bw_Bpm"], (int, float))]
    print(f"\nSUMMARY: streaming={len(up_nodes)}/{len([r for r in rows if r.get('present')])} | "
          f"RPC latency med={statistics.median(lats) if lats else '?'}ms (n={len(lats)}) | "
          f"total exact BW={round(sum(bws),1) if bws else '?'} B/min")
    with open("tools/net_capacity_last.json", "w") as f:
        json.dump({"rows": rows, "mesh": mesh, "ts": now}, f, indent=2)
    print("saved -> tools/net_capacity_last.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
