#!/usr/bin/env python3
"""Test Object 33001 become_child (/33001/0/1) on ONE node against pi4 edge.
Reversible (become_router re-promotes). Proves the execute path works."""
from __future__ import annotations
import sys, time
import fleet_common as fc
fc.bootstrap_venv(); sys.path.insert(0, str(fc.TOOLS_DIR))
from ota_push_direct import Edge

TARGET = "ami-esp32c6-f854"   # a confirmed Child
e = Edge("192.168.1.111", 8090, fc.EDGE_TENANT_USER, fc.EDGE_TENANT_PASS)

d = e.s.get(f"{e.base}/api/tenant/devices?pageSize=1&page=0&textSearch={TARGET}", timeout=15).json()
if not d["data"]:
    print(f"{TARGET} not found"); sys.exit(1)
did = d["data"][0]["id"]["id"]
print(f"target: {TARGET}  id={did}")

def dec(v):
    if not v: return v
    try:
        return bytes.fromhex(v).decode(errors="replace").rstrip("\x00").strip()
    except Exception:
        return v

def role():
    try: return dec(e.read_str(did, "/33001/0/4"))
    except Exception as ex: return f"ERR {str(ex)[:40]}"
def elig():
    try: return e.read_str(did, "/33001/0/5")
    except Exception as ex: return f"ERR {str(ex)[:40]}"

print(f"\nBEFORE: role={role()!r}  is_router_eligible(raw)={elig()!r}")
print("\nexecute /33001/0/1 (become_child)...")
try:
    r = e.rpc(did, "Execute", {"id": "/33001/0/1"}, timeout_ms=15000)
    print("  rpc result:", r)
except Exception as ex:
    print("  rpc ERR:", ex)
time.sleep(4)
print(f"\nAFTER: role={role()!r}  is_router_eligible(raw)={elig()!r}")
print("\n(reversible with Execute /33001/0/0 become_router)")
