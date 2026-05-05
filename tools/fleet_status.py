#!/usr/bin/env python3
"""Fleet audit: list every ami-esp32c6-* device known to TB Edge.

Output columns:
  endpoint | active | last seen (sensor) | top sensor values

Useful to verify a deployment session afterwards or to spot drifters.

Examples:
    python tools/fleet_status.py
    python tools/fleet_status.py --since-hours 1
    python tools/fleet_status.py --csv fleet_status.csv
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys

import fleet_common as fc

fc.bootstrap_venv()

sys.path.insert(0, str(fc.TOOLS_DIR))
from provision_node import TBClient  # noqa: E402

PREFIX = "ami-esp32c6-"
INTERESTING = ["voltage", "current", "frequency", "activePower"]


def _ts_to_iso(ms: int) -> str:
    if not ms:
        return ""
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat(timespec="seconds")


def _max_ts(tel: dict) -> int:
    out = 0
    for v in tel.values():
        if isinstance(v, list) and v:
            ts = v[0].get("ts", 0) if isinstance(v[0], dict) else 0
            out = max(out, int(ts or 0))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mesh", default=fc.DEFAULT_MESH, choices=fc.MESH_TARGETS,
                    help=f"Which TB Edge to query. Default: {fc.DEFAULT_MESH}")
    ap.add_argument("--host", default=None, help="Override TB Edge host (else derived from --mesh)")
    ap.add_argument("--port", type=int, default=None, help="Override TB Edge port (else derived from --mesh)")
    ap.add_argument("--user", default=fc.EDGE_TENANT_USER)
    ap.add_argument("--password", default=fc.EDGE_TENANT_PASS)
    ap.add_argument("--since-hours", type=float, default=0,
                    help="Only show devices with telemetry newer than N hours")
    ap.add_argument("--csv", default="", help="Optional CSV output path")
    args = ap.parse_args()

    mesh_host, mesh_port = fc.edge_for_mesh(args.mesh)
    if args.host is None:
        args.host = mesh_host
    if args.port is None:
        args.port = mesh_port
    print(f"[fleet_status] mesh={args.mesh}  Edge=http://{args.host}:{args.port}\n")

    tb = TBClient(args.host, args.port, args.user, args.password)
    tb.login()

    page = 0
    rows: list[dict] = []
    while True:
        data = tb._get("/api/tenant/deviceInfos", {"pageSize": 100, "page": page})
        items = data.get("data", [])
        for d in items:
            name = d.get("name", "")
            if not name.startswith(PREFIX):
                continue
            did = d["id"]["id"]
            tel = tb.get_latest_telemetry(did) or {}
            ts = _max_ts(tel)
            row = {
                "endpoint": name,
                "active": d.get("active"),
                "last_ts": _ts_to_iso(ts),
                "device_id": did,
            }
            for k in INTERESTING:
                v = tel.get(k)
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    row[k] = v[0].get("value")
                else:
                    row[k] = ""
            rows.append(row)
        if data.get("hasNext") is False or not items:
            break
        page += 1

    # filter by recency
    if args.since_hours > 0:
        cutoff_ms = int((dt.datetime.now(dt.timezone.utc).timestamp() - args.since_hours * 3600) * 1000)
        rows = [r for r in rows if r["last_ts"] and dt.datetime.fromisoformat(r["last_ts"]).timestamp() * 1000 >= cutoff_ms]

    rows.sort(key=lambda r: r["endpoint"])

    # console table
    cols = ["endpoint", "active", "last_ts"] + INTERESTING
    widths = {c: max(len(c), max((len(str(r.get(c, ""))) for r in rows), default=0)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    print("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))
    print(f"\nTotal: {len(rows)} ({sum(1 for r in rows if r['active'])} active)")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols + ["device_id"])
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, "") for c in cols + ["device_id"]})
        print(f"CSV written: {args.csv}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
