#!/usr/bin/env python3
"""Fleet OTA driver — push an OTA image to many nodes over the air via the
proven Object-5 WriteReplace path (no USB, no TB campaign engine).

Per node: oneway WriteReplace /5/0/0 (hex image) -> poll /5/0/3 until
Downloaded(2); the firmware AUTO-APPLIES (swap+reboot) on Downloaded, so we
then poll /3/0/3 until it == target. Execute /5/0/2 is sent as a fallback in
case a node doesn't auto-apply (harmless METHOD_NOT_ALLOWED if it already did).

Nodes are processed in BATCHES (default 3): the image is pushed to the whole
batch first (oneway returns instantly), then all download concurrently while we
poll. Failures are retried once at the end. Marginal/brownout-prone nodes that
time out are reported, NOT bricked (they stay on the old image).

Usage:
  python tools/ota_fleet.py --version 0.6.34                 # all active not-yet-on-target
  python tools/ota_fleet.py --version 0.6.34 --batch 3
  python tools/ota_fleet.py --version 0.6.34 --only c144,d2b4 # specific nodes
  python tools/ota_fleet.py --version 0.6.34 --dry-run
"""
from __future__ import annotations
import argparse, sys, time
import fleet_common as fc
fc.bootstrap_venv(); sys.path.insert(0, str(fc.TOOLS_DIR))
from ota_push_direct import Edge, EDGE_HOST, EDGE_PORT, USER, PASS, DEFAULT_BIN
from pathlib import Path

PREFIX = "ami-esp32c6-"


def active_targets(e, version, only, exclude):
    devs, page = [], 0
    while True:
        d = e.s.get(f"{e.base}/api/tenant/deviceInfos",
                    params={"pageSize": 100, "page": page}, timeout=20).json()
        devs += [x for x in d.get("data", []) if x.get("name", "").startswith(PREFIX)]
        if not d.get("hasNext"):
            break
        page += 1
    out = []
    for x in devs:
        suf = x["name"][len(PREFIX):]
        if only and suf not in only:
            continue
        if suf in exclude:
            continue
        if not x.get("active"):
            print(f"  skip {suf}: inactive")
            continue
        out.append((suf, x["name"], x["id"]["id"]))
    return out


def push_one(e, did, hexval):
    # All wrapped: a transient Edge RPC read-timeout must NOT crash the run.
    try:
        e.rpc(did, "WriteReplace", {"id": "/5/0/1", "value": ""}, timeout_ms=10000)  # reset Obj5
    except Exception as ex:
        print(f"    (reset Obj5 timeout: {str(ex)[:40]} — continuing)", flush=True)
    time.sleep(1)
    try:
        e.rpc(did, "WriteReplace", {"id": "/5/0/0", "value": hexval}, oneway=True, timeout_ms=600000)
        return True
    except Exception as ex:
        print(f"    (push timeout: {str(ex)[:40]})", flush=True)
        return False


def do_batch(e, batch, hexval, version, dl_timeout, t):
    print(f"\n=== batch: {', '.join(s for s, _, _ in batch)} ===", flush=True)
    for suf, _, did in batch:
        push_one(e, did, hexval)
        print(f"  [{t()}] {suf}: image pushed (oneway)", flush=True)
    done, pending = {}, {s: did for s, _, did in batch}
    deadline = time.time() + dl_timeout
    executed = set()
    while pending and time.time() < deadline:
        time.sleep(15)
        for suf in list(pending):
            did = pending[suf]
            try:
                fw = e.read_str(did, "/3/0/3")
                if fw == version:
                    done[suf] = "OK"; del pending[suf]
                    print(f"  [{t()}] {suf}: -> {version} OK", flush=True); continue
                st = e.read_int(did, "/5/0/3")
                if st == 2 and suf not in executed:  # Downloaded -> nudge Execute (fallback)
                    e.rpc(did, "Execute", {"id": "/5/0/2"}, timeout_ms=10000)
                    executed.add(suf)
                    print(f"  [{t()}] {suf}: Downloaded, Execute sent", flush=True)
                else:
                    print(f"  [{t()}] {suf}: state={st} fw={fw!r}", flush=True)
            except Exception as ex:
                print(f"  [{t()}] {suf}: poll err {str(ex)[:40]}", flush=True)
    for suf in pending:
        done[suf] = "TIMEOUT"
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--bin", default=str(DEFAULT_BIN))
    ap.add_argument("--batch", type=int, default=1,
                    help="nodes per batch. KEEP AT 1: concurrent downloads "
                         "starve the slow ones on the ~250kbps mesh and overload "
                         "the Edge RPC. Sequential is slower (~8-10min/node) but reliable.")
    ap.add_argument("--dl-timeout", type=int, default=720, help="per-batch seconds (~12min/node)")
    ap.add_argument("--only", default="")
    ap.add_argument("--exclude", default="")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    data = Path(a.bin).read_bytes(); hexval = data.hex()
    print(f"[fleet-ota] image {Path(a.bin).name} size={len(data)}B target={a.version}")
    e = Edge(EDGE_HOST, EDGE_PORT, USER, PASS)
    only = set(s.strip() for s in a.only.split(",") if s.strip())
    exclude = set(s.strip() for s in a.exclude.split(",") if s.strip())
    targets = active_targets(e, a.version, only, exclude)

    # skip nodes already on target
    todo = []
    for suf, nm, did in targets:
        cur = ""
        try: cur = e.read_str(did, "/3/0/3")
        except Exception: pass
        if cur == a.version:
            print(f"  {suf}: already {a.version}, skip")
        else:
            todo.append((suf, nm, did))
    print(f"[fleet-ota] {len(todo)} node(s) to update: {', '.join(s for s,_,_ in todo)}")
    if a.dry_run or not todo:
        return

    t0 = time.time()
    t = lambda: f"{(time.time()-t0)/60:.1f}m"
    results = {}
    for i in range(0, len(todo), a.batch):
        results.update(do_batch(e, todo[i:i+a.batch], hexval, a.version, a.dl_timeout, t))

    # retry the timeouts once
    failed = [(s, n, d) for s, n, d in todo if results.get(s) != "OK"]
    if failed:
        print(f"\n[fleet-ota] retrying {len(failed)} failed: {', '.join(s for s,_,_ in failed)}")
        for i in range(0, len(failed), a.batch):
            results.update(do_batch(e, failed[i:i+a.batch], hexval, a.version, a.dl_timeout, t))

    ok = [s for s, r in results.items() if r == "OK"]
    bad = [s for s, r in results.items() if r != "OK"]
    print(f"\n[fleet-ota] DONE in {t()}.  OK {len(ok)}/{len(todo)}")
    if bad:
        print(f"[fleet-ota] still not on {a.version}: {', '.join(bad)} (old image intact, retry later)")


if __name__ == "__main__":
    main()
