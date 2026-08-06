"""Fleet-wide PSU stability verification for v0.7.5-prod (fat) boards.

Because the production build is FAT (Object 33000), every board exposes
total_resets (RID 22), uptime (RID 10) and last_reboot_code (RID 37) over RPC.
This polls ALL ami-esp32c6-* devices in TB and reports, each cycle:
  - how many are reachable (RPC answers)
  - how many are STABLE (total_resets frozen vs first reading)
  - how many have crossed uptime 706s (past the old reboot cliff)
  - any board whose total_resets grew, with the RID 37 reboot CAUSE

So you can watch the 30 on the PSU and see, in one line, whether they all hold
- and if one reboots, exactly why (no guessing).

Usage:  python tools/verify_fleet.py [period_s=180]
"""
import re, sys, time, urllib.request, json

TB = 'http://192.168.8.111:8090'
from reboot_codes import REBOOT_CODE as CODE   # canonical map, incl. codes 12-16


def login():
    r = urllib.request.Request(TB+'/api/auth/login',
        data=json.dumps({'username':'tenant@thingsboard.org','password':'tenant'}).encode(),
        headers={'Content-Type':'application/json'}, method='POST')
    return json.loads(urllib.request.urlopen(r, timeout=15).read())['token']


def devices(tok):
    d = json.loads(urllib.request.urlopen(urllib.request.Request(
        TB+'/api/tenant/devices?pageSize=300&page=0',
        headers={'X-Authorization': f'Bearer {tok}'}), timeout=15).read())['data']
    return {x['name']: x['id']['id'] for x in d if x['name'].startswith('ami-esp32c6-')}


def rint(tok, did, path):
    req = urllib.request.Request(TB+f'/api/rpc/twoway/{did}',
        data=json.dumps({'method':'Read','params':{'id':path},'timeout':8000}).encode(),
        headers={'X-Authorization':f'Bearer {tok}','Content-Type':'application/json'}, method='POST')
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=15).read() or b'{}')
        v = str(r.get('value',''))
        if 'OPAQUE' in v:
            m = re.search(r'value=([0-9a-fA-F]+)', v); return int(m.group(1),16) if m else None
        m = re.search(r'value=(-?\d+)', v); return int(m.group(1)) if m else None
    except Exception:
        return None


def main():
    period = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    tok = login()
    devs = devices(tok)
    print(f"[verify] {len(devs)} ami boards in TB; polling uptime/TR/RID37 every {period}s", flush=True)
    tr0 = {}
    tick = 0
    while True:
        tick += 1
        if tick % 12 == 0:
            try: tok = login(); devs = devices(tok)
            except Exception: pass
        reach = stable = crossed = 0
        events = []
        for name, did in sorted(devs.items()):
            up = rint(tok, did, '/33000/0/10')
            tr = rint(tok, did, '/33000/0/22')
            if tr is None and up is None:
                continue            # silent (offline / sleeping)
            reach += 1
            if tr is not None:
                if name not in tr0:
                    tr0[name] = tr
                elif tr > tr0[name]:
                    rc = rint(tok, did, '/33000/0/37')
                    events.append(f"{name.split('-')[-1]} TR {tr0[name]}->{tr} cause={CODE.get(rc,rc)}")
                    tr0[name] = tr
                else:
                    stable += 1
            if up is not None and up > 706:
                crossed += 1
        line = (f"[v{tick:03d} {time.strftime('%H:%M')}] reachable={reach}/{len(devs)} "
                f"stable(TR frozen)={stable} crossed706={crossed}")
        if events:
            line += " | RESETS: " + " ; ".join(events)
        print(line, flush=True)
        time.sleep(period)


if __name__ == '__main__':
    main()
