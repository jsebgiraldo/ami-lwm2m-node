#!/usr/bin/env python3
"""Baseline monitor for TB-Edge active<->inactive FLICKER (session drops).

Goal: characterize, on the real 30-node PSU fleet, how often boards flip
'inactive' in TB Edge, and WHY — so a reliability fix can be chosen with
evidence (not by adding traffic, which makes congestion worse).

Server-side only (TB Edge REST). Zero mesh load. For each board it polls the
'active' flag every POLL_S and, on every transition, logs:
  - direction (DOWN = lost session, UP = recovered) + duration of the gap
  - thread_role AT THE DROP (Router vs Child -> is it router-churn?)
  - thread_partition_id (split/merge?)
  - total_resets delta across the gap -> REBOOT vs MESH-DROP (different bugs!)

Per-event rows go to tools/flicker_log.csv for offline analysis. A summary
line is emitted every SUMMARY_CYCLES poll cycles.

Usage:  python tools/flicker_baseline.py [poll_s=30]
"""
import csv, json, sys, time, urllib.request, pathlib
from concurrent.futures import ThreadPoolExecutor

TB = 'http://192.168.8.111:8090'
POLL_S = int(sys.argv[1]) if len(sys.argv) > 1 else 30
SUMMARY_CYCLES = 24                       # ~12 min at 30s
LOG = pathlib.Path(__file__).resolve().parent / "flicker_log.csv"
FLEET_MAP = pathlib.Path(__file__).resolve().parent / "fleet_map.csv"


def labels():
    out = {}
    if FLEET_MAP.exists():
        with FLEET_MAP.open() as f:
            for r in csv.DictReader(f):
                ep = (r.get("endpoint") or "").strip()
                if ep:
                    out[ep.split("-")[-1]] = r.get("label")
    return out


def login():
    last = None
    for _ in range(4):
        try:
            r = urllib.request.Request(TB + '/api/auth/login',
                data=json.dumps({'username': 'tenant@thingsboard.org', 'password': 'tenant'}).encode(),
                headers={'Content-Type': 'application/json'}, method='POST')
            return json.loads(urllib.request.urlopen(r, timeout=30).read())['token']
        except Exception as e:
            last = e; time.sleep(3)
    raise last


def devices(tok):
    d = json.loads(urllib.request.urlopen(urllib.request.Request(
        TB + '/api/tenant/devices?pageSize=300&page=0',
        headers={'X-Authorization': f'Bearer {tok}'}), timeout=15).read())['data']
    return {x['name'].split('-')[-1]: x['id']['id'] for x in d if x['name'].startswith('ami-esp32c6-')}


def poll(tok, s, did):
    """Return (active:bool, role:str, part:str, total_resets:int|None)."""
    o = [None, '?', '?', None]
    try:
        a = {x['key']: x['value'] for x in json.loads(urllib.request.urlopen(urllib.request.Request(
            f'{TB}/api/plugins/telemetry/DEVICE/{did}/values/attributes/SERVER_SCOPE',
            headers={'X-Authorization': f'Bearer {tok}'}), timeout=10).read())}
        o[0] = a.get('active') in (True, 'true')
        v = json.loads(urllib.request.urlopen(urllib.request.Request(
            f'{TB}/api/plugins/telemetry/DEVICE/{did}/values/timeseries?keys=thread_role,thread_partition_id,total_resets',
            headers={'X-Authorization': f'Bearer {tok}'}), timeout=8).read() or b'{}')
        if v.get('thread_role'): o[1] = v['thread_role'][0]['value']
        if v.get('thread_partition_id'): o[2] = str(v['thread_partition_id'][0]['value'])
        if v.get('total_resets'): o[3] = int(v['total_resets'][0]['value'])
    except Exception:
        pass
    return tuple(o)


def main():
    LAB = labels()
    tok = login(); devs = devices(tok)
    new = not LOG.exists()
    fcsv = LOG.open("a", newline="", encoding="utf-8"); w = csv.writer(fcsv)
    if new:
        w.writerow(["ts", "lab", "board", "event", "gap_s", "role_at_drop", "kind", "partition", "tr"])
    st = {s: {"act": None, "since": time.time(), "role": "?", "tr": None,
              "flick": 0, "ina_s": 0.0, "reboots": 0, "mesh_drops": 0} for s in devs}
    def lab(s): return LAB.get(s, '?')
    print(f"[flicker] baseline on {len(devs)} boards | poll={POLL_S}s | DOWN=session lost, UP=recovered | kind: MESH vs REBOOT", flush=True)
    cyc = 0
    while True:
        cyc += 1
        if cyc % 40 == 0:
            try: tok = login()
            except Exception: pass
        with ThreadPoolExecutor(max_workers=10) as ex:
            res = dict(zip(sorted(devs), ex.map(lambda it: poll(tok, it[0], it[1]), sorted(devs.items()))))
        now = time.time()
        for s, (act, role, part, tr) in res.items():
            if act is None:
                continue
            d = st[s]
            if d["role"] == "?" and role != "?": d["role"] = role
            if role != "?": d["role"] = role
            if d["act"] is None:                     # first reading
                d["act"] = act; d["since"] = now; d["tr"] = tr; continue
            if act != d["act"]:
                gap = now - d["since"]
                if not act:                          # DOWN: session lost
                    d["flick"] += 1
                    w.writerow([time.strftime('%H:%M:%S'), lab(s), s, "DOWN", "", d["role"], "", part, tr]); fcsv.flush()
                    print(f"[{time.strftime('%H:%M:%S')}] DOWN  Lab {lab(s):>3} {s}  role={d['role']} part={part}", flush=True)
                else:                                # UP: recovered
                    rebooted = (tr is not None and d["tr"] is not None and tr > d["tr"])
                    kind = "REBOOT" if rebooted else "MESH"
                    if rebooted: d["reboots"] += 1
                    else: d["mesh_drops"] += 1
                    d["ina_s"] += gap
                    w.writerow([time.strftime('%H:%M:%S'), lab(s), s, "UP", f"{gap:.0f}", d["role"], kind, part, tr]); fcsv.flush()
                    print(f"[{time.strftime('%H:%M:%S')}] UP    Lab {lab(s):>3} {s}  after {gap:.0f}s  [{kind}]"
                          + (f" TR {d['tr']}->{tr}" if rebooted else ""), flush=True)
                d["act"] = act; d["since"] = now
            if tr is not None: d["tr"] = tr
        if cyc % SUMMARY_CYCLES == 0:
            inactive = [s for s in devs if st[s]["act"] is False]
            tot_f = sum(d["flick"] for d in st.values())
            tot_reboot = sum(d["reboots"] for d in st.values())
            tot_mesh = sum(d["mesh_drops"] for d in st.values())
            top = sorted(((d["flick"], s) for s, d in st.items() if d["flick"]), reverse=True)[:6]
            topstr = " ".join(f"Lab{lab(s)}({n})" for n, s in top) or "none"
            print(f"[{time.strftime('%H:%M')} SUMMARY c{cyc}] active={len(devs)-len(inactive)}/{len(devs)} "
                  f"now-inactive={[lab(s) for s in inactive]} | total drops={tot_f} (MESH={tot_mesh} REBOOT={tot_reboot}) "
                  f"| top flickers: {topstr}", flush=True)
        time.sleep(POLL_S)


if __name__ == '__main__':
    main()
