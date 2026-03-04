"""Quick check: device online + SSH to Edge."""
import urllib.request, json, time, sys

BASE = "http://192.168.1.111:8090"
DEV_ID = "cc9da070-135b-11f1-80f9-cdb955f2c365"

# 1. TB Edge login
body = json.dumps({"username": "tenant@thingsboard.org", "password": "tenant"}).encode()
req = urllib.request.Request(BASE + "/api/auth/login", data=body, headers={"Content-Type": "application/json"})
tok = json.loads(urllib.request.urlopen(req, timeout=10).read())["token"]
hdr = {"X-Authorization": f"Bearer {tok}"}
print("[OK] TB Edge login")

# 2. Device info
req2 = urllib.request.Request(BASE + f"/api/device/{DEV_ID}", headers=hdr)
dev = json.loads(urllib.request.urlopen(req2, timeout=10).read())
print(f"  Device: {dev['name']}")
print(f"  Profile ID: {dev['deviceProfileId']['id']}")

# 3. Latest telemetry
now = int(time.time() * 1000)
keys = "voltage,current,frequency,activePower,activeEnergy,apparentPower"
url = BASE + f"/api/plugins/telemetry/DEVICE/{DEV_ID}/values/timeseries?keys={keys}&startTs={now-120000}&endTs={now}"
req3 = urllib.request.Request(url, headers=hdr)
ts = json.loads(urllib.request.urlopen(req3, timeout=10).read())
active = 0
for k, v in sorted(ts.items()):
    if v:
        age = (now - v[0]["ts"]) / 1000
        print(f"  {k}: {v[0]['value']} ({age:.0f}s ago)")
        active += 1
    else:
        print(f"  {k}: no data last 2min")
print(f"[{'OK' if active else 'WARN'}] {active}/{len(ts)} keys with recent data")

# 4. SSH to Edge
try:
    import paramiko
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect("192.168.1.111", username="root", password="root", timeout=10)
    _, out, _ = ssh.exec_command("cat /proc/loadavg; free -m | head -2; cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo NA")
    print(f"[OK] SSH to Edge")
    for line in out.read().decode().strip().split("\n"):
        print(f"  {line}")
    ssh.close()
except Exception as e:
    print(f"[WARN] SSH failed: {e}")

print("\nReady to run benchmarks!" if active else "\nDevice may be offline - check Thread/LwM2M")
