from __future__ import annotations
import sys, time
import fleet_common as fc
fc.bootstrap_venv(); sys.path.insert(0, str(fc.TOOLS_DIR))
from ota_push_direct import Edge
e = Edge("192.168.1.111", 8090, fc.EDGE_TENANT_USER, fc.EDGE_TENANT_PASS)
for suf in ["ba64", "f768", "f6d4", "c5cc", "c600"]:
    d = e.s.get(f"{e.base}/api/tenant/devices?pageSize=1&page=0&textSearch=ami-esp32c6-{suf}", timeout=15).json()
    if not d["data"]:
        print(f"{suf}: not found"); continue
    did = d["data"][0]["id"]["id"]
    try:
        r = e.rpc(did, "Execute", {"id": "/33001/0/1"}, timeout_ms=15000)
        print(f"{suf}: {r.get('result') if isinstance(r,dict) else r}")
    except Exception as ex:
        print(f"{suf}: ERR {str(ex)[:60]}")
    time.sleep(1.5)
