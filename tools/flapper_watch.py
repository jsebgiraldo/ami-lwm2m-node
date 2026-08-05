"""Focused diagnostic watch on the flapping boards.

Polls a small set of unstable boards fast (default 25s) over RPC. When one is
briefly reachable, it grabs uptime / total_resets / reset_reason (RID 21) /
last_reboot_code (RID 37) / last_err (RID 17). On every total_resets increment
it prints the CAPTURED CAUSE — so we learn WHY they flap:
  reset_reason=16 (HW-WDOG) + rbcode=0  -> CPU stall (fat build under load)
  reset_reason=2  (SW) + rbcode=4/5/... -> a software watchdog path (named)
  reset_reason=4  (BROWNOUT)            -> power/inrush
Selective stdout (resets + up/down transitions + ~4min heartbeat) so a Monitor
wrapper only pings on signal. Full detail -> logs/flapper_watch.log.

Usage: python tools/flapper_watch.py [mac_suffix ...]   default = the 6 flappers
"""
import re, sys, time, urllib.request, json

TB = 'http://192.168.8.111:8090'
DEFAULT = ['1494', '14c8', '1534', 'f6d4', 'f854', 'fbb4']
PERIOD = 25
HEARTBEAT = 10
from reboot_codes import REBOOT_CODE as CODE, RESET_CAUSE as RR   # canonical maps


def login():
    r = urllib.request.Request(TB+'/api/auth/login',
        data=json.dumps({'username':'tenant@thingsboard.org','password':'tenant'}).encode(),
        headers={'Content-Type':'application/json'}, method='POST')
    return json.loads(urllib.request.urlopen(r, timeout=15).read())['token']


def rint(tok, did, p):
    req = urllib.request.Request(TB+f'/api/rpc/twoway/{did}',
        data=json.dumps({'method':'Read','params':{'id':p},'timeout':6000}).encode(),
        headers={'X-Authorization':f'Bearer {tok}','Content-Type':'application/json'}, method='POST')
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=11).read() or b'{}')
        v = str(r.get('value',''))
        if 'OPAQUE' in v:
            m = re.search(r'value=([0-9a-fA-F]+)', v); return int(m.group(1),16) if m else None
        m = re.search(r'value=(-?\d+)', v); return int(m.group(1)) if m else None
    except Exception:
        return None


def main():
    macs = sys.argv[1:] or DEFAULT
    tok = login()
    alld = {d['name']: d['id']['id'] for d in json.loads(urllib.request.urlopen(urllib.request.Request(
        TB+'/api/tenant/devices?pageSize=300&page=0', headers={'X-Authorization': f'Bearer {tok}'}),
        timeout=15).read())['data']}
    did = {m: alld.get(f'ami-esp32c6-{m}') for m in macs}
    print(f"[flap] watching {macs} every {PERIOD}s", flush=True)
    f = open('logs/flapper_watch.log', 'w', encoding='utf-8')
    tr_prev, was_up = {}, {}
    tick = 0
    while True:
        tick += 1
        if tick % 30 == 0:
            try: tok = login()
            except Exception: pass
        up_now = []
        row = []
        for m in macs:
            d = did[m]
            up = rint(tok, d, '/33000/0/10')
            reachable = up is not None
            if reachable:
                up_now.append(m)
                tr = rint(tok, d, '/33000/0/22')
                if was_up.get(m) is False or m not in was_up:
                    rr = rint(tok, d, '/33000/0/21'); rc = rint(tok, d, '/33000/0/37'); le = rint(tok, d, '/33000/0/17')
                    print(f"[{time.strftime('%H:%M:%S')}] {m} UP up={up}s TR={tr} "
                          f"reset_reason={RR.get(rr,rr)} rbcode={CODE.get(rc,rc)} last_err={le}", flush=True)
                if tr is not None:
                    if tr_prev.get(m) is not None and tr > tr_prev[m]:
                        rr = rint(tok, d, '/33000/0/21'); rc = rint(tok, d, '/33000/0/37')
                        print(f"[{time.strftime('%H:%M:%S')}] {m} RESET TR {tr_prev[m]}->{tr} "
                              f"reset_reason={RR.get(rr,rr)} rbcode={CODE.get(rc,rc)}", flush=True)
                    tr_prev[m] = tr
                row.append(f"{m}:up{up}/TR{tr}")
            else:
                if was_up.get(m):
                    print(f"[{time.strftime('%H:%M:%S')}] {m} went DOWN", flush=True)
                row.append(f"{m}:--")
            was_up[m] = reachable
        f.write(f"[{time.strftime('%H:%M:%S')} t{tick}] " + " ".join(row) + "\n"); f.flush()
        if tick == 1 or tick % HEARTBEAT == 0:
            print(f"[{time.strftime('%H:%M')} t{tick} HB] up={len(up_now)}/{len(macs)} [{','.join(up_now)}]", flush=True)
        time.sleep(PERIOD)


if __name__ == '__main__':
    main()
