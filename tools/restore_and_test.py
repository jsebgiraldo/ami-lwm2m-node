#!/usr/bin/env python3
"""Restore device profile to baseline and verify."""
import urllib.request, json, ssl, time

EDGE = "http://192.168.1.111:8090"
PROFILE_ID = "b6d55c90-12db-11f1-b535-433a231637c4"
DEVICE_ID = "cc9da070-135b-11f1-80f9-cdb955f2c365"

GRUPO1 = ["/10242_1.0/0/10", "/10242_1.0/0/11", "/10242_1.0/0/34", "/10242_1.0/0/35"]

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def api(path, method="GET", data=None, token=None):
    url = f"{EDGE}{path}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Authorization"] = f"Bearer {token}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else {}

# Login
token = api("/api/auth/login", "POST", {"username": "tenant@thingsboard.org", "password": "tenant"})["token"]

# Get profile
profile = api(f"/api/deviceProfile/{PROFILE_ID}", token=token)
tc = profile["profileData"]["transportConfiguration"]
attr_lwm2m = tc["observeAttr"]["attributeLwm2m"]

print("ANTES:")
for p in sorted(attr_lwm2m.keys())[:4]:
    v = attr_lwm2m[p]
    print(f"  {p}: pmin={v.get('pmin')}, pmax={v.get('pmax')}")

# Set baseline
for p in attr_lwm2m:
    if p in GRUPO1:
        attr_lwm2m[p] = {"pmin": 15, "pmax": 30}
    else:
        attr_lwm2m[p] = {"pmin": 60, "pmax": 300}

tc["observeAttr"]["attributeLwm2m"] = attr_lwm2m
profile["profileData"]["transportConfiguration"] = tc
api("/api/deviceProfile", "POST", profile, token=token)
print("\nPerfil restaurado a baseline (Grupo1: 15/30, Grupo2: 60/300)")

# Quick test: set 1s on voltage only, wait, and check telemetry rate
import sys
if "--test-1s" in sys.argv:
    print("\n=== TEST: Configurando pmin=1/pmax=1 para voltage ===")
    profile = api(f"/api/deviceProfile/{PROFILE_ID}", token=token)
    tc = profile["profileData"]["transportConfiguration"]
    attr_lwm2m = tc["observeAttr"]["attributeLwm2m"]
    for p in attr_lwm2m:
        attr_lwm2m[p] = {"pmin": 1, "pmax": 1}
    tc["observeAttr"]["attributeLwm2m"] = attr_lwm2m
    profile["profileData"]["transportConfiguration"] = tc
    api("/api/deviceProfile", "POST", profile, token=token)
    print("  Perfil actualizado a 1s")

    # Wait for observe re-negotiation
    print("  Esperando 90s para re-negociacion de observe...")
    time.sleep(90)

    # Check reception rate
    end_ts = int(time.time() * 1000)
    start_ts = end_ts - 60000
    data = api(
        f"/api/plugins/telemetry/DEVICE/{DEVICE_ID}/values/timeseries"
        f"?keys=voltage&startTs={start_ts}&endTs={end_ts}&limit=500&orderBy=ASC",
        token=token,
    )
    samples = data.get("voltage", [])
    print(f"\n  voltage: {len(samples)} muestras en 60s")
    if len(samples) >= 2:
        ts_list = sorted([s["ts"] for s in samples])
        deltas = [(ts_list[i+1] - ts_list[i])/1000 for i in range(len(ts_list)-1)]
        avg_iat = sum(deltas) / len(deltas)
        min_iat = min(deltas)
        max_iat = max(deltas)
        print(f"  IAT: min={min_iat:.1f}s, avg={avg_iat:.1f}s, max={max_iat:.1f}s")
        print(f"  Ultimas 10 muestras:")
        for s in samples[-10:]:
            ts_str = time.strftime("%H:%M:%S", time.localtime(s["ts"] / 1000))
            print(f"    {ts_str} = {s['value']}")
    
    # Restore baseline after test
    profile = api(f"/api/deviceProfile/{PROFILE_ID}", token=token)
    tc = profile["profileData"]["transportConfiguration"]
    attr_lwm2m = tc["observeAttr"]["attributeLwm2m"]
    for p in attr_lwm2m:
        if p in GRUPO1:
            attr_lwm2m[p] = {"pmin": 15, "pmax": 30}
        else:
            attr_lwm2m[p] = {"pmin": 60, "pmax": 300}
    tc["observeAttr"]["attributeLwm2m"] = attr_lwm2m
    profile["profileData"]["transportConfiguration"] = tc
    api("/api/deviceProfile", "POST", profile, token=token)
    print("\n  Perfil restaurado a baseline")
