"""Non-intrusive kafix validation watch (RPC only, no JTAG halting).

All 3 SuperMini now run resprobe+kafix (Object 33000). Reads each board's
uptime / total_resets / keepalive_consec_fail every 2 min. The fix is proven
when every board crosses uptime 706s with total_resets frozen and consec_fail
never reaching 3 (the old bug rebooted at ~706s with consec_fail hitting 3).
"""
import re, urllib.request, json, time

TB = 'http://192.168.8.111:8090'
NODES = ('1494', 'f7b4', 'fbb8')


def login():
    r = urllib.request.Request(TB+'/api/auth/login',
        data=json.dumps({'username':'tenant@thingsboard.org','password':'tenant'}).encode(),
        headers={'Content-Type':'application/json'}, method='POST')
    return json.loads(urllib.request.urlopen(r, timeout=15).read())['token']


def rpc(tok, d, path):
    req = urllib.request.Request(TB+f'/api/rpc/twoway/{d}',
        data=json.dumps({'method':'Read','params':{'id':path},'timeout':12000}).encode(),
        headers={'X-Authorization': f'Bearer {tok}', 'Content-Type':'application/json'}, method='POST')
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=20).read() or b'{}')
        v = str(r.get('value',''))
        if 'OPAQUE' in v:
            m = re.search(r'value=([0-9a-fA-F]+)', v); return int(m.group(1),16) if m else None
        m = re.search(r'value=(-?\d+)', v); return int(m.group(1)) if m else None
    except Exception:
        return None


def main():
    tok = login()
    devs = json.loads(urllib.request.urlopen(urllib.request.Request(
        TB+'/api/tenant/devices?pageSize=300&page=0',
        headers={'X-Authorization': f'Bearer {tok}'}), timeout=15).read())['data']
    did = {n: next((x['id']['id'] for x in devs if x['name']==f'ami-esp32c6-{n}'), None) for n in NODES}
    tr0, crossed = {}, set()
    tick = 0
    MAX_TICKS = 9
    while tick < MAX_TICKS:
        tick += 1
        parts = []
        for n in NODES:
            d = did[n]
            up = rpc(tok, d, '/33000/0/10'); tr = rpc(tok, d, '/33000/0/22'); cf = rpc(tok, d, '/33000/0/30')
            if tr is not None and n not in tr0:
                tr0[n] = tr
            flag = ''
            if up is not None and up > 706 and n not in crossed:
                crossed.add(n); flag = ' <706-CROSSED>'
            if cf is not None and cf >= 2:
                flag += ' !!CF'
            parts.append(f'{n}:up={up}/TR={tr}/cf={cf}{flag}')
        print(f'[v{tick:03d}] ' + ' | '.join(parts), flush=True)
        time.sleep(120)


if __name__ == '__main__':
    main()
