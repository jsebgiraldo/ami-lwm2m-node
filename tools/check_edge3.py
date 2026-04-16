"""Check LwM2M registration status and Edge network interfaces"""
import json
import urllib.request
import urllib.error

BASE = "http://192.168.1.111:8090"
TOKEN = None

def api(path, method="GET", data=None):
    url = BASE + path
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["X-Authorization"] = f"Bearer {TOKEN}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        print(f"  HTTP {e.code}: {body}")
        return None
    except Exception as e:
        print(f"  Error: {e}")
        return None

# Login
resp = api("/api/auth/login", "POST", {"username": "tenant@thingsboard.org", "password": "tenant"})
TOKEN = resp["token"]
print("Logged in OK\n")

DEVICE_ID = "cc9da070-135b-11f1-80f9-cdb955f2c365"

# 1. Device activity/connection status
print("=" * 60)
print("DEVICE CONNECTION STATUS")
print("=" * 60)

# Check if device is active (has recent activity)
dev = api(f"/api/device/{DEVICE_ID}")
if dev:
    print(f"  Name: {dev.get('name')}")
    print(f"  Created: {dev.get('createdTime')}")
    additional = dev.get("additionalInfo", {})
    if additional:
        print(f"  Additional info: {json.dumps(additional, indent=2)[:500]}")

# 2. Get device attributes (server-side, shared, client)
print(f"\n{'=' * 60}")
print("DEVICE ATTRIBUTES")
print(f"{'=' * 60}")
for scope in ["SERVER_SCOPE", "SHARED_SCOPE", "CLIENT_SCOPE"]:
    attrs = api(f"/api/plugins/telemetry/DEVICE/{DEVICE_ID}/values/attributes/{scope}")
    if attrs:
        print(f"\n  {scope}:")
        for a in attrs:
            key = a.get("key", "?")
            val = a.get("value", "?")
            ts = a.get("lastUpdateTs", 0)
            print(f"    {key} = {str(val)[:100]} (ts={ts})")
    else:
        print(f"\n  {scope}: none")

# 3. Latest telemetry
print(f"\n{'=' * 60}")
print("LATEST TELEMETRY")
print(f"{'=' * 60}")
telem = api(f"/api/plugins/telemetry/DEVICE/{DEVICE_ID}/values/timeseries")
if telem:
    for key, values in telem.items():
        if values:
            v = values[0]
            print(f"  {key} = {str(v.get('value','?'))[:80]} (ts={v.get('ts',0)})")
else:
    print("  No telemetry")

# 4. Get full device profile transport config
print(f"\n{'=' * 60}")
print("FULL LwM2M TRANSPORT CONFIG")
print(f"{'=' * 60}")
profile = api(f"/api/deviceProfile/b6d55c90-12db-11f1-b535-433a231637c4")
if profile and "profileData" in profile:
    tc = profile["profileData"].get("transportConfiguration", {})
    # Print key parts
    print(f"  Type: {tc.get('type')}")
    
    obs = tc.get("observeAttr", {})
    if obs:
        kn = obs.get("keyName", {})
        print(f"\n  Key Name mappings ({len(kn)}):")
        for path, name in sorted(kn.items()):
            print(f"    {path} -> {name}")
        
        observe = obs.get("observe", [])
        print(f"\n  Observe list ({len(observe)}):")
        for o in observe:
            print(f"    {o}")
        
        attr = obs.get("attribute", [])
        print(f"\n  Attribute list ({len(attr)}):")
        for a in attr:
            print(f"    {a}")
        
        telemetry = obs.get("telemetry", [])
        print(f"\n  Telemetry list ({len(telemetry)}):")
        for t in telemetry:
            print(f"    {t}")

print("\nDone.")
