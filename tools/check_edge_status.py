#!/usr/bin/env python3
"""Quick check of device status on TB Edge."""
import requests, time, json, sys

TB_URL = "http://192.168.1.111:8090"
USER   = "tenant@thingsboard.org"
PASSW  = "tenant"

try:
    r = requests.post(f"{TB_URL}/api/auth/login",
                      json={"username": USER, "password": PASSW}, timeout=10)
    r.raise_for_status()
    token = r.json()["token"]
except Exception as e:
    print(f"ERROR: Cannot login to TB Edge: {e}")
    sys.exit(1)

h = {"X-Authorization": f"Bearer {token}"}

# List all devices
r = requests.get(f"{TB_URL}/api/tenant/devices?pageSize=50&page=0", headers=h, timeout=10)
r.raise_for_status()
data = r.json()
total = data.get("totalElements", "?")
devices = data.get("data", [])
print(f"=== TB Edge Devices ({total}) ===")

for dev in devices:
    dev_id = dev["id"]["id"]
    name = dev.get("name", "?")
    dtype = dev.get("type", "?")
    label = dev.get("label", "")
    print(f"\n--- {name} (type={dtype}) ---")
    print(f"  ID: {dev_id}")
    if label:
        print(f"  Label: {label}")

    # Attributes
    r2 = requests.get(
        f"{TB_URL}/api/plugins/telemetry/DEVICE/{dev_id}/values/attributes",
        headers=h, timeout=10)
    if r2.status_code == 200 and r2.text.strip():
        attrs = r2.json()
        if attrs:
            print(f"  Attributes ({len(attrs)}):")
            for a in sorted(attrs, key=lambda x: x["key"]):
                print(f"    {a['key']:30s} = {a['value']}")

    # Latest telemetry
    r3 = requests.get(
        f"{TB_URL}/api/plugins/telemetry/DEVICE/{dev_id}/values/timeseries",
        headers=h, timeout=10)
    if r3.status_code == 200 and r3.text.strip():
        ts = r3.json()
        if ts:
            now_ms = int(time.time() * 1000)
            print(f"  Telemetry ({len(ts)} keys):")
            for k in sorted(ts.keys()):
                vals = ts[k]
                if vals:
                    v = vals[0]
                    age_s = (now_ms - v["ts"]) / 1000
                    if age_s < 120:
                        age_str = f"{age_s:.0f}s ago"
                    elif age_s < 7200:
                        age_str = f"{age_s/60:.1f}min ago"
                    else:
                        age_str = f"{age_s/3600:.1f}h ago"
                    print(f"    {k:25s} = {str(v['value']):>12s}  ({age_str})")

print("\nDone.")
