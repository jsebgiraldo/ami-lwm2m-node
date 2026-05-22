#!/usr/bin/env python3
"""Census every INTERVAL seconds for DURATION; log the down-set each round so we
can see whether it ROTATES (intermittent firmware/RF) or stays FIXED (specific
nodes / power / placement). Writes tools/rotation_watch.csv (ts,active,down_set).

Usage: python tools/rotation_watch.py [--interval 300] [--duration 7200]
"""
from __future__ import annotations
import argparse, csv, datetime as dt, sys, time
import fleet_common as fc
fc.bootstrap_venv()
sys.path.insert(0, str(fc.TOOLS_DIR))
from provision_node import TBClient  # noqa: E402

PREFIX = "ami-esp32c6-"


def census(tb):
    active, down, page = [], [], 0
    while True:
        data = tb._get("/api/tenant/deviceInfos", {"pageSize": 100, "page": page})
        for d in data.get("data", []):
            nm = d.get("name", "")
            if not nm.startswith(PREFIX):
                continue
            (active if d.get("active") else down).append(nm[len(PREFIX):])
        if not data.get("hasNext"):
            break
        page += 1
    return sorted(active), sorted(down)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--duration", type=int, default=7200)
    ap.add_argument("--csv", default="tools/rotation_watch.csv")
    a = ap.parse_args()

    host, port = fc.edge_for_mesh(fc.DEFAULT_MESH)
    tb = TBClient(host, port, fc.EDGE_TENANT_USER, fc.EDGE_TENANT_PASS)
    tb.login()

    end = time.time() + a.duration
    prev_down = None
    seen_down = set()
    with open(a.csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts_iso", "n_active", "n_down", "down_set", "newly_down", "recovered"])
        while time.time() < end:
            ts = dt.datetime.now().isoformat(timespec="seconds")
            try:
                active, down = census(tb)
            except Exception as e:
                print(f"{ts} census error: {e}", flush=True)
                time.sleep(a.interval)
                continue
            dset = set(down)
            newly = sorted(dset - (prev_down or set()))
            recov = sorted((prev_down or set()) - dset)
            seen_down |= dset
            w.writerow([ts, len(active), len(down), "|".join(down),
                        "|".join(newly), "|".join(recov)])
            f.flush()
            print(f"{ts}  active={len(active)} down={len(down)} "
                  f"[{','.join(down)}]  new={newly} recov={recov}  "
                  f"distinct_down_so_far={len(seen_down)}", flush=True)
            prev_down = dset
            if time.time() < end:
                time.sleep(a.interval)
    print(f"\nDONE. Distinct nodes that went down at least once: "
          f"{len(seen_down)} -> {sorted(seen_down)}", flush=True)
    if len(seen_down) > 6:
        print("=> ROTATING down-set (many distinct nodes) -> intermittent "
              "firmware/RF, NOT fixed power.", flush=True)
    else:
        print("=> Mostly FIXED down-set -> suspect specific nodes / "
              "power / placement.", flush=True)


if __name__ == "__main__":
    main()
