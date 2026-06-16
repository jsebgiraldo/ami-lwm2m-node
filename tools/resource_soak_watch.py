"""Resource-soak + Send watch.

- 1494 (resprobe build, Object 33000 on): track heap_min_free_live over time
  to detect a leak (monotonic fall), plus watchdog_count / recover_count /
  total_resets to catch resets, and uptime_s to confirm liveness.
- f7b4 + fbb8 (send builds): voltage / activePower freshness to keep an eye
  on the Send cadence.

Heap verdict: prints the delta of heap_min_free_live vs the first reading.
A steadily falling min-free = leak; flat/rising = healthy.
"""
import re, urllib.request, json, time

TB = 'http://192.168.8.111:8090'
def login():
    r = urllib.request.Request(TB+'/api/auth/login',
        data=json.dumps({'username':'tenant@thingsboard.org','password':'tenant'}).encode(),
        headers={'Content-Type':'application/json'}, method='POST')
    return json.loads(urllib.request.urlopen(r, timeout=15).read())['token']
def get(tok, p):
    return json.loads(urllib.request.urlopen(urllib.request.Request(
        TB+p, headers={'X-Authorization': f'Bearer {tok}'}), timeout=15).read())
def rpc_read_int(tok, did, path):
    """RID 36 (heap_min_free_live) comes back as OPAQUE hex because TB's
    Object 33000 model predates RID 36. RPC-read it and parse the hex."""
    req = urllib.request.Request(TB+f'/api/rpc/twoway/{did}',
        data=json.dumps({'method':'Read','params':{'id':path},'timeout':20000}).encode(),
        headers={'X-Authorization': f'Bearer {tok}', 'Content-Type':'application/json'}, method='POST')
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=30).read() or b'{}')
        v = str(r.get('value',''))
        m = re.search(r'value=([0-9a-fA-F]+)', v)
        if m and 'OPAQUE' in v:
            return int(m.group(1), 16)
        m = re.search(r'value=(-?\d+)', v)
        return int(m.group(1)) if m else None
    except Exception:
        return None

def main():
    tok = login()
    devs = get(tok, '/api/tenant/devices?pageSize=300&page=0')['data']
    rid = next(d['id']['id'] for d in devs if d['name'] == 'ami-esp32c6-1494')  # resprobe
    send = {k: next(d['id']['id'] for d in devs if d['name'] == f'ami-esp32c6-{k}')
            for k in ('f7b4', 'fbb8')}
    heap0 = None
    tick = 0
    while True:
        tick += 1
        if tick % 18 == 0:
            try: tok = login()
            except Exception: pass
        nowms = int(time.time()*1000)
        # 1494 resource counters
        rs = get(tok, f'/api/plugins/telemetry/DEVICE/{rid}/values/timeseries'
                      '?keys=watchdog_count,recover_count,total_resets,uptime_s')
        def val(k):
            v = rs.get(k); return v[0]['value'] if v else None
        # heap min-free via RPC (OPAQUE hex) — every 3rd tick to limit RPC load
        heap = rpc_read_int(tok, rid, '/33000/0/36') if tick % 1 == 0 else None
        if heap0 is None and heap: heap0 = heap
        dh = (heap - heap0) if (heap and heap0) else '?'
        up = val('uptime_s')
        upage = (nowms - rs['uptime_s'][0]['ts'])//1000 if rs.get('uptime_s') else '?'
        line = (f'[t{tick:03d}] 1494(resprobe): heap_min={heap}B (d={dh}B) '
                f'up={up}s(age{upage}s) wdog={val("watchdog_count")} '
                f'recov={val("recover_count")} TR={val("total_resets")}')
        # send boards V/P freshness
        for k, did in send.items():
            ts = get(tok, f'/api/plugins/telemetry/DEVICE/{did}/values/timeseries?keys=voltage,activePower')
            v = (nowms-ts['voltage'][0]['ts'])//1000 if ts.get('voltage') else '?'
            ap = (nowms-ts['activePower'][0]['ts'])//1000 if ts.get('activePower') else '?'
            line += f' | {k}:V={v}/P={ap}'
        print(line, flush=True)
        time.sleep(120)

if __name__ == '__main__':
    main()
