#!/usr/bin/env python3
"""Aggregate per-node health from Object 33000 diagnostic telemetry on the pi4
edge. Flags nodes that reboot / re-register / trip the watchdog. Read-only."""
from __future__ import annotations
import sys
import fleet_common as fc
fc.bootstrap_venv(); sys.path.insert(0, str(fc.TOOLS_DIR))
from ota_push_direct import Edge

PREFIX = "ami-esp32c6-"
e = Edge("192.168.1.111", 8090, fc.EDGE_TENANT_USER, fc.EDGE_TENANT_PASS)

# active devices
devs, page = [], 0
while True:
    d = e.s.get(f"{e.base}/api/tenant/deviceInfos", params={"pageSize": 100, "page": page}, timeout=20).json()
    devs += [x for x in d.get("data", []) if x.get("name", "").startswith(PREFIX)]
    if not d.get("hasNext"): break
    page += 1
active = [x for x in devs if x.get("active")]
inactive = [x for x in devs if not x.get("active")]
print(f"total={len(devs)}  active={len(active)}  inactive={len(inactive)}")

# discover available keys on one active node
sample_id = active[0]["id"]["id"]
keys = e.s.get(f"{e.base}/api/plugins/telemetry/DEVICE/{sample_id}/keys/timeseries", timeout=15).json()
print(f"\nkeys available on {active[0]['name']}:\n  {keys}\n")

# candidate diagnostic keys (whatever exists)
WANT = ["total_resets", "uptime", "reg_attempts", "reg_success", "recover_count",
        "watchdog_count", "storm_backoff_applied", "last_reset_reason", "last_error_code",
        "notify_emitted", "notify_throttled", "boot_burst", "noreg_boots"]
present = [k for k in WANT if k in keys]
print(f"diagnostic keys present: {present}\n")

rows = []
for x in active:
    did = x["id"]["id"]; suf = x["name"][len(PREFIX):]
    ts = e.s.get(f"{e.base}/api/plugins/telemetry/DEVICE/{did}/values/timeseries",
                 params={"keys": ",".join(present)}, timeout=15).json()
    val = {k: (ts[k][0]["value"] if k in ts and ts[k] else None) for k in present}
    rows.append((suf, val))

def gi(v):
    try: return int(float(v))
    except Exception: return -1

# sort by total_resets desc
rows.sort(key=lambda r: gi(r[1].get("total_resets")), reverse=True)
print("== per-node diagnostics (sorted by total_resets desc) ==")
hdr = ["node", "resets", "uptime_s", "reg_a", "reg_ok", "recov", "wdog", "storm", "rst_rsn"]
print("  " + "  ".join(f"{h:>8}" for h in hdr))
for suf, v in rows:
    line = [suf, v.get("total_resets"), v.get("uptime"), v.get("reg_attempts"),
            v.get("reg_success"), v.get("recover_count"), v.get("watchdog_count"),
            v.get("storm_backoff_applied"), v.get("last_reset_reason")]
    print("  " + "  ".join(f"{str(c):>8}" for c in line))

# aggregate flags
hi_reset = [s for s, v in rows if gi(v.get("total_resets")) >= 50]
hi_recov = [s for s, v in rows if gi(v.get("recover_count")) >= 5]
hi_wdog  = [s for s, v in rows if gi(v.get("watchdog_count")) >= 3]
low_up   = [s for s, v in rows if 0 <= gi(v.get("uptime")) < 300]
print(f"\nFLAGS:")
print(f"  high resets (>=50): {hi_reset}")
print(f"  high recover (>=5): {hi_recov}")
print(f"  watchdog fired (>=3): {hi_wdog}")
print(f"  low uptime (<300s, recently rebooted): {low_up}")
