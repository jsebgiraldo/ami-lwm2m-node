#!/usr/bin/env python3
"""Bulk recovery: execute become_child (/33001/0/1) on every active CHILD node
so it stops auto-promoting to Router (churn fix). Current Routers/Leaders are
kept. Reversible with become_router (/33001/0/0). Targets the pi4 edge."""
from __future__ import annotations
import sys, time, datetime
import fleet_common as fc
fc.bootstrap_venv(); sys.path.insert(0, str(fc.TOOLS_DIR))
from ota_push_direct import Edge

LOG = r"C:\Users\User\AppData\Local\Temp\claude\c--Users-User-Documents-UNAL-ami-lwm2m-node\96becc0d-3257-49c7-aceb-1accac57187d\scratchpad\bulk_demote.log"
def log(m):
    with open(LOG, "a", encoding="utf-8") as f: f.write(m + "\n")

PREFIX = "ami-esp32c6-"
e = Edge("192.168.1.111", 8090, fc.EDGE_TENANT_USER, fc.EDGE_TENANT_PASS)

devs, page = [], 0
while True:
    d = e.s.get(f"{e.base}/api/tenant/deviceInfos", params={"pageSize": 100, "page": page}, timeout=20).json()
    devs += [x for x in d.get("data", []) if x.get("name", "").startswith(PREFIX) and x.get("active")]
    if not d.get("hasNext"): break
    page += 1
log(f"=== BULK DEMOTE {datetime.datetime.now().strftime('%H:%M:%S')} ===  active nodes: {len(devs)}")

def dec(v):
    if v is None:
        return "?"
    s = str(v)
    tok = s.split()[0] if s.split() else s   # drop " type=OPAQUE" suffix if present
    try:
        s = bytes.fromhex(tok).decode(errors="replace")   # hex-encoded (pre model-upload)
    except Exception:
        pass
    return s.split("\x00")[0].strip()          # strip null padding -> "Child"/"Router"/"Leader"

routers, children, unknown = [], [], []
for x in devs:
    suf = x["name"][len(PREFIX):]; did = x["id"]["id"]
    try:
        role = dec(e.read_str(did, "/33001/0/4"))
    except Exception:
        role = "?"
    if role in ("Router", "Leader"): routers.append(suf)
    elif role == "Child": children.append((suf, did))
    else: unknown.append((suf, role))

log(f"routers KEPT ({len(routers)}): {sorted(routers)}")
log(f"children to demote: {len(children)}")
log(f"unknown/no-33001 ({len(unknown)}): {unknown}")

ok = err = 0
for suf, did in children:
    try:
        r = e.rpc(did, "Execute", {"id": "/33001/0/1"}, timeout_ms=15000)
        res = r.get("result") if isinstance(r, dict) else r
        if "CHANGED" in str(res) or "SUCCESS" in str(res):
            ok += 1
        else:
            err += 1; log(f"  {suf}: UNEXPECTED {r}")
    except Exception as ex:
        err += 1; log(f"  {suf}: ERR {str(ex)[:60]}")
    time.sleep(0.8)

log(f"=== DONE: demoted {ok}/{len(children)}  errors={err}  routers kept={len(routers)}  unknown={len(unknown)} ===")
print("DONE", ok, err)
