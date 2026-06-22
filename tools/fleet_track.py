"""Fleet power-on tracking — watch the 30 come up on the PSU and stay healthy.

Telemetry-based (light): per board reads the latest uptime_s / total_resets /
thread_role from TB timeseries + freshness. Logs full detail every cycle to
logs/fleet_track.log; prints to STDOUT selectively (initial snapshot, a heartbeat
every ~15 min, and immediately on a reset or a board going silent) so a Monitor
wrapper only pings on what matters.

Healthy fleet = reporting count climbs to 30 and holds, total_resets frozen,
~10-11 routers self-elect. Any total_resets bump is flagged per board.

Usage:  python tools/fleet_track.py [period_s=120]
"""
import re, sys, time, urllib.request, json

TB = 'http://192.168.8.111:8090'
LOG = 'logs/fleet_track.log'
HEARTBEAT_EVERY = 8        # ~16 min at 120s
FRESH_S = 300              # telemetry newer than this = "reporting"


def login():
    r = urllib.request.Request(TB+'/api/auth/login',
        data=json.dumps({'username':'tenant@thingsboard.org','password':'tenant'}).encode(),
        headers={'Content-Type':'application/json'}, method='POST')
    return json.loads(urllib.request.urlopen(r, timeout=15).read())['token']


def get(tok, path):
    return json.loads(urllib.request.urlopen(urllib.request.Request(
        TB+path, headers={'X-Authorization': f'Bearer {tok}'}), timeout=15).read())


def main():
    period = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    tok = login()
    devs = {d['name']: d['id']['id'] for d in get(tok, '/api/tenant/devices?pageSize=300&page=0')['data']
            if d['name'].startswith('ami-esp32c6-')}
    print(f"[fleet] tracking {len(devs)} boards, period={period}s", flush=True)
    f = open(LOG, 'w', encoding='utf-8')
    tr0, prev_report = {}, {}
    resets_since_start = 0
    tick = 0
    while True:
        tick += 1
        if tick % 15 == 0:
            try:
                tok = login()
                devs = {d['name']: d['id']['id'] for d in get(tok, '/api/tenant/devices?pageSize=300&page=0')['data']
                        if d['name'].startswith('ami-esp32c6-')}
            except Exception:
                pass
        now = int(time.time()*1000)
        reporting = routers = children = 0
        roles = {}
        events = []
        rows = []
        for name in sorted(devs):
            short = name.split('-')[-1]
            try:
                ts = get(tok, f'/api/plugins/telemetry/DEVICE/{devs[name]}/values/timeseries'
                              '?keys=uptime_s,total_resets,thread_role')
            except Exception:
                ts = {}
            def val(k):
                v = ts.get(k); return v[0]['value'] if v else None
            def age(k):
                v = ts.get(k); return (now - v[0]['ts'])//1000 if v else None
            up, tr, role = val('uptime_s'), val('total_resets'), val('thread_role')
            fresh = age('uptime_s')
            is_rep = fresh is not None and fresh < FRESH_S
            if is_rep:
                reporting += 1
                if role:
                    roles[role] = roles.get(role, 0) + 1
                    if 'Router' in str(role) or 'Leader' in str(role):
                        routers += 1
                    elif 'Child' in str(role):
                        children += 1
            if tr is not None:
                try:
                    tri = int(tr)
                    if name not in tr0:
                        tr0[name] = tri
                    elif tri > tr0[name]:
                        resets_since_start += (tri - tr0[name])
                        events.append(f"{short} RESET total_resets {tr0[name]}->{tri}")
                        tr0[name] = tri
                except Exception:
                    pass
            # silent transition
            if prev_report.get(name) and not is_rep:
                events.append(f"{short} went SILENT (last uptime {up}s, {fresh}s ago)")
            prev_report[name] = is_rep
            rows.append(f"{short}:up={up}/TR={tr}/role={role}/age={fresh}")
        f.write(f"[{time.strftime('%H:%M:%S')} t{tick}] reporting={reporting}/{len(devs)} | "
                + " | ".join(rows) + "\n"); f.flush()
        for e in events:
            print(f"[{time.strftime('%H:%M')}] {e}", flush=True)
        if tick == 1 or tick % HEARTBEAT_EVERY == 0:
            rolestr = " ".join(f"{k}={v}" for k, v in sorted(roles.items()))
            print(f"[{time.strftime('%H:%M')} t{tick}] reporting={reporting}/{len(devs)} | "
                  f"routers={routers} children={children} [{rolestr}] | resets_since_start={resets_since_start}",
                  flush=True)
        time.sleep(period)


if __name__ == '__main__':
    main()
