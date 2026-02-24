#!/usr/bin/env python3
"""
Generate comparison graphs: V1 (optimized) vs V2 (retry+firmware)
Shows latency distribution, per-round failures, and per-resource analysis.
"""

import csv
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(BASE, "results")

CSV_V1 = os.path.join(BASE, "results", "latency_20260223_154752_delay5000ms_10rounds.csv")
CSV_V2 = os.path.join(BASE, "results", "latency_20260223_204618_delay5000ms_10rounds.csv")

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    import numpy as np
except ImportError:
    print("ERROR: matplotlib/numpy not installed. Install with:")
    print("  pip install matplotlib numpy")
    sys.exit(1)


def load_csv(path):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            r["LatencyMs"] = int(r["LatencyMs"])
            r["Round"] = int(r["Round"])
            r["Failed"] = "FAIL" in r["Status"]
            rows.append(r)
    return rows


def fig1_latency_comparison(v1, v2):
    """Side-by-side latency distribution (successes only)."""
    v1_ok = [r["LatencyMs"] for r in v1 if not r["Failed"]]
    v2_ok = [r["LatencyMs"] for r in v2 if not r["Failed"]]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))

    # V1 histogram
    ax1.hist(v1_ok, bins=50, color="#2196F3", alpha=0.8, edgecolor="white")
    ax1.set_title(f"V1: Optimized (94.5% success)\nAvg={sum(v1_ok)/len(v1_ok):.0f}ms, 33 reads >500ms",
                  fontsize=11)
    ax1.set_xlabel("Latency (ms)")
    ax1.set_ylabel("Count")
    ax1.set_xlim(0, 3500)
    ax1.axvline(x=500, color="orange", linestyle="--", alpha=0.7, label=">500ms threshold")
    ax1.legend()

    # V2 histogram
    ax2.hist(v2_ok, bins=50, color="#4CAF50", alpha=0.8, edgecolor="white")
    ax2.set_title(f"V2: INF+Buf16+Retry (90.9% success)\nAvg={sum(v2_ok)/len(v2_ok):.0f}ms, 0 reads >500ms",
                  fontsize=11)
    ax2.set_xlabel("Latency (ms)")
    ax2.set_ylabel("Count")
    ax2.set_xlim(0, 3500)
    ax2.axvline(x=500, color="orange", linestyle="--", alpha=0.7, label=">500ms threshold")
    ax2.legend()

    fig.suptitle("Latency Distribution: V1 vs V2 (Successes Only)", fontsize=14, fontweight="bold")
    fig.tight_layout()
    path = os.path.join(OUT_DIR, "graph_v1_v2_latency_distribution.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {path}")
    return path


def fig2_round_failures(v1, v2):
    """Per-round failure count comparison."""
    rounds = range(1, 11)
    v1_fails = []
    v2_fails = []
    for rnd in rounds:
        v1_fails.append(len([r for r in v1 if r["Round"] == rnd and r["Failed"]]))
        v2_fails.append(len([r for r in v2 if r["Round"] == rnd and r["Failed"]]))

    fig, ax = plt.subplots(figsize=(10, 5))
    x = np.arange(len(rounds))
    w = 0.35

    bars1 = ax.bar(x - w/2, v1_fails, w, label=f"V1: 94.5% ({sum(v1_fails)} fails)", color="#2196F3", alpha=0.85)
    bars2 = ax.bar(x + w/2, v2_fails, w, label=f"V2: 90.9% ({sum(v2_fails)} fails)", color="#F44336", alpha=0.85)

    ax.set_xlabel("Round", fontsize=12)
    ax.set_ylabel("Failures per Round", fontsize=12)
    ax.set_title("Per-Round Failures: V1 vs V2", fontsize=14, fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels([f"R{r}" for r in rounds])
    ax.set_ylim(0, 8)
    ax.legend(fontsize=11)

    # Add value labels
    for bar in bars1:
        h = bar.get_height()
        if h > 0:
            ax.text(bar.get_x() + bar.get_width()/2, h + 0.1, str(int(h)),
                    ha="center", va="bottom", fontsize=9)
    for bar in bars2:
        h = bar.get_height()
        if h > 0:
            ax.text(bar.get_x() + bar.get_width()/2, h + 0.1, str(int(h)),
                    ha="center", va="bottom", fontsize=9)

    fig.tight_layout()
    path = os.path.join(OUT_DIR, "graph_v1_v2_round_failures.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {path}")
    return path


def fig3_latency_timeline(v1, v2):
    """Timeline of all latencies across both tests."""
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True)

    # V1
    v1_ok_x = [i+1 for i, r in enumerate(v1) if not r["Failed"]]
    v1_ok_y = [r["LatencyMs"] for r in v1 if not r["Failed"]]
    v1_fl_x = [i+1 for i, r in enumerate(v1) if r["Failed"]]
    v1_fl_y = [r["LatencyMs"] for r in v1 if r["Failed"]]

    ax1.scatter(v1_ok_x, v1_ok_y, s=15, c="#2196F3", alpha=0.7, label="Success")
    ax1.scatter(v1_fl_x, v1_fl_y, s=40, c="red", marker="x", label="FAIL (504)")
    ax1.set_ylabel("Latency (ms)")
    ax1.set_title("V1: Optimized (no retry) — 12 failures, 33 slow reads (2-3s)", fontsize=11)
    ax1.set_ylim(0, 5500)
    ax1.axhline(y=5000, color="red", linestyle=":", alpha=0.4)
    ax1.legend(loc="upper right")

    # Round separators
    for rnd in range(1, 11):
        ax1.axvline(x=rnd * 22 + 0.5, color="gray", linestyle=":", alpha=0.3)

    # V2
    v2_ok_x = [i+1 for i, r in enumerate(v2) if not r["Failed"]]
    v2_ok_y = [r["LatencyMs"] for r in v2 if not r["Failed"]]
    v2_fl_x = [i+1 for i, r in enumerate(v2) if r["Failed"]]
    v2_fl_y = [r["LatencyMs"] for r in v2 if r["Failed"]]

    ax2.scatter(v2_ok_x, v2_ok_y, s=15, c="#4CAF50", alpha=0.7, label="Success")
    ax2.scatter(v2_fl_x, v2_fl_y, s=40, c="red", marker="x", label="FAIL (504)")
    ax2.set_ylabel("Latency (ms)")
    ax2.set_xlabel("Request Sequence Number")
    ax2.set_title("V2: INF+Buf16+Retry — 20 failures, ALL successes <60ms", fontsize=11)
    ax2.set_ylim(0, 5500)
    ax2.axhline(y=5000, color="red", linestyle=":", alpha=0.4)
    ax2.legend(loc="upper right")

    for rnd in range(1, 11):
        ax2.axvline(x=rnd * 22 + 0.5, color="gray", linestyle=":", alpha=0.3)

    fig.suptitle("Latency Timeline: V1 vs V2 (All 220 Reads)", fontsize=14, fontweight="bold")
    fig.tight_layout()
    path = os.path.join(OUT_DIR, "graph_v1_v2_timeline.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {path}")
    return path


