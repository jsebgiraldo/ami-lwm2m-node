"""Fleet-wide dynamic role audit + balance demo via TB Edge LwM2M RPC.

Reads /33001/0/4 (current_role) from every active board, then optionally
demotes routers that have few children (over-provisioned).

Usage:
  python tools/dynamic_role_audit.py audit         # read-only inventory
  python tools/dynamic_role_audit.py demote <LAB>  # Execute /33001/0/1 on one
  python tools/dynamic_role_audit.py promote <LAB> # Execute /33001/0/0 on one
"""
import csv
import json
import pathlib
import sys
import time
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
FLEET = REPO / "tools" / "fleet_map.csv"
BASE = "http://192.168.8.111:8090"


def api(t, p, m="GET", d=None, timeout=15):
    h = {"Content-Type": "application/json"}
    if t:
        h["X-Authorization"] = f"Bearer {t}"
    body = json.dumps(d).encode() if d else None
    r = urllib.request.Request(BASE + p, data=body, headers=h, method=m)
    try:
        return urllib.request.urlopen(r, timeout=timeout).read().decode()
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:
        return f"ERR: {e}"


def login():
    r = api(None, "/api/auth/login", "POST", {"username": "tenant@thingsboard.org", "password": "tenant"})
    return json.loads(r)["token"]


def list_devices(tok):
    return json.loads(api(tok, "/api/tenant/devices?pageSize=100&page=0"))["data"]


def rpc(tok, did, method, params, timeout_ms=5000):
    body = {"method": method, "params": params, "timeout": timeout_ms}
    return api(tok, f"/api/rpc/twoway/{did}", "POST", body)


def parse_value(resp):
    """Extract value=X from 'LwM2mSingleResource [id=N, value=V, type=T]'."""
    try:
        d = json.loads(resp)
        v = str(d.get("value", ""))
        if "value=" in v:
            seg = v.split("value=", 1)[1]
            return seg.split(",")[0].strip().strip("\x00").strip()
        return d.get("value")
    except Exception:
        return None


def fleet_map():
    out = {}
    with open(FLEET) as f:
        for r in csv.DictReader(f):
            out[r["endpoint"]] = r["label"]
    return out


def audit(tok):
    fmap = fleet_map()
    devs = list_devices(tok)
    rows = []
    print(f"Reading /33001/0/4 from all 30 boards via RPC...")
    for d in sorted(devs, key=lambda x: int(fmap.get(x["name"], "999")) if fmap.get(x["name"], "999").isdigit() else 999):
        if not d["name"].startswith("ami-esp32c6-"):
            continue
        lbl = fmap.get(d["name"], "?")
        if lbl in ("31", "32", "?"):
            continue
        did = d["id"]["id"]
        t0 = time.time()
        resp = rpc(tok, did, "Read", {"id": "/33001/0/4"}, 3000)
        dt = time.time() - t0
        role = parse_value(resp)
        # Read upgrade_thr too
        resp2 = rpc(tok, did, "Read", {"id": "/33001/0/2"}, 3000)
        thr = parse_value(resp2)
        ok = role and "HTTP" not in str(role)
        print(f"  L{lbl:>3} {d['name'][-10:]:>11}  role={role!s:<12} upgrade_thr={thr!s:<4} ({dt:.1f}s) {'OK' if ok else 'FAIL'}")
        rows.append({"lab": int(lbl) if lbl.isdigit() else 999, "did": did, "role": role, "upgrade_thr": thr})
    # Summary
    print()
    print("=== AGGREGATE ===")
    by_role = {}
    for r in rows:
        by_role.setdefault(str(r["role"]), []).append(r["lab"])
    for k, v in sorted(by_role.items()):
        print(f"  {k:<10} ({len(v):>2}): {sorted(v)}")


def execute(tok, label, action):
    fmap = fleet_map()
    target_ep = None
    for ep, lbl in fmap.items():
        if str(lbl) == str(label):
            target_ep = ep
            break
    if not target_ep:
        print(f"Lab {label} not found in fleet_map.csv")
        return
    devs = list_devices(tok)
    d = [d for d in devs if d["name"] == target_ep]
    if not d:
        print(f"Device {target_ep} not in TB Edge")
        return
    did = d[0]["id"]["id"]
    rid = {"demote": "/33001/0/1", "promote": "/33001/0/0"}[action]
    print(f"Executing {action} on Lab {label} (did={did}, rid={rid})...")
    resp = rpc(tok, did, "Execute", {"id": rid}, 5000)
    print(f"  -> {resp}")
    # Re-read after a delay
    print("  Re-reading role in 3s...")
    time.sleep(3)
    print(f"  current_role: {parse_value(rpc(tok, did, 'Read', {'id': '/33001/0/4'}, 3000))}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "audit"
    tok = login()
    if cmd == "audit":
        audit(tok)
    elif cmd in ("demote", "promote") and len(sys.argv) > 2:
        execute(tok, sys.argv[2], cmd)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
