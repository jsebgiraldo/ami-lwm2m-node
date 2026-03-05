"""Quick check: TB Edge + device status"""
import urllib.request, json

EDGE_URL = 'http://192.168.1.111:8090'
DEVICE_ID = 'cc9da070-135b-11f1-80f9-cdb955f2c365'

# Login
data = json.dumps({'username': 'tenant@thingsboard.org', 'password': 'tenant'}).encode()
req = urllib.request.Request(f'{EDGE_URL}/api/auth/login', data=data,
                             headers={'Content-Type': 'application/json'})
try:
    resp = urllib.request.urlopen(req, timeout=5)
    token = json.loads(resp.read())['token']
    print('TB Edge login OK')
except Exception as e:
    print(f'TB Edge login FAILED: {e}')
    exit(1)

# Check device
headers = {'X-Authorization': f'Bearer {token}'}
req2 = urllib.request.Request(f'{EDGE_URL}/api/device/{DEVICE_ID}', headers=headers)
try:
    resp2 = urllib.request.urlopen(req2, timeout=5)
    dev = json.loads(resp2.read())
    name = dev.get('name', '?')
    dtype = dev.get('type', '?')
    print(f'Device: {name} (type={dtype})')
except Exception as e:
    print(f'Device check FAILED: {e}')
    exit(1)

# Get latest telemetry
keys = 'voltage,frequency,radioSignalStrength,current,activePower'
req3 = urllib.request.Request(
    f'{EDGE_URL}/api/plugins/telemetry/DEVICE/{DEVICE_ID}/values/timeseries?keys={keys}',
    headers=headers)
try:
    resp3 = urllib.request.urlopen(req3, timeout=5)
    ts = json.loads(resp3.read())
    print(f'Telemetry keys found: {len(ts)}')
    for k in sorted(ts.keys()):
        v = ts[k]
        if v:
            val = v[0]['value']
            t = v[0]['ts']
            import time
            age = time.time() * 1000 - t
            print(f'  {k}: {val}  (age: {age/1000:.0f}s)')
        else:
            print(f'  {k}: no data')
except Exception as e:
    print(f'Telemetry check FAILED: {e}')
