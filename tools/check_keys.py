#!/usr/bin/env python3
"""Quick script to check all TB Edge telemetry keys and latest values."""
import json, urllib.request, time

TB = "http://192.168.1.111:8090"
DEV_ID = "cc9da070-3b28-11f0-9e66-e30e147e8748"

# Login
data = json.dumps({"username": "tenant@thingsboard.org", "password": "tenant"}).encode()
req = urllib.request.Request(TB + "/api/auth/login", data=data, headers={"Content-Type": "application/json"})
token = json.loads(urllib.request.urlopen(req, timeout=10).read())["token"]
h = {"X-Authorization": "Bearer " + token}

# Get keys
req = urllib.request.Request(TB + f"/api/plugins/telemetry/DEVICE/{DEV_ID}/keys/timeseries", headers=h)
keys = json.loads(urllib.request.urlopen(req).read())
print(f"Total keys: {len(keys)}")
print(f"Keys: {sorted(keys)}\n")

# Get latest values
keys_str = ",".join(keys)
req = urllib.request.Request(TB + f"/api/plugins/telemetry/DEVICE/{DEV_ID}/values/timeseries?keys={keys_str}", headers=h)
ts = json.loads(urllib.request.urlopen(req).read())

now_ms = int(time.time() * 1000)
print(f"{'Key':30s} {'Value':>15s}  {'Age':>10s}")
print("-" * 60)
for k in sorted(ts):
    if k == "transportLog":
        print(f"{'transportLog':30s} {'(log entry)':>15s}  {'':>10s}")
        continue
    val = ts[k][0]["value"]
    age_min = (now_ms - int(ts[k][0]["ts"])) / 60000
    print(f"{k:30s} {val:>15s}  {age_min:>8.1f} min")

# Also get device attributes
print("\n--- Client Attributes ---")
req = urllib.request.Request(TB + f"/api/plugins/telemetry/DEVICE/{DEV_ID}/values/attributes/CLIENT_SCOPE", headers=h)
attrs = json.loads(urllib.request.urlopen(req).read())
for a in sorted(attrs, key=lambda x: x["key"]):
    print(f"  {a['key']:30s} = {a['value']}")

print("\n--- Shared Attributes ---")
req = urllib.request.Request(TB + f"/api/plugins/telemetry/DEVICE/{DEV_ID}/values/attributes/SHARED_SCOPE", headers=h)
attrs = json.loads(urllib.request.urlopen(req).read())
for a in sorted(attrs, key=lambda x: x["key"]):
    print(f"  {a['key']:30s} = {a['value']}")
