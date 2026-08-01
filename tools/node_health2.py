#!/usr/bin/env python3
"""Follow-up: current uptime + decoded reset-reason distribution + inactive list."""
from __future__ import annotations
import sys
import fleet_common as fc
fc.bootstrap_venv(); sys.path.insert(0, str(fc.TOOLS_DIR))
from ota_push_direct import Edge

# Zephyr hwinfo RESET_* bit meanings
RST = {1: "PIN", 2: "SOFTWARE", 4: "BROWNOUT", 8: "POR", 16: "WATCHDOG",
       32: "DEBUG", 64: "SECURITY", 128: "LOWPWR_WAKE", 256: "CPU_LOCKUP"}
def rst_name(v):
    try: v = int(float(v))
    except Exception: return "?"
    parts = [n for b, n in RST.items() if v & b]
    return "+".join(parts) or f"0x{v:x}"

PREFIX = "ami-esp32c6-"
e = Edge("192.168.1.111", 8090, fc.EDGE_TENANT_USER, fc.EDGE_TENANT_PASS)
devs, page = [], 0
while True:
    d = e.s.get(f"{e.base}/api/tenant/deviceInfos", params={"pageSize": 100, "page": page}, timeout=20).json()
    devs += [x for x in d.get("data", []) if x.get("name", "").startswith(PREFIX)]
    if not d.get("hasNext"): break
    page += 1
active = [x for x in devs if x.get("active")]
inactive = [x for x in devs if not x.get("active")]

def gi(v):
    try: return int(float(v))
    except Exception: return -1

# uptime + reset reason for active
ups = []; rst_dist = {}
for x in active:
    did = x["id"]["id"]; suf = x["name"][len(PREFIX):]
    ts = e.s.get(f"{e.base}/api/plugins/telemetry/DEVICE/{did}/values/timeseries",
                 params={"keys": "uptime_s,last_reset_reason"}, timeout=15).json()
    up = gi(ts.get("uptime_s", [{}])[0].get("value")) if ts.get("uptime_s") else -1
    rr = ts.get("last_reset_reason", [{}])[0].get("value") if ts.get("last_reset_reason") else None
    ups.append((suf, up)); nm = rst_name(rr); rst_dist[nm] = rst_dist.get(nm, 0) + 1

ups.sort(key=lambda t: t[1])
print("== current uptime (lowest first) ==")
for suf, up in ups[:15]:
    h = up/3600 if up >= 0 else -1
    print(f"  {suf:6} uptime={up:>8}s ({h:.1f}h)")
alive = [u for _, u in ups if u >= 0]
if alive:
    print(f"\n  uptime: min={min(alive)}s  median={sorted(alive)[len(alive)//2]}s  max={max(alive)}s ({max(alive)/3600:.1f}h)")
    print(f"  nodes uptime <1h: {sum(1 for u in alive if u < 3600)}   <6h: {sum(1 for u in alive if u < 21600)}   >24h: {sum(1 for u in alive if u > 86400)}")

print(f"\n== last_reset_reason distribution (active nodes) ==")
for nm, c in sorted(rst_dist.items(), key=lambda t: -t[1]):
    print(f"  {nm:20} {c}")

print(f"\n== {len(inactive)} INACTIVE nodes ==")
for x in sorted(inactive, key=lambda d: d["name"]):
    print(f"  {x['name'][len(PREFIX):]}")
