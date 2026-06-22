#!/usr/bin/env python3
"""AMI network SOAK scanner — sample the whole fleet + mesh every interval for a
fixed duration, then judge whether the network held steady (and therefore whether
scaling further is supported).

Tracks per sample:
  - streaming/N (delivery liveness)         - sum total_resets / watchdog / recover (stability: any cycling?)
  - routers / children (mesh structure)      - eidcache resolved/retry (address-resolution health)
  - TxErrCca (PHY channel saturation)        - TxDirectMaxRetryExpiry, Rx/TxTotal (congestion + traffic)

Connection-safe: TB REST (re-login per sample, no Postgres) + one SSH per sample
(closed after). Robust: a failed sample is logged and the loop continues.

Logs a CSV under soak_logs/ and prints each sample. At the end prints a STABILITY
VERDICT + a linear extrapolation of the channel/congestion metrics to a target
node count.

Usage:
  python tools/net_soak.py --hours 2 --interval 300 --scale-to 60
"""
from __future__ import annotations
import argparse
import json
import pathlib
import time
import urllib.request
import urllib.error

import fleet_common as fc
fc.bootstrap_venv()
import paramiko  # noqa: E402

TB = "http://192.168.8.111:8090"
HERE = pathlib.Path(__file__).resolve().parent


def labels():
    import csv
    m = {}
    fp = HERE / "fleet_map.csv"
    try:
        for r in csv.DictReader(open(fp, encoding="utf-8")):
            if r.get("label", "").isdigit():
                m[int(r["label"])] = r["mac"].replace(":", "")[-4:].lower()
    except Exception:
        pass
    return m


