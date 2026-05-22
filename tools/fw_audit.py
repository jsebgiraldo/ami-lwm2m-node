#!/usr/bin/env python3
"""Audit firmware version across the fleet via LwM2M Read /3/0/3.

Lists every ami-esp32c6-* device, its active state, and (for active ones) the
Firmware Version it reports on Object 3/0/3. Flags any node not on --expect.

Usage: python tools/fw_audit.py --expect 0.6.33
"""
from __future__ import annotations
import argparse, sys
import fleet_common as fc
fc.bootstrap_venv(); sys.path.insert(0, str(fc.TOOLS_DIR))
from ota_push_direct import Edge  # reuse Read RPC + hex decode

PREFIX = "ami-esp32c6-"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect", default="0.6.33")
    a = ap.parse_args()
    host, port = fc.edge_for_mesh(fc.DEFAULT_MESH)
    e = Edge(host, port, fc.EDGE_TENANT_USER, fc.EDGE_TENANT_PASS)

    # full device list with active flag
    devs, page = [], 0
    while True:
        d = e.s.get(f"{e.base}/api/tenant/deviceInfos",
                    params={"pageSize": 100, "page": page}, timeout=20).json()
        devs += [x for x in d.get("data", []) if x.get("name", "").startswith(PREFIX)]
        if not d.get("hasNext"):
            break
        page += 1
    devs.sort(key=lambda x: x["name"])

    print(f"{'endpoint':<20} {'active':<7} {'fw /3/0/3':<12} note")
    print("-" * 56)
    inactive, mismatch, ok = [], [], 0
    for d in devs:
        nm, did, act = d["name"], d["id"]["id"], d.get("active")
        if not act:
            inactive.append(nm)
            print(f"{nm:<20} {'False':<7} {'-':<12} INACTIVE")
            continue
        try:
            fw = e.read_str(did, "/3/0/3") or "?"
        except Exception as ex:
            fw = f"err:{ex}"[:12]
        note = "OK" if fw == a.expect else "*** MISMATCH ***"
        if fw == a.expect:
            ok += 1
        else:
            mismatch.append((nm, fw))
        print(f"{nm:<20} {'True':<7} {fw:<12} {note}")

    print("-" * 56)
    print(f"Total {len(devs)} | active {len(devs)-len(inactive)} | "
          f"on {a.expect}: {ok} | mismatch: {len(mismatch)} | inactive: {len(inactive)}")
    if mismatch:
        print("MISMATCH:", ", ".join(f"{n}={v}" for n, v in mismatch))
    if inactive:
        print("INACTIVE:", ", ".join(inactive))


if __name__ == "__main__":
    main()
