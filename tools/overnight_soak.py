"""Overnight stability soak for the 3 SuperMini on v0.7.5-live (Fix A).

Polls all 3 every 180s. Writes a full per-tick record to logs/overnight_soak.log
for morning review. Prints to STDOUT *selectively* (so a Monitor wrapper only
notifies on what matters): an initial confirmation, a heartbeat every ~2h, and
IMMEDIATELY on any reset (total_resets increment) with the decoded RID 37 cause.

Goal: prove total_resets stays FROZEN for hours (the ~700s HW-liveness reboot
loop is gone). If any board does reset, RID 37 names the path (firmware now
self-documents), so the morning review is conclusive either way.
"""
import re, urllib.request, json, time

TB = 'http://192.168.8.111:8090'
NODES = ('1494', 'f7b4', 'fbb8')
CODE = {0:'(none/HW/ext)',1:'boot-watchdog',2:'mesh-alone',3:'conn-mon-no-first-tick',
        4:'conn-mon-WEDGED',5:'max-recover-attempts',6:'lwm2m-device-reboot',7:'shell',
        8:'ip6-enable-fail',9:'thread-enable-fail',10:'dns-sd-boot-fail',11:'PANIC',99:'other'}
LOG = 'logs/overnight_soak.log'
PERIOD = 180
HEARTBEAT_EVERY = 40        # ~2h


def login():
    r = urllib.request.Request(TB+'/api/auth/login',
        data=json.dumps({'username':'tenant@thingsboard.org','password':'tenant'}).encode(),
        headers={'Content-Type':'application/json'}, method='POST')
    return json.loads(urllib.request.urlopen(r, timeout=15).read())['token']


def rint(tok, d, p):
    req = urllib.request.Request(TB+f'/api/rpc/twoway/{d}',
        data=json.dumps({'method':'Read','params':{'id':p},'timeout':10000}).encode(),
        headers={'X-Authorization':f'Bearer {tok}','Content-Type':'application/json'}, method='POST')
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=18).read() or b'{}')
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
    tr_base, tr_prev, max_up, resets = {}, {}, {}, {n:0 for n in NODES}
    t_start = time.time()
    tick = 0
    print(f'[soak] overnight started {time.strftime("%H:%M")} — 3 boards on v0.7.5-live, period={PERIOD}s', flush=True)
    f = open(LOG, 'w', encoding='utf-8')
    while True:
        tick += 1
        if tick % 18 == 0:
            try: tok = login()
            except Exception: pass
        rec, events = [], []
        for n in NODES:
            d = did[n]
            up = rint(tok, d, '/33000/0/10'); tr = rint(tok, d, '/33000/0/22')
            rc = rint(tok, d, '/33000/0/37'); cf = rint(tok, d, '/33000/0/30')
            rs = rint(tok, d, '/33000/0/15')   # recover_count
            if tr is not None:
                tr_base.setdefault(n, tr)
                if tr_prev.get(n) is not None and tr > tr_prev[n]:
                    resets[n] += (tr - tr_prev[n])
                    events.append(f'{n} RESET! TR {tr_prev[n]}->{tr} cause=RID37={rc}={CODE.get(rc,rc)} (up now {up})')
                tr_prev[n] = tr
            if up is not None:
                max_up[n] = max(max_up.get(n, 0), up)
            rec.append(f'{n}:up={up}/TR={tr}/rb={rc}/cf={cf}/rec={rs}')
        hh = (time.time()-t_start)/3600.0
        line = f'[{time.strftime("%H:%M:%S")} +{hh:.1f}h t{tick}] ' + ' | '.join(rec)
        f.write(line+'\n'); f.flush()
        for e in events:
            print(f'[{time.strftime("%H:%M")} +{hh:.1f}h] {e}', flush=True)
        if tick == 3 or tick % HEARTBEAT_EVERY == 0:
            tot = sum(resets.values())
            mu = ' '.join(f'{n}:max_up={max_up.get(n,0)}s/TR+{tr_prev.get(n,0)-tr_base.get(n,0) if n in tr_base else "?"}' for n in NODES)
            print(f'[{time.strftime("%H:%M")} +{hh:.1f}h HEARTBEAT] resets_total={tot} | {mu}', flush=True)
        time.sleep(PERIOD)


if __name__ == '__main__':
    main()
