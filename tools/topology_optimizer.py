#!/usr/bin/env python3
"""Thread mesh topology optimiser — picks which nodes should be Routers and
issues LwM2M Execute /33001/0/0 (become_router) or /33001/0/1 (become_child)
to make the live mesh match the chosen set.

Requires v0.6.38+ firmware on every node (all-FTD compile + Object 33001).

Strategy (simple first pass — tunable):
  1. Read /33001/0/4 (current_role) and Object 4 RSSI/LQI for every active node.
  2. Active routers = nodes currently role=Router or Leader.
  3. Targeted active-router count = ceil(N_active * --router-ratio) (default 20%).
  4. For each child, score = bestRSSI + LQI*0.1 (lower=worse link, candidate to
     promote a router near it).
  5. Promote: pick the worst-link children's neighbours-that-are-children with
     diverse MAC clusters (proxy for geographic diversity), up to target count.
  6. Demote: if current routers > target + slack, demote those with the best
     link quality (least useful as a router).

The optimiser is conservative: at most --max-promotes promotions and
--max-demotes demotions per run. Re-run periodically. OpenThread also has
its own router-selection-jitter (~120s) so changes settle.

Usage:
    python tools/topology_optimizer.py --dry-run               # what would I do
    python tools/topology_optimizer.py --router-ratio 0.25
    python tools/topology_optimizer.py --max-promotes 2 --max-demotes 0
"""
from __future__ import annotations
import argparse, math, sys
from collections import defaultdict
import fleet_common as fc
fc.bootstrap_venv(); sys.path.insert(0, str(fc.TOOLS_DIR))
from ota_push_direct import Edge

PREFIX = "ami-esp32c6-"


def read_node(e, did):
    """Returns dict with role, rssi, lqi, eligible, suf-based fields."""
    out = {}
    try: out["role"] = e.read_str(did, "/33001/0/4")
    except Exception: out["role"] = "?"
    # Object 4 (Connectivity Monitoring) — RSSI is /4/0/2, LQI /4/0/3 in our setup
    try: out["rssi"] = e.read_int(did, "/4/0/2")
    except Exception: out["rssi"] = None
    try: out["lqi"] = e.read_int(did, "/4/0/3")
    except Exception: out["lqi"] = None
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--router-ratio", type=float, default=0.20,
                    help="target fraction of active nodes that should be Routers (default 0.20)")
    ap.add_argument("--max-promotes", type=int, default=3)
    ap.add_argument("--max-demotes", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    host, port = fc.edge_for_mesh(fc.DEFAULT_MESH)
    e = Edge(host, port, fc.EDGE_TENANT_USER, fc.EDGE_TENANT_PASS)

    # Gather all active devices
    devs, page = [], 0
    while True:
        d = e.s.get(f"{e.base}/api/tenant/deviceInfos",
                    params={"pageSize": 100, "page": page}, timeout=20).json()
        devs += [x for x in d.get("data", []) if x.get("name", "").startswith(PREFIX) and x.get("active")]
        if not d.get("hasNext"): break
        page += 1
    print(f"[opt] {len(devs)} active nodes")

    info = {}
    for x in devs:
        suf = x["name"][len(PREFIX):]
        info[suf] = read_node(e, x["id"]["id"])
        info[suf]["did"] = x["id"]["id"]
    n = len(devs)

    routers = [s for s, v in info.items() if v["role"] in ("Router", "Leader")]
    children = [s for s, v in info.items() if v["role"] == "Child"]
    print(f"[opt] current: {len(routers)} routers / {len(children)} children")
    target = max(2, math.ceil(n * a.router_ratio))
    print(f"[opt] target routers: {target} (ratio {a.router_ratio})")

    # ----- pick promotions ------------------------------------------------
    # Score children by inverse link quality (poorer link first). Diversify by
    # MAC cluster (last 4-hex prefix) so we don't promote 3 neighbours.
    def cluster(suf): return suf[:2]
    cluster_has_router = {cluster(s) for s in routers}
    promote_pool = []
    for s in children:
        rssi = info[s]["rssi"] if info[s]["rssi"] is not None else -100
        lqi  = info[s]["lqi"]  if info[s]["lqi"]  is not None else 0
        # Lower link quality -> higher priority to promote a nearby router.
        # BUT only promote nodes with usable link (don't promote a dead one).
        if rssi < -85: continue
        promote_pool.append((s, rssi, lqi))
    # Sort by weakest first (worse link nearby = more benefit from promotion).
    promote_pool.sort(key=lambda t: (t[1] + t[2] * 0.1))

    to_promote = []
    if len(routers) < target:
        needed = target - len(routers)
        for s, rssi, lqi in promote_pool:
            if len(to_promote) >= min(needed, a.max_promotes): break
            c = cluster(s)
            if c in cluster_has_router: continue   # diversify
            to_promote.append(s)
            cluster_has_router.add(c)

    # ----- pick demotions -------------------------------------------------
    to_demote = []
    if len(routers) > target + 1:  # leave 1 slack
        # Demote routers with the BEST link (least value as router; their
        # children could attach to the BR or another router fine).
        scored = []
        for s in routers:
            r = info[s]["rssi"] if info[s]["rssi"] is not None else -100
            l = info[s]["lqi"]  if info[s]["lqi"]  is not None else 0
            scored.append((s, r + l * 0.1))
        scored.sort(key=lambda t: -t[1])  # best link first
        excess = len(routers) - target
        for s, _ in scored[:min(excess, a.max_demotes)]:
            to_demote.append(s)

    print(f"[opt] plan: promote {to_promote}  demote {to_demote}")
    if a.dry_run or (not to_promote and not to_demote):
        print("[opt] dry-run / no change")
        return

    # ----- act ------------------------------------------------------------
    for s in to_promote:
        try:
            r = e.rpc(info[s]["did"], "Execute",
                      {"id": "/33001/0/0"}, timeout_ms=15000)
            print(f"  promote {s}: {r}")
        except Exception as ex:
            print(f"  promote {s}: ERR {ex}")
    for s in to_demote:
        try:
            r = e.rpc(info[s]["did"], "Execute",
                      {"id": "/33001/0/1"}, timeout_ms=15000)
            print(f"  demote  {s}: {r}")
        except Exception as ex:
            print(f"  demote  {s}: ERR {ex}")
    print("[opt] OT router-selection-jitter ~120s — re-run later to see effect.")


if __name__ == "__main__":
    main()