def fig4_summary_dashboard(v1, v2):
    """Summary dashboard with key metrics."""
    fig, axes = plt.subplots(1, 3, figsize=(15, 5))

    # Panel 1: Success rate comparison
    ax = axes[0]
    labels = ["V1\n(Optimized)", "V2\n(+Retry+INF)"]
    rates = [94.5, 90.9]
    colors = ["#2196F3", "#F44336"]
    bars = ax.bar(labels, rates, color=colors, alpha=0.85, width=0.5)
    ax.set_ylabel("Success Rate (%)")
    ax.set_title("Success Rate", fontsize=12, fontweight="bold")
    ax.set_ylim(80, 100)
    ax.axhline(y=95, color="green", linestyle="--", alpha=0.4, label="95% target")
    for bar, rate in zip(bars, rates):
        ax.text(bar.get_x() + bar.get_width()/2, rate + 0.3, f"{rate}%",
                ha="center", fontsize=12, fontweight="bold")
    ax.legend()

    # Panel 2: Average latency (successes)
    ax = axes[1]
    v1_ok = [r["LatencyMs"] for r in v1 if not r["Failed"]]
    v2_ok = [r["LatencyMs"] for r in v2 if not r["Failed"]]
    avgs = [sum(v1_ok)/len(v1_ok), sum(v2_ok)/len(v2_ok)]
    bars = ax.bar(labels, avgs, color=colors, alpha=0.85, width=0.5)
    ax.set_ylabel("Avg Latency (ms)")
    ax.set_title("Avg Success Latency", fontsize=12, fontweight="bold")
    for bar, avg in zip(bars, avgs):
        ax.text(bar.get_x() + bar.get_width()/2, avg + 10, f"{avg:.0f}ms",
                ha="center", fontsize=12, fontweight="bold")

    # Panel 3: Slow reads (>500ms)
    ax = axes[2]
    v1_slow = len([l for l in v1_ok if l > 500])
    v2_slow = len([l for l in v2_ok if l > 500])
    bars = ax.bar(labels, [v1_slow, v2_slow], color=colors, alpha=0.85, width=0.5)
    ax.set_ylabel("Count")
    ax.set_title("Slow Reads (>500ms)", fontsize=12, fontweight="bold")
    for bar, count in zip(bars, [v1_slow, v2_slow]):
        ax.text(bar.get_x() + bar.get_width()/2, count + 0.5, str(count),
                ha="center", fontsize=12, fontweight="bold")

    fig.suptitle("V1 vs V2 Regression Analysis Dashboard", fontsize=14, fontweight="bold")
    fig.tight_layout()
    path = os.path.join(OUT_DIR, "graph_v1_v2_dashboard.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {path}")
    return path


def main():
    print("Loading CSV files...")
    v1 = load_csv(CSV_V1)
    v2 = load_csv(CSV_V2)
    print(f"  V1: {len(v1)} rows, V2: {len(v2)} rows")

    print("\nGenerating comparison graphs...")
    fig1_latency_comparison(v1, v2)
    fig2_round_failures(v1, v2)
    fig3_latency_timeline(v1, v2)
    fig4_summary_dashboard(v1, v2)

    print("\nAll graphs saved to results/ directory.")


if __name__ == "__main__":
    main()
