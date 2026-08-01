#!/usr/bin/env python3
"""Upload the COMPLETE Object 33001 (Thread Role Control) model to the pi4 edge
resource library, then retry become_child on one node to see if the execute
resolves without an edge restart."""
from __future__ import annotations
import sys, time
from pathlib import Path
import fleet_common as fc
fc.bootstrap_venv(); sys.path.insert(0, str(fc.TOOLS_DIR))
from tb_edge_upload_models import login, upload_model
from ota_push_direct import Edge

host, port = fc.edge_for_mesh("pi4")
base = f"http://{host}:{port}"
xml = (Path(fc.REPO_ROOT) / "models" / "33001.xml").read_text(encoding="utf-8")

print(f"=== uploading 33001.xml to {base} ===")
s = login(host, port, fc.EDGE_TENANT_USER, fc.EDGE_TENANT_PASS)
upload_model(s, base, "Object 33001 v1.0 - Thread Role Control", "33001.xml", xml)
time.sleep(3)

print("\n=== retry become_child on ami-esp32c6-f854 (no restart yet) ===")
e = Edge(host, port, fc.EDGE_TENANT_USER, fc.EDGE_TENANT_PASS)
d = e.s.get(f"{e.base}/api/tenant/devices?pageSize=1&page=0&textSearch=ami-esp32c6-f854", timeout=15).json()
did = d["data"][0]["id"]["id"]
try:
    r = e.rpc(did, "Execute", {"id": "/33001/0/1"}, timeout_ms=15000)
    print("  execute result:", r)
except Exception as ex:
    print("  execute ERR:", ex)
