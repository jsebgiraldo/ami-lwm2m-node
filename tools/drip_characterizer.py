#!/usr/bin/env python3
"""Characterize the telemetry DRIP — how long boards stay stale before self-recovering.

Context: after the two reboot bugs were fixed (v0.7.3-kafix keepalive inversion,
v0.7.5-live HW-liveness starvation), the 30-board fleet stops rebooting (resets
frozen for hours) but only ~19/30 push telemetry at any instant. Boards stay UP
(uptime climbs for hours) yet periodically go silent for a window, then recover.
The aggr config (faster keepalive/liveness) did NOT stop it -> drip is control-
plane reachability (mesh path / Edge round-trip), not firmware resource/reboot.

A naive notify-liveness watchdog would REBOOT boards that were about to recover on
their own. To size the threshold correctly we need the DRIP-DURATION DISTRIBUTION.

This polls every board's uptime_s TIMESTAMP from TB (server-side, zero mesh load)
and logs, per board, each FRESH->STALE->FRESH transition with the stale duration.
Output is selective (only transitions + periodic histogram), safe for a Monitor.

  FRESH  = uptime_s pushed within STALE_S (normal cadence ~60s pmax)
  STALE  = no uptime_s push for >= STALE_S  (a drip window has begun)

Usage:  python tools/drip_characterizer.py [poll_s=45] [stale_s=200]
"""
import json, sys, time, urllib.request

TB = 'http://192.168.8.111:8090'
POLL_S = int(sys.argv[1]) if len(sys.argv) > 1 else 45
STALE_S = int(sys.argv[2]) if len(sys.argv) > 2 else 200


def login():
    r = urllib.request.Request(TB + '/api/auth/login',
        data=json.dumps({'username': 'tenant@thingsboard.org', 'password': 'tenant'}).encode(),
        headers={'Content-Type': 'application/json'}, method='POST')
    return json.loads(urllib.request.urlopen(r, timeout=15).read())['token']


def devices(tok):
    d = json.loads(urllib.request.urlopen(urllib.request.Request(
        TB + '/api/tenant/devices?pageSize=300&page=0',
        headers={'X-Authorization': f'Bearer {tok}'}), timeout=15).read())['data']
    return {x['name']: x['id']['id'] for x in d if x['name'].startswith('ami-esp32c6-')}


def age_s(tok, did):
    """Seconds since uptime_s was last pushed, or None if no data / error."""
    u = TB + f'/api/plugins/telemetry/DEVICE/{did}/values/timeseries?keys=uptime_s'
    try:
        v = json.loads(urllib.request.urlopen(urllib.request.Request(
            u, headers={'X-Authorization': f'Bearer {tok}'}), timeout=12).read() or b'{}')
        arr = v.get('uptime_s')
        if not arr:
            return None
        return (int(time.time() * 1000) - arr[0]['ts']) / 1000.0
    except Exception:
        return None


def main():
    tok = login()
    devs = devices(tok)
    print(f"[drip] {len(devs)} boards | poll={POLL_S}s stale>={STALE_S}s | server-side, no mesh load", flush=True)
    stale_since = {}          # name -> wall time the current drip began
    drips = []                # completed drip durations (s)
    tick = 0
    while True:
        tick += 1
        if tick % 15 == 0:
            try:
                tok = login(); devs = devices(tok)
            except Exception:
                pass
        fresh = 0
        now = time.time()
        for name, did in sorted(devs.items()):
            a = age_s(tok, did)
            short = name.split('-')[-1]
            is_stale = (a is None) or (a >= STALE_S)
            if not is_stale:
                fresh += 1
                if short in stale_since:                     # FRESH again -> drip ended
                    dur = now - stale_since.pop(short)
                    drips.append(dur)
                    print(f"[{time.strftime('%H:%M:%S')}] {short} RECOVERED after {dur:.0f}s drip", flush=True)
            else:
                if short not in stale_since:                  # new drip begins
                    stale_since[short] = now - (a if a else STALE_S)
                    print(f"[{time.strftime('%H:%M:%S')}] {short} DRIP start (age={a if a else 'no-data'})", flush=True)
        # periodic distribution summary
        if tick % 8 == 0:
            ongoing = len(stale_since)
            if drips:
                ds = sorted(drips)
                p50 = ds[len(ds)//2]; p90 = ds[int(len(ds)*0.9)]; mx = ds[-1]
                print(f"[{time.strftime('%H:%M')} sum] fresh={fresh}/{len(devs)} ongoing_drips={ongoing} "
                      f"| completed={len(ds)} dur p50={p50:.0f}s p90={p90:.0f}s max={mx:.0f}s", flush=True)
            else:
                print(f"[{time.strftime('%H:%M')} sum] fresh={fresh}/{len(devs)} ongoing_drips={ongoing} | no completed drips yet", flush=True)
        time.sleep(POLL_S)


if __name__ == '__main__':
    main()
