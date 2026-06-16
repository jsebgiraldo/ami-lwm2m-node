#!/usr/bin/env python3
"""Fase 0 — single-knob hypothesis test: force every active node to Child.

Hipotesis: v0.6.38 all-FTD default (30 nodos compitiendo por router-slot) es la
causa principal de la caida activos 26 -> 8-15. Si forzar Child estabiliza la
mesh y baja el panic loop, all-FTD es el culpable.

Mecanica:
  1. Write /33001/0/2 = 32 (router_upgrade_threshold maximo, desactiva
     auto-promote: el firmware tiene threshold=16 default -> al demote, los
     neighbors lo re-promueven en segundos. Esto es por que el "Execute returns
     CHANGED but role doesn't change" reportado como Bug #7).
  2. Execute /33001/0/1 (become_child) -> demote inmediato.
  3. Poll /33001/0/4 (current_role) a +30s/+60s para confirmar transicion.
  4. Snapshot t=+5/+10/+15 min: active_count, routers, neighbors.

Si despues de 15 min los 8 stay alive y los reset-loopers paran -> all-FTD es
la causa. Si los reset-loopers siguen -> es un %s residual o cambio del Layer 1,
no mesh churn.

Usage:
    python tools/phase0_force_sed.py --dry-run             # solo lista
    python tools/phase0_force_sed.py                       # ejecuta
    python tools/phase0_force_sed.py --threshold 32        # default 32 (max)
    python tools/phase0_force_sed.py --restore             # vuelve a threshold=16

Reversible: --restore vuelve threshold=16 en todos los nodos que tocamos.
"""
from __future__ import annotations
import argparse, csv, json, sys, time
from pathlib import Path

import requests

EDGE_HOST, EDGE_PORT = "192.168.8.111", 8090
USER, PASS = "tenant@thingsboard.org", "tenant"
PREFIX = "ami-esp32c6-"


class Edge:
    def __init__(self, host, port, user, password):
        self.base = f"http://{host}:{port}"
        self.s = requests.Session()
        r = self.s.post(f"{self.base}/api/auth/login",
                        json={"username": user, "password": password}, timeout=15)
        r.raise_for_status()
        self.s.headers.update({"X-Authorization": f"Bearer {r.json()['token']}"})

    def list_active_devices(self):
        out, page = [], 0
        while True:
            d = self.s.get(f"{self.base}/api/tenant/deviceInfos",
                           params={"pageSize": 100, "page": page}, timeout=20).json()
            for x in d.get("data", []):
                if x.get("name", "").startswith(PREFIX) and x.get("active"):
                    out.append({"name": x["name"], "did": x["id"]["id"]})
            if not d.get("hasNext"): break
            page += 1
        return out

    def rpc(self, did, method, params, oneway=False, timeout_ms=15000):
        kind = "oneway" if oneway else "twoway"
        try:
            r = self.s.post(f"{self.base}/api/rpc/{kind}/{did}",
                            json={"method": method, "params": params, "timeout": timeout_ms},
                            timeout=(timeout_ms / 1000 + 10))
            return r.json() if r.text else {}
        except Exception as e:
            return {"err": str(e)[:120]}

    def read_str(self, did, path):
        r = self.rpc(did, "Read", {"id": path})
        v = str(r.get("value", ""))
        if "value=" in v:
            raw = v.split("value=")[1].split(",")[0].split("]")[0].strip()
            try:
                return bytes.fromhex(raw).decode("ascii", "replace") if raw else ""
            except ValueError:
                return raw
        return v.strip()

    def read_int(self, did, path):
        r = self.rpc(did, "Read", {"id": path})
        v = r.get("value", "")
        if "value=" in str(v):
            try:
                return int(str(v).split("value=")[1].split(",")[0].split("]")[0])
            except ValueError:
                return None
        return None


def snapshot_mesh_ssh():
    """Returns (routers, neighbors, ts). Best-effort SSH to Pi4."""
    import subprocess
    try:
        out = subprocess.check_output(
            ["ssh", "-i", str(Path.home() / ".ssh/id_ed25519_suntek_ws"),
             "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
             "root@192.168.8.111",
             "curl -s http://localhost:9102/metrics | grep -E '^otbr_(routers|neighbors)_total'"],
            stderr=subprocess.DEVNULL, timeout=15).decode()
        r = n = None
        for ln in out.splitlines():
            if ln.startswith("otbr_routers_total"):
                r = int(float(ln.split()[1]))
            elif ln.startswith("otbr_neighbors_total"):
                n = int(float(ln.split()[1]))
        return r, n
    except Exception as e:
        return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore", action="store_true",
                    help="reset router_upgrade_threshold back to 16")
    ap.add_argument("--threshold", type=int, default=32, help="1..32 (default 32)")
    ap.add_argument("--monitor-min", type=int, default=15, help="post-action monitoring window minutes")
    ap.add_argument("--report", default="tools/phase0_report.csv")
    args = ap.parse_args()

    e = Edge(EDGE_HOST, EDGE_PORT, USER, PASS)
    devs = e.list_active_devices()
    if not devs:
        print("[fase0] no active devices found"); return 0
    print(f"[fase0] {len(devs)} active devices:")
    for d in devs: print(f"  - {d['name']}")

    # Pre snapshot
    r0, n0 = snapshot_mesh_ssh()
    print(f"[fase0] PRE   routers={r0} neighbors={n0} active_tb={len(devs)}")

    if args.dry_run:
        return 0

    # Snapshot per-node pre roles
    pre = {}
    for d in devs:
        role = e.read_str(d["did"], "/33001/0/4")
        pre[d["name"]] = role
        print(f"  pre  {d['name']}: role={role!r}")

    target_thr = 16 if args.restore else args.threshold
    print(f"[fase0] {'RESTORE' if args.restore else 'APPLY'} threshold={target_thr}, "
          f"{'become_child=skip' if args.restore else 'become_child=execute'}")

    # 1) Write threshold first (prevents instant auto-promote)
    for d in devs:
        r = e.rpc(d["did"], "Write", {"id": "/33001/0/2", "value": target_thr}, timeout_ms=10000)
        print(f"  thr  {d['name']}: {r}")

    if args.restore:
        print("[fase0] restore-only: not issuing become_child")
        return 0

    # 2) Execute become_child
    for d in devs:
        r = e.rpc(d["did"], "Execute", {"id": "/33001/0/1"}, timeout_ms=10000)
        print(f"  demote {d['name']}: {r}")

    # 3) Monitoring loop
    rows = []
    t_start = time.time()
    next_tick = 60
    while time.time() - t_start < args.monitor_min * 60:
        time.sleep(max(0, next_tick - (time.time() - t_start)))
        elapsed = int(time.time() - t_start)
        rr, nn = snapshot_mesh_ssh()
        active = e.list_active_devices()
        roles = {}
        for d in active:
            try: roles[d["name"]] = e.read_str(d["did"], "/33001/0/4")
            except Exception: roles[d["name"]] = "?"
        print(f"[t+{elapsed:4d}s] routers={rr} neighbors={nn} active_tb={len(active)}")
        for n, r in sorted(roles.items()):
            print(f"           {n}: {r!r}")
        rows.append({
            "t_s": elapsed, "routers": rr, "neighbors": nn,
            "active_tb": len(active),
            "roles": json.dumps(roles, ensure_ascii=False),
        })
        next_tick += 300 if elapsed > 60 else 60  # 60s once, then every 5 min

    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    with open(args.report, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["t_s", "routers", "neighbors", "active_tb", "roles"])
        w.writeheader(); w.writerows(rows)
    print(f"[fase0] report -> {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
