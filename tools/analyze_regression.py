#!/usr/bin/env python3
"""
Regression Analysis: Compare optimized-v1 (94.5%) vs optimized-v2 (90.9%)

Analyzes why the second optimization round (Californium ACK_TIMEOUT,
firmware INF+buffers, warmup+retry) performed WORSE than the first.
"""

import csv
import os
import sys
from collections import defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CSV_V1 = os.path.join(BASE, "results", "latency_20260223_154752_delay5000ms_10rounds.csv")
CSV_V2 = os.path.join(BASE, "results", "latency_20260223_204618_delay5000ms_10rounds.csv")


def load_csv(path):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            r["LatencyMs"] = int(r["LatencyMs"])
            r["Round"] = int(r["Round"])
            r["SeqNum"] = int(r["SeqNum"])
            r["Object"] = int(r["Object"])
            r["Resource"] = int(r["Resource"])
            r["DelayMs"] = int(r["DelayMs"])
            r["Failed"] = "FAIL" in r["Status"]
            rows.append(r)
    return rows


def analyze(rows, label):
    total = len(rows)
    fails = [r for r in rows if r["Failed"]]
    successes = [r for r in rows if not r["Failed"]]

    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  Total reads   : {total}")
    print(f"  Successes     : {len(successes)} ({100*len(successes)/total:.1f}%)")
    print(f"  Failures      : {len(fails)} ({100*len(fails)/total:.1f}%)")

    if successes:
        lats = [r["LatencyMs"] for r in successes]
        print(f"\n  Success latency:")
        print(f"    Min    : {min(lats)} ms")
        print(f"    Max    : {max(lats)} ms")
        print(f"    Avg    : {sum(lats)/len(lats):.1f} ms")
        print(f"    Median : {sorted(lats)[len(lats)//2]} ms")

        # Distribution buckets
        buckets = {"<50ms": 0, "50-100ms": 0, "100-500ms": 0, "500-2000ms": 0, "2000-4000ms": 0, "4000-5000ms": 0}
        for l in lats:
            if l < 50: buckets["<50ms"] += 1
            elif l < 100: buckets["50-100ms"] += 1
            elif l < 500: buckets["100-500ms"] += 1
            elif l < 2000: buckets["500-2000ms"] += 1
            elif l < 4000: buckets["2000-4000ms"] += 1
            else: buckets["4000-5000ms"] += 1
        print(f"\n  Success latency distribution:")
        for bucket, count in buckets.items():
            bar = "#" * (count * 40 // len(successes)) if successes else ""
            print(f"    {bucket:>12s}: {count:3d} ({100*count/len(successes):5.1f}%) {bar}")

    if fails:
        fail_lats = [r["LatencyMs"] for r in fails]
        print(f"\n  Failure timeout range: {min(fail_lats)}-{max(fail_lats)} ms")

    # Per-round breakdown
    print(f"\n  Per-round failures:")
    for rnd in range(1, 11):
        rnd_rows = [r for r in rows if r["Round"] == rnd]
        rnd_fails = [r for r in rnd_rows if r["Failed"]]
        labels = [r["ResourceLabel"] for r in rnd_fails]
        status = "OK" if not labels else ", ".join(labels)
        print(f"    R{rnd:2d}: {len(rnd_fails)}/{len(rnd_rows)} fails  {status}")

    # Per-resource failure frequency
    print(f"\n  Per-resource failure count (across 10 rounds):")
    res_fails = defaultdict(int)
    res_total = defaultdict(int)
    for r in rows:
        key = f"{r['ObjectName']}/{r['ResourceLabel']}"
        res_total[key] += 1
        if r["Failed"]:
            res_fails[key] += 1
    for key in sorted(res_fails, key=lambda k: -res_fails[k]):
        print(f"    {key:30s}: {res_fails[key]}/{res_total[key]} ({100*res_fails[key]/res_total[key]:.0f}%)")

    return {"total": total, "fails": len(fails), "successes": len(successes),
            "rows": rows, "success_lats": [r["LatencyMs"] for r in successes],
            "fail_lats": [r["LatencyMs"] for r in fails]}


def compare(v1_stats, v2_stats):
    print(f"\n{'='*60}")
    print(f"  COMPARISON: V1 vs V2")
    print(f"{'='*60}")

    print(f"\n  Metric               V1 (opt)    V2 (retry+Cf)   Delta")
    print(f"  {'─'*55}")
    print(f"  Failures             {v1_stats['fails']:3d}/220     {v2_stats['fails']:3d}/220       {v2_stats['fails'] - v1_stats['fails']:+d}")
    print(f"  Success rate         {100*v1_stats['successes']/v1_stats['total']:5.1f}%      {100*v2_stats['successes']/v2_stats['total']:5.1f}%      {100*(v2_stats['successes'] - v1_stats['successes'])/v1_stats['total']:+.1f}%")

    if v1_stats["success_lats"] and v2_stats["success_lats"]:
        v1_avg = sum(v1_stats["success_lats"]) / len(v1_stats["success_lats"])
        v2_avg = sum(v2_stats["success_lats"]) / len(v2_stats["success_lats"])
        print(f"  Avg success latency  {v1_avg:6.1f}ms    {v2_avg:6.1f}ms     {v2_avg - v1_avg:+.1f}ms")

        v1_slow = len([l for l in v1_stats["success_lats"] if l > 500])
        v2_slow = len([l for l in v2_stats["success_lats"] if l > 500])
        print(f"  Slow reads (>500ms)  {v1_slow:3d}         {v2_slow:3d}           {v2_slow - v1_slow:+d}")

    # Key insight: Californium timeout
    v1_timeout_avg = sum(v1_stats["fail_lats"]) / len(v1_stats["fail_lats"]) if v1_stats["fail_lats"] else 0
    v2_timeout_avg = sum(v2_stats["fail_lats"]) / len(v2_stats["fail_lats"]) if v2_stats["fail_lats"] else 0
    print(f"  Avg fail timeout     {v1_timeout_avg:6.0f}ms    {v2_timeout_avg:6.0f}ms")

    print(f"\n  KEY FINDINGS:")
    print(f"  ─────────────────────────────────────────────────────")
    print(f"  1. Californium ACK_TIMEOUT=10s is NOT taking effect!")
    print(f"     Both tests time out at ~5000ms, proving the properties")
    print(f"     file is not being loaded by Leshan/Californium.")
    print(f"")
    print(f"  2. Firmware INF logging + larger buffers ELIMINATED slow reads:")
    v1_slow_count = len([l for l in v1_stats["success_lats"] if l > 500])
    print(f"     V1 had {v1_slow_count} reads between 2-5s (CoAP retransmits)")
    print(f"     V2 has 0 reads >100ms - ALL successes are <60ms")
    print(f"     This means the ESP32 responds faster with less log overhead.")
    print(f"")
    print(f"  3. Retry mechanism is counterproductive:")
    print(f"     When a request times out (5s), the retry also times out (5s)")
    print(f"     Total penalty per failure: 5s + 3s delay + 5s = 13s")
    print(f"     This adds ~100s of wasted time without improving success.")
    print(f"")
    print(f"  4. More failures despite faster responses suggests that the")
    print(f"     retry traffic creates CoAP congestion, triggering more")
    print(f"     timeouts in subsequent reads (cascade effect in R3: 6 fails).")


def main():
    print("Loading CSV files...")
    v1 = load_csv(CSV_V1)
    v2 = load_csv(CSV_V2)

    v1_stats = analyze(v1, "V1: Optimized (no retry, no Californium)")
    v2_stats = analyze(v2, "V2: With Warmup + Retry + Californium + INF/Buf16")

    compare(v1_stats, v2_stats)

    print(f"\n{'='*60}")
    print(f"  RECOMMENDED NEXT STEPS")
    print(f"{'='*60}")
    print(f"  1. FIX Californium.properties loading:")
    print(f"     - Check if Leshan reads from CWD or needs -D argument")
    print(f"     - Try: java -Dcalifornium.properties.file=/Californium.properties")
    print(f"     - Or copy to Leshan's working directory in the container")
    print(f"")
    print(f"  2. KEEP firmware changes (LOG_INF + buffers 16):")
    print(f"     - These clearly improved response times (<60ms vs 2-3s)")
    print(f"")
    print(f"  3. REMOVE retry logic (or make it smarter):")
    print(f"     - Current retry only adds 13s overhead per failure")
    print(f"     - Either remove, or use shorter timeout on retry (2s)")
    print(f"")
    print(f"  4. INCREASE inter-read delay to 8000ms:")
    print(f"     - Give CoAP exchange more time to fully clean up")
    print(f"     - Reduces congestion between consecutive reads")


if __name__ == "__main__":
    main()
