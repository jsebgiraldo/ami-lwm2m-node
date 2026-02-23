#!/usr/bin/env python3
"""Analyze 504 failure patterns from latency test CSV."""
import csv, sys
from collections import defaultdict

csv_path = sys.argv[1] if len(sys.argv) > 1 else "results/latency_20260223_131058_delay3000ms_10rounds.csv"
rows = list(csv.DictReader(open(csv_path)))
total = len(rows)
fails = [r for r in rows if "FAIL" in r["Status"]]
print(f"Total requests: {total}, Failed: {len(fails)} ({100*len(fails)/total:.1f}%)\n")

# --- Failures per round ---
print("=== FAILURES PER ROUND ===")
for r in range(1, 11):
    rr = [x for x in rows if int(x["Round"]) == r]
    rf = [x for x in rr if "FAIL" in x["Status"]]
    bar = "#" * len(rf) + "." * (22 - len(rf))
    print(f"  Round {r:2d}: {len(rf):2d}/22 fail ({100*len(rf)/22:5.1f}%)  [{bar}]")

# --- Failures per resource ---
print("\n=== FAILURES PER RESOURCE (sorted by fail rate) ===")
res_fails = defaultdict(int)
res_total = defaultdict(int)
for x in rows:
    key = x["ObjectName"] + "/" + x["ResourceLabel"]
    res_total[key] += 1
    if "FAIL" in x["Status"]:
        res_fails[key] += 1

for k in sorted(res_total.keys(), key=lambda k: res_fails.get(k, 0), reverse=True):
    f = res_fails.get(k, 0)
    pct = 100 * f / res_total[k]
    bar = "#" * f + "." * (res_total[k] - f)
    print(f"  {k:40s}  {f:2d}/{res_total[k]:2d} ({pct:5.1f}%)  [{bar}]")

# --- Failure rate by sequence position ---
print("\n=== FAILURE RATE BY SEQUENCE POSITION ===")
print("  (Shows whether later requests in a round fail more often)")
for seq in range(1, 23):
    sr = [x for x in rows if int(x["SeqNum"]) == seq]
    sf = [x for x in sr if "FAIL" in x["Status"]]
    name = sr[0]["ObjectName"] + "/" + sr[0]["ResourceLabel"] if sr else "?"
    pct = 100 * len(sf) / len(sr) if sr else 0
    bar = "#" * len(sf) + "." * (len(sr) - len(sf))
    print(f"  Seq {seq:2d} {name:35s}: {len(sf):2d}/10 ({pct:5.1f}%)  [{bar}]")

# --- Consecutive failure streaks ---
print("\n=== LONGEST CONSECUTIVE FAILURE STREAKS PER ROUND ===")
for r in range(1, 11):
    rr = sorted([x for x in rows if int(x["Round"]) == r], key=lambda x: int(x["SeqNum"]))
    max_streak = 0
    streak = 0
    start_seq = 0
    best_start = 0
    for x in rr:
        if "FAIL" in x["Status"]:
            if streak == 0:
                start_seq = int(x["SeqNum"])
            streak += 1
            if streak > max_streak:
                max_streak = streak
                best_start = start_seq
        else:
            streak = 0
    print(f"  Round {r:2d}: max streak = {max_streak} consecutive 504s (starting at seq {best_start})")

# --- Latency distribution for CONTENT responses ---
print("\n=== LATENCY BUCKETS (CONTENT only) ===")
ok = [int(r["LatencyMs"]) for r in rows if "FAIL" not in r["Status"]]
buckets = [(0, 50, "Cache hit (<50ms)"), (50, 500, "Fast (50-500ms)"),
           (500, 2000, "Medium (0.5-2s)"), (2000, 3500, "Real CoAP (2-3.5s)"),
           (3500, 5100, "Near-timeout (3.5-5.1s)")]
for lo, hi, label in buckets:
    n = len([x for x in ok if lo <= x < hi])
    pct = 100 * n / len(ok) if ok else 0
    print(f"  {label:30s}: {n:3d} ({pct:5.1f}%)")

# --- Time between consecutive fails pattern ---
print("\n=== CONGESTION BUILD-UP ANALYSIS ===")
print("  Average latency by request position within each round:")
for seq in range(1, 23):
    sr = [x for x in rows if int(x["SeqNum"]) == seq]
    avg_lat = sum(int(x["LatencyMs"]) for x in sr) / len(sr)
    fail_count = sum(1 for x in sr if "FAIL" in x["Status"])
    print(f"  Seq {seq:2d}: avg={avg_lat:7.0f}ms  fails={fail_count}/10")

print("\n=== DIAGNOSIS ===")
# Determine if it's position-dependent (congestion) or resource-dependent
early_fails = sum(1 for x in fails if int(x["SeqNum"]) <= 7)
late_fails = sum(1 for x in fails if int(x["SeqNum"]) > 7)
print(f"  Failures in seq 1-7  (early):  {early_fails}")
print(f"  Failures in seq 8-22 (late):   {late_fails}")
if late_fails > early_fails * 1.5:
    print("  → CONGESTION PATTERN: Later requests fail more → inter-request delay too short")
elif early_fails > late_fails * 1.5:
    print("  → EARLY FAILURE PATTERN: First requests fail more → device not ready")
else:
    print("  → MIXED PATTERN: Failures distributed across positions")

round_fails = [sum(1 for x in rows if int(x["Round"]) == r and "FAIL" in x["Status"]) for r in range(1, 11)]
early_round_fails = sum(round_fails[:5])
late_round_fails = sum(round_fails[5:])
print(f"\n  Failures in rounds 1-5:  {early_round_fails}")
print(f"  Failures in rounds 6-10: {late_round_fails}")
if late_round_fails > early_round_fails * 1.3:
    print("  → DEGRADATION: Device performance degrades over time (resource exhaustion?)")
else:
    print("  → STABLE: Failure rate is consistent across rounds")
