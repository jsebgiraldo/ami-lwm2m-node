#!/usr/bin/env python3
"""Characterise the node's current bursts across every FNB capture on disk.

Answers the question the fleet has been arguing about for months: how big and how
frequent is a single node's current excursion, and how many such nodes can share
one USB host before the 500 mA budget is gone?

Pools tools/fnb_power_*.csv and tools/lab_burst_fnb_*.csv, rejects the meter's
decode glitches, then reports the idle floor, the burst-event distribution
(peak, duration, rate) and the resulting per-host node budget.

  python tools/lab_burst_analyze.py [--glob-extra PATTERN] [--usb-budget 500]
"""
from __future__ import annotations

import argparse
import csv
import glob
import pathlib
import statistics as st
from lab_paths import captures_dir

HERE = pathlib.Path(__file__).resolve().parent


def load(paths: list[str]) -> list[float]:
    out: list[float] = []
    for p in paths:
        try:
            with open(p, encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    v = r.get("i_ma")
                    if v in (None, "", "None"):
                        continue
                    try:
                        out.append(float(v))
                    except ValueError:
                        pass
        except OSError:
            continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--usb-budget", type=float, default=500.0,
                    help="per-host current budget in mA (USB 2.0 = 500)")
    ap.add_argument("--rate-hz", type=float, default=100.0,
                    help="FNB sample rate, for turning sample counts into seconds")
    args = ap.parse_args()

    files = sorted(glob.glob(str(captures_dir() / "fnb_power_*.csv"))
                   + glob.glob(str(captures_dir() / "lab_burst_fnb_*.csv")))
    if not files:
        print("no FNB captures found in tools/")
        return 1
    cur = load(files)
    if not cur:
        print("captures contained no usable i_ma samples")
        return 1

    # Reject decode glitches: the FNB-C2 emits rare CRC-valid garbage samples
    # (1290 / 2097 / 8388 mA against a ~60 mA floor). A real ESP32-C6 cannot
    # draw ~1 A, so anything past 8x the median is instrument noise, not current.
    med_all = st.median(cur)
    cut = max(med_all * 8.0, 400.0)
    clean = [x for x in cur if x <= cut]
    glitches = len(cur) - len(clean)

    idle = st.median(clean)
    s = sorted(clean)

    def pct(p: float) -> float:
        return s[min(int(len(s) * p), len(s) - 1)]

    # A "burst" = a run of consecutive samples above idle + 15 mA. 15 mA is well
    # clear of the meter's own noise (p50..p90 spread is a couple of mA) but low
    # enough to catch a radio TX excursion.
    thresh = idle + 15.0
    bursts: list[tuple[int, float]] = []   # (n_samples, peak)
    run, peak = 0, 0.0
    for x in clean:
        if x >= thresh:
            run += 1
            peak = max(peak, x)
        elif run:
            bursts.append((run, peak))
            run, peak = 0, 0.0
    if run:
        bursts.append((run, peak))

    total_s = len(clean) / args.rate_hz
    print("=" * 66)
    print(f"  SINGLE-NODE CURRENT PROFILE   ({len(files)} captures, "
          f"{len(clean):,} samples ~ {total_s/60:.1f} min)")
    print("=" * 66)
    print(f"  idle floor (median) : {idle:7.1f} mA")
    print(f"  p90 / p99 / p99.9   : {pct(0.90):7.1f} / {pct(0.99):7.1f} / {pct(0.999):7.1f} mA")
    print(f"  max (clean)         : {s[-1]:7.1f} mA")
    print(f"  decode glitches     : {glitches}/{len(cur)} rejected (> {cut:.0f} mA)")

    print(f"\n  BURST EVENTS (>= idle+15 = {thresh:.0f} mA)")
    if bursts:
        peaks = sorted(b[1] for b in bursts)
        durs = [b[0] / args.rate_hz * 1000 for b in bursts]
        print(f"    count            : {len(bursts)}  ({len(bursts)/(total_s/60):.1f} per minute)")
        print(f"    peak  med / max  : {peaks[len(peaks)//2]:.1f} / {peaks[-1]:.1f} mA")
        print(f"    duration med/max : {st.median(durs):.0f} / {max(durs):.0f} ms")
        worst = peaks[-1]
    else:
        print("    none above threshold")
        worst = s[-1]

    print(f"\n  USB HOST BUDGET ({args.usb_budget:.0f} mA)")
    print(f"    nodes at idle             : {int(args.usb_budget // idle)}")
    print(f"    nodes if ALL burst at once: {int(args.usb_budget // worst)}"
          f"   <-- the synchronised-burst case")
    print(f"    worst-case node peak      : {worst:.1f} mA "
          f"({worst / args.usb_budget * 100:.0f}% of one host)")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
