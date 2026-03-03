#!/usr/bin/env python3
"""Quick check: what telemetry rate is the Edge actually receiving?"""
import urllib.request, json, ssl, time

EDGE = "http://192.168.1.111:8090"
DEVICE = "cc9da070-135b-11f1-80f9-cdb955f2c365"

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

body = json.dumps({"username": "tenant@thingsboard.org", "password": "tenant"}).encode()
req = urllib.request.Request(
    f"{EDGE}/api/auth/login", data=body,
    headers={"Content-Type": "application/json"}, method="POST",
)
resp = urllib.request.urlopen(req, timeout=10, context=ctx)
token = json.loads(resp.read())["token"]

end_ts = int(time.time() * 1000)
start_ts = end_ts - 120000  # last 120 seconds
keys = "voltage,current,frequency,activeEnergy,activePower"
path = (
    f"/api/plugins/telemetry/DEVICE/{DEVICE}/values/timeseries"
    f"?keys={keys}&startTs={start_ts}&endTs={end_ts}&limit=500&orderBy=ASC"
)
req2 = urllib.request.Request(
    f"{EDGE}{path}", headers={"X-Authorization": f"Bearer {token}"}
)
data = json.loads(urllib.request.urlopen(req2, timeout=10, context=ctx).read())

print("=== TELEMETRIA ULTIMOS 120s ===")
print(f"    Consultado a las {time.strftime('%H:%M:%S')}")
print()
for key in sorted(data.keys()):
    samples = data[key]
    print(f"  {key}: {len(samples)} muestras")
    if len(samples) >= 2:
        timestamps = [s["ts"] for s in samples]
        deltas = [(timestamps[i + 1] - timestamps[i]) / 1000 for i in range(len(timestamps) - 1)]
        avg_delta = sum(deltas) / len(deltas)
        min_delta = min(deltas)
        max_delta = max(deltas)
        print(f"    IAT: min={min_delta:.1f}s, avg={avg_delta:.1f}s, max={max_delta:.1f}s")
        print(f"    Ultimas 8 muestras:")
        for s in samples[-8:]:
            ts_str = time.strftime("%H:%M:%S", time.localtime(s["ts"] / 1000))
            print(f"      {ts_str} = {s['value']}")
    elif samples:
        ts_str = time.strftime("%H:%M:%S", time.localtime(samples[0]["ts"] / 1000))
        print(f"    {ts_str} = {samples[0]['value']}")
    else:
        print("    (sin datos)")
    print()

# Also check what the profile currently has
print("=== PERFIL OBSERVE ACTUAL ===")
prof_path = f"/api/deviceProfile/b6d55c90-12db-11f1-b535-433a231637c4"
req3 = urllib.request.Request(
    f"{EDGE}{prof_path}", headers={"X-Authorization": f"Bearer {token}"}
)
profile = json.loads(urllib.request.urlopen(req3, timeout=10, context=ctx).read())
tc = profile.get("profileData", {}).get("transportConfiguration", {})
attr_lwm2m = tc.get("observeAttr", {}).get("attributeLwm2m", {})
for p in sorted(attr_lwm2m.keys())[:6]:
    vals = attr_lwm2m[p]
    print(f"  {p}: pmin={vals.get('pmin')}, pmax={vals.get('pmax')}")
print(f"  ... ({len(attr_lwm2m)} paths total)")
