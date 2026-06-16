"""Fire Execute /3/0/4 (Reboot) the moment each target board wakes.

Queue-mode SEDs sleep between contacts; TB/Leshan throws
ClientSleepingException if you RPC while asleep. But after every notify the
client stays online for CONFIG_LWM2M_QUEUE_MODE_UPTIME (90 s here). This
script polls lastActivityTime every 10 s and strikes immediately when a
target's contact is <45 s old, then removes it from the list.

Usage: python tools/wake_strike_reboot.py 091c 14bc 14c8 ...
"""
import json, sys, time, urllib.request

TB = 'http://192.168.8.111:8090'

def login():
    r = urllib.request.Request(TB+'/api/auth/login',
        data=json.dumps({'username':'tenant@thingsboard.org','password':'tenant'}).encode(),
        headers={'Content-Type':'application/json'}, method='POST')
    return json.loads(urllib.request.urlopen(r, timeout=15).read())['token']

def get(tok, p):
    return json.loads(urllib.request.urlopen(urllib.request.Request(
        TB+p, headers={'X-Authorization': f'Bearer {tok}'}), timeout=15).read())

def post(tok, p, body, timeout=30):
    req = urllib.request.Request(TB+p, data=json.dumps(body).encode(),
        headers={'X-Authorization': f'Bearer {tok}', 'Content-Type': 'application/json'},
        method='POST')
    return urllib.request.urlopen(req, timeout=timeout).read()

def main():
    targets = set(sys.argv[1:])
    tok = login()
    devs = get(tok, '/api/tenant/devices?pageSize=300&page=0')['data']
    ids = {}
    for t in list(targets):
        d = next((x for x in devs if x['name'] == f'ami-esp32c6-{t}'), None)
        if d: ids[t] = d['id']['id']
        else: targets.discard(t)
    print(f'# wake-strike armed for {len(targets)} boards', flush=True)
    t0 = time.time(); relogin = time.time() + 1800
    while targets and time.time() - t0 < 2400:
        if time.time() > relogin:
            tok = login(); relogin = time.time() + 1800
        nowms = int(time.time() * 1000)
        for t in sorted(targets):
            try:
                attrs = get(tok, f'/api/plugins/telemetry/DEVICE/{ids[t]}/values/attributes/SERVER_SCOPE')
                am = {a['key']: a['value'] for a in attrs}
                la = am.get('lastActivityTime')
                la_age = (nowms - la) / 1000 if la else 1e9
                if la_age < 45:
                    try:
                        post(tok, f'/api/rpc/oneway/{ids[t]}',
                             {'method': 'Execute', 'params': {'id': '/3/0/4'}, 'timeout': 8000})
                        print(f'[{time.strftime("%H:%M:%S")}] {t}: STRIKE (la={la_age:.0f}s) sent', flush=True)
                        targets.discard(t)
                    except Exception as e:
                        print(f'[{time.strftime("%H:%M:%S")}] {t}: strike failed {e}', flush=True)
            except Exception:
                pass
        time.sleep(10)
    print(f'# done. remaining unstruck: {sorted(targets)}', flush=True)

if __name__ == '__main__':
    main()