def login():
    r = urllib.request.Request(
        TB + "/api/auth/login",
        data=json.dumps({"username": "tenant@thingsboard.org", "password": "tenant"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(r, timeout=15).read())["token"]


def sample(lab2short):
    """One network snapshot. Returns a metrics dict (or {'err':...})."""
    out = {}
    try:
        tok = login()

        def g(p):
            return json.loads(urllib.request.urlopen(
                urllib.request.Request(TB + p, headers={"X-Authorization": f"Bearer {tok}"}), timeout=20).read())

        devs = {d["name"]: d["id"]["id"] for d in g("/api/tenant/devices?pageSize=400&page=0")["data"]}
        now = int(time.time() * 1000)
        streaming = sresets = swd = srecover = present = 0
        for n, sh in lab2short.items():
            did = devs.get(f"ami-esp32c6-{sh}")
            if not did:
                continue
            present += 1
            ts = g(f"/api/plugins/telemetry/DEVICE/{did}/values/timeseries?keys=uptime_s,total_resets,watchdog_count,recover_count")
            x = ts.get("uptime_s", [{}])[0]
            if x.get("ts") and (now - x["ts"]) // 1000 < 150:
                streaming += 1
            for k, acc in (("total_resets", "sresets"), ("watchdog_count", "swd"), ("recover_count", "srecover")):
                v = ts.get(k, [{}])[0].get("value")
                if str(v).lstrip("-").isdigit():
                    if k == "total_resets": sresets += int(v)
                    elif k == "watchdog_count": swd += int(v)
                    else: srecover += int(v)
        out.update(present=present, streaming=streaming, sum_resets=sresets, sum_wd=swd, sum_recover=srecover)
    except Exception as ex:
        return {"err": f"tb:{ex}"}
    try:
        c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect("192.168.8.111", username="root", password="root", timeout=15)
        def sh(cmd, t=20):
            _i, o, e = c.exec_command(cmd, timeout=t); return (o.read().decode() + e.read().decode()).strip()
        def cnt(t):
            return max(sum(l.strip().startswith("|") for l in sh(f"ot-ctl {t}").splitlines()) - 1, 0)
        out["routers"] = cnt("router table"); out["children"] = cnt("child table")
        ec = sh("ot-ctl eidcache").splitlines()
        out["eid_res"] = sum("cache" in l and "retry" not in l for l in ec)
        out["eid_retry"] = sum("retry" in l for l in ec)
        mac = sh("ot-ctl counters mac")
        for k in ("TxErrCca", "TxDirectMaxRetryExpiry", "RxTotal", "TxTotal"):
            v = next((l.split(":")[1].strip() for l in mac.splitlines() if l.strip().startswith(k + ":")), None)
            out[k] = int(v) if v and v.isdigit() else v
        c.close()
    except Exception as ex:
        out["mesh_err"] = str(ex)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=2.0)
    ap.add_argument("--interval", type=int, default=300, help="seconds between samples")
    ap.add_argument("--scale-to", type=int, default=60, help="target node count for extrapolation")
    args = ap.parse_args()

    lab2short = labels()
    n_nodes = len(lab2short)
    soak_dir = HERE.parent / "soak_logs"; soak_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    csv_path = soak_dir / f"net_soak_{stamp}.csv"
    cols = ["elapsed_min", "present", "streaming", "sum_resets", "sum_wd", "sum_recover",
            "routers", "children", "eid_res", "eid_retry", "TxErrCca", "TxDirectMaxRetryExpiry", "RxTotal", "TxTotal"]
    csv_path.write_text("ts," + ",".join(cols) + "\n", encoding="utf-8")

    print(f"=== AMI net SOAK: {args.hours}h, every {args.interval}s, {n_nodes} labels -> {csv_path.name} ===", flush=True)
    t0 = time.time(); deadline = t0 + args.hours * 3600
    samples = []
    while time.time() < deadline:
        s = sample(lab2short)
        el = round((time.time() - t0) / 60, 1)
        if "err" in s:
            print(f"+{el:6}m  SAMPLE ERROR: {s['err']}", flush=True)
        else:
            samples.append((el, s))
            row = [el] + [s.get(c, "") for c in cols[1:]]
            with csv_path.open("a", encoding="utf-8") as f:
                f.write(time.strftime("%H:%M:%S") + "," + ",".join(str(x) for x in row) + "\n")
            print(f"+{el:6}m  stream={s.get('streaming')}/{s.get('present')} "
                  f"resets={s.get('sum_resets')} wd={s.get('sum_wd')} recover={s.get('sum_recover')} | "
                  f"R/C={s.get('routers')}/{s.get('children')} eid={s.get('eid_res')}/{s.get('eid_retry')}r "
                  f"CCA={s.get('TxErrCca')} retryExp={s.get('TxDirectMaxRetryExpiry')}", flush=True)
        # sleep but wake early if deadline near
        rem = deadline - time.time()
        if rem <= 0: break
        time.sleep(min(args.interval, rem))

    # ---- verdict ----
    print("\n" + "=" * 60)
    if len(samples) < 2:
        print("SOAK: too few samples for a verdict."); return 1
    first = samples[0][1]; last = samples[-1][1]
    strm = [s["streaming"] for _, s in samples if "streaming" in s]
    retry = [s["eid_retry"] for _, s in samples if isinstance(s.get("eid_retry"), int)]
    cca = [s["TxErrCca"] for _, s in samples if isinstance(s.get("TxErrCca"), int)]
    res_delta = (last.get("sum_resets", 0) or 0) - (first.get("sum_resets", 0) or 0)
    wd_delta = (last.get("sum_wd", 0) or 0) - (first.get("sum_wd", 0) or 0)
    print(f"SOAK SUMMARY ({len(samples)} samples over {samples[-1][0]:.0f} min, {n_nodes} nodes):")
    print(f"  streaming    : min={min(strm)} max={max(strm)} final={strm[-1]}  (steadier = better)")
    print(f"  eidcache retry: min={min(retry) if retry else '?'} max={max(retry) if retry else '?'}  (low+flat = resolution scales)")
    print(f"  TxErrCca     : start={cca[0] if cca else '?'} end={cca[-1] if cca else '?'}  (flat = PHY headroom)")
    print(f"  total_resets delta over soak: {res_delta}  (reboots during soak)")
    print(f"  watchdog delta over soak    : {wd_delta}  (HW-watchdog fires; 0 = healthy)")
    cca_end = cca[-1] if cca else None
    if cca_end is not None and n_nodes:
        print(f"  EXTRAPOLATION to {args.scale_to} nodes (linear): "
              f"CCA ~{round(cca_end * args.scale_to / n_nodes, 1)}, "
              f"retryExp ~{round((last.get('TxDirectMaxRetryExpiry') or 0) * args.scale_to / n_nodes)}")
    stable = (max(strm) - min(strm) <= max(3, n_nodes // 6)) and wd_delta == 0 and (not retry or max(retry) < n_nodes)
    print(f"\n  VERDICT: {'STABLE -> scaling further is SUPPORTED' if stable else 'NOT fully steady -> investigate before scaling'}")
    print(f"  (full trend: {csv_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
