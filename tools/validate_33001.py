#!/usr/bin/env python3
"""Validate Object 33001 (Thread Role Control) RPC path against the pi4 edge.
Reads current_role (/33001/0/4) + is_router_eligible (/33001/0/5) for active nodes.
Read-only — makes NO changes."""
from __future__ import annotations
import sys
import fleet_common as fc
fc.bootstrap_venv(); sys.path.insert(0, str(fc.TOOLS_DIR))
from ota_push_direct import Edge

PREFIX = "ami-esp32c6-"
e = Edge("192.168.1.111", 8090, fc.EDGE_TENANT_USER, fc.EDGE_TENANT_PASS)

devs, page = [], 0
while True:
    d = e.s.get(f"{e.base}/api/tenant/deviceInfos",
                params={"pageSize": 100, "page": page}, timeout=20).json()
    devs += [x for x in d.get("data", []) if x.get("name", "").startswith(PREFIX) and x.get("active")]
    if not d.get("hasNext"): break
    page += 1
print(f"[validate] {len(devs)} active nodes")

roles = {"Router": [], "Leader": [], "Child": [], "?": [], "other": []}
n_test = 0
for x in devs:
    suf = x["name"][len(PREFIX):]
    try:
        role = (e.read_str(x["id"]["id"], "/33001/0/4") or "?").rstrip("\x00").strip() or "?"
    except Exception as ex:
        role = f"ERR:{str(ex)[:30]}"
    n_test += 1
    if role in roles: roles[role].append(suf)
    elif role == "?": roles["?"].append(suf)
    else: roles["other"].append(f"{suf}={role}")
    if n_test >= 20:  # sample first 20 to validate the path quickly
        break

print(f"\n[validate] sampled {n_test} nodes via /33001/0/4:")
for k in ("Router", "Leader", "Child", "?", "other"):
    if roles[k]:
        print(f"  {k:8}: {len(roles[k])}  {roles[k][:12]}")
