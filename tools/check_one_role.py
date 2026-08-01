from __future__ import annotations
import sys
import fleet_common as fc
fc.bootstrap_venv(); sys.path.insert(0, str(fc.TOOLS_DIR))
from ota_push_direct import Edge
e = Edge("192.168.1.111", 8090, fc.EDGE_TENANT_USER, fc.EDGE_TENANT_PASS)
d = e.s.get(f"{e.base}/api/tenant/devices?pageSize=1&page=0&textSearch=ami-esp32c6-f854", timeout=15).json()
did = d["data"][0]["id"]["id"]
def dec(v):
    try: return bytes.fromhex(v.split()[0]).decode(errors="replace").rstrip("\x00").strip()
    except Exception: return v
role = e.read_str(did, "/33001/0/4")
elig = e.read_str(did, "/33001/0/5")
print(f"f854 AFTER become_child:  role={dec(role)!r}  eligible(raw)={elig!r}")
print("  (eligible raw '00' = false = will NOT auto-promote to router = churn stopped)")
