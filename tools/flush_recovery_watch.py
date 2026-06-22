#!/usr/bin/env python3
"""Watch fleet recovery after an OTBR otbr-agent restart (address-cache flush).

After flushing the poisoned TMF address cache (eidcache 254 retry -> 0), the 30
nodes re-attach + re-register. This tracks the recovery curve WITHOUT poisoning the
cache itself:
  - telemetry-fresh (OUTBOUND, server-side, zero mesh load) = how many nodes pushed
    any telemetry in the last FRESH_S seconds = re-attach/report recovery.
  - OTBR eidcache cached/retry (SSH, cheap) = is resolution working / re-poisoning?
  - a TINY 4-board RPC probe (inbound) = does Edge->node resolution work now? kept
    small on purpose so the measurement isn't the poison source.

Prints one line per cycle (Monitor-friendly). Usage: python tools/flush_recovery_watch.py [cycles=14] [period=75]
"""
import re, json, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
import paramiko

TB = 'http://192.168.8.111:8090'
FRESH_S = 100
PROBE = ['1494', 'f854', '155c', '08fc']     # 4 fixed boards for the inbound RPC probe
CYCLES = int(sys.argv[1]) if len(sys.argv) > 1 else 14
PERIOD = int(sys.argv[2]) if len(sys.argv) > 2 else 75
KEYS = 'uptime_s,activePower,reg_success,thread_role'


def login():
    last = None
    for _ in range(4):                      # host is overloaded -> retry slow logins
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
    return {x['name']: x['id']['id'] for x in d if x['name'].startswith('ami-esp32c6-')}


def freshest(tok, did):
    u = TB + f'/api/plugins/telemetry/DEVICE/{did}/values/timeseries?keys={KEYS}'
    try:
        v = json.loads(urllib.request.urlopen(urllib.request.Request(
            u, headers={'X-Authorization': f'Bearer {tok}'}), timeout=10).read() or b'{}')
        now = int(time.time() * 1000)
        ages = [(now - a[0]['ts']) / 1000 for a in v.values() if a]
        return min(ages) if ages else 9e9
    except Exception:
        return 9e9


def rpc(tok, did, tmo=6000):
    req = urllib.request.Request(TB + f'/api/rpc/twoway/{did}',
        data=json.dumps({'method': 'Read', 'params': {'id': '/33000/0/10'}, 'timeout': tmo}).encode(),
        headers={'X-Authorization': f'Bearer {tok}', 'Content-Type': 'application/json'}, method='POST')
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=(tmo / 1000) + 4).read() or b'{}')
        return re.search(r'value=(-?\d+)', str(r.get('value', ''))) is not None
    except Exception:
        return False


def otbr():
    try:
        c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect('192.168.8.111', username='root', password='root', timeout=10,
                  look_for_keys=False, allow_agent=False)
        si, so, se = c.exec_command("ot-ctl eidcache 2>&1", timeout=15)
        ec = so.read().decode('utf-8', 'replace')
        c.close()
        return len(re.findall(r'cached', ec)), len(re.findall(r'retry', ec))
    except Exception:
        return -1, -1


def main():
    tok = login(); devs = devices(tok)
    probe_ids = {s: did for n, did in devs.items() for s in [n.split('-')[-1]] if s in PROBE}
    print(f"[flushrec] {len(devs)} boards, {CYCLES} cycles x {PERIOD}s | fresh<{FRESH_S}s=reporting, probe={PROBE}", flush=True)
    for k in range(CYCLES):
        t0 = time.time()
        if k % 8 == 7:
            try: tok = login()
            except Exception: pass
        with ThreadPoolExecutor(max_workers=10) as ex:
            ages = list(ex.map(lambda it: freshest(tok, it[1]), devs.items()))
        fresh = sum(1 for a in ages if a < FRESH_S)
        ca, rt = otbr()
        prb = {s: rpc(tok, did) for s, did in probe_ids.items()}
        ok = sum(1 for v in prb.values() if v)
        pr = " ".join(f"{s}:{'Y' if prb[s] else 'n'}" for s in PROBE if s in prb)
        print(f"[{time.strftime('%H:%M:%S')} c{k+1}] reporting={fresh}/{len(devs)} | "
              f"eidcache cached={ca} retry={rt} | RPCprobe {ok}/{len(prb)} [{pr}]", flush=True)
        dt = PERIOD - (time.time() - t0)
        if dt > 0 and k < CYCLES - 1:
            time.sleep(dt)
    print("[flushrec] done", flush=True)


if __name__ == '__main__':
    main()
