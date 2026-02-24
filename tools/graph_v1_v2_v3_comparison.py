#!/usr/bin/env python3
"""
Generate 3-way comparison graphs: V1 vs V2 vs V3
  V1: Californium ACK_TIMEOUT=2500 + Leshan default 5s timeout  (94.5%)
  V2: Californium ACK_TIMEOUT=10s  + Leshan default 5s timeout  (90.9%) [REGRESSION]
  V3: Californium ACK_TIMEOUT=2500 + Leshan ?timeout=10 on API  (97.3%)

Shows how V3 combines fast CoAP retransmits with extended API timeout
to recover reads in the 5-10s band that V1 lost.
"""

import csv
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(BASE, "results")

CSV_V1 = os.path.join(BASE, "results", "latency_20260223_154752_delay5000ms_10rounds.csv")
CSV_V2 = os.path.join(BASE, "results", "latency_20260223_204618_delay5000ms_10rounds.csv")
CSV_V3 = os.path.join(BASE, "results", "latency_20260223_215325_delay5000ms_10rounds.csv")

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

# Color palette
C_V1 = "#2196F3"  # Blue
C_V2 = "#F44336"  # Red
C_V3 = "#4CAF50"  # Green


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


def stats(data):
    """Compute statistics for a dataset."""
    ok = [r["LatencyMs"] for r in data if not r["Failed"]]
    fails = [r for r in data if r["Failed"]]
    total = len(data)
    n_ok = len(ok)
    n_fail = len(fails)
    rate = 100 * n_ok / total if total > 0 else 0
    avg = sum(ok) / len(ok) if ok else 0
    median = sorted(ok)[len(ok) // 2] if ok else 0
    p95 = sorted(ok)[int(len(ok) * 0.95)] if ok else 0
    p99 = sorted(ok)[int(len(ok) * 0.99)] if ok else 0
    max_ok = max(ok) if ok else 0
    slow_500 = len([l for l in ok if l > 500])
    slow_5000 = len([l for l in ok if l > 5000])
    recovered_5_10 = len([l for l in ok if 5000 < l <= 10000])
    return {
        "total": total, "ok": n_ok, "fail": n_fail, "rate": rate,
        "avg": avg, "median": median, "p95": p95, "p99": p99,
        "max": max_ok, "slow_500": slow_500, "slow_5000": slow_5000,
        "recovered_5_10": recovered_5_10,
        "ok_latencies": ok, "fails": fails,
    }


def fig1_success_rate_bar(s1, s2, s3):
    """Success rate comparison bar chart."""
    fig, ax = plt.subplots(figsize=(8, 5))
    labels = [
        "V1\nACK=2.5s\nDefault tout",
        "V2\nACK=10s\nDefault tout",
        "V3\nACK=2.5s\n?timeout=10",
    ]
    rates = [s1["rate"], s2["rate"], s3["rate"]]
    colors = [C_V1, C_V2, C_V3]
    bars = ax.bar(labels, rates, color=colors, alpha=0.85, width=0.55, edgecolor="white")

    ax.set_ylabel("Success Rate (%)", fontsize=12)
    ax.set_title("Success Rate Evolution: V1 → V2 → V3", fontsize=14, fontweight="bold")
    ax.set_ylim(85, 100)
    ax.axhline(y=95, color="gray", linestyle="--", alpha=0.5, label="95% target")

    for bar, rate, fails in zip(bars, rates, [s1["fail"], s2["fail"], s3["fail"]]):
        ax.text(bar.get_x() + bar.get_width()/2, rate + 0.3,
                f"{rate:.1f}%\n({fails} fails)", ha="center", fontsize=11, fontweight="bold")

    ax.legend(fontsize=10)
    fig.tight_layout()
    path = os.path.join(OUT_DIR, "graph_v3_success_rate.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {path}")


def fig2_round_failures(v1, v2, v3):
    """Per-round failure count for all 3 versions."""
    rounds = range(1, 11)
    get_fails = lambda data, rnd: len([r for r in data if r["Round"] == rnd and r["Failed"]])

    v1_f = [get_fails(v1, r) for r in rounds]
    v2_f = [get_fails(v2, r) for r in rounds]
    v3_f = [get_fails(v3, r) for r in rounds]

    fig, ax = plt.subplots(figsize=(12, 5))
    x = np.arange(len(rounds))
    w = 0.25

    ax.bar(x - w, v1_f, w, label=f"V1: {sum(v1_f)} fails (94.5%)", color=C_V1, alpha=0.85)
    ax.bar(x, v2_f, w, label=f"V2: {sum(v2_f)} fails (90.9%)", color=C_V2, alpha=0.85)
    ax.bar(x + w, v3_f, w, label=f"V3: {sum(v3_f)} fails (97.3%)", color=C_V3, alpha=0.85)

    ax.set_xlabel("Round", fontsize=12)
    ax.set_ylabel("Failures", fontsize=12)
    ax.set_title("Failures per Round: V1 vs V2 vs V3", fontsize=14, fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels([f"R{r}" for r in rounds])
    ax.set_ylim(0, 8)
    ax.legend(fontsize=10)
    ax.yaxis.set_major_locator(plt.MaxNLocator(integer=True))

    fig.tight_layout()
    path = os.path.join(OUT_DIR, "graph_v3_round_failures.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {path}")


def fig3_latency_timeline(v1, v2, v3):
    """Timeline scatter plot for all 3 versions."""
    fig, axes = plt.subplots(3, 1, figsize=(16, 12), sharex=True)

    datasets = [
        (v1, "V1: ACK=2.5s, Default timeout — 12 fails, max OK=3741ms", C_V1),
        (v2, "V2: ACK=10s, Default timeout — 20 fails, max OK=48ms", C_V2),
        (v3, "V3: ACK=2.5s, ?timeout=10 — 6 fails, recoveries up to 9916ms", C_V3),
    ]

    for ax, (data, title, color) in zip(axes, datasets):
        ok_x = [i+1 for i, r in enumerate(data) if not r["Failed"]]
        ok_y = [r["LatencyMs"] for r in data if not r["Failed"]]
        fl_x = [i+1 for i, r in enumerate(data) if r["Failed"]]
        fl_y = [r["LatencyMs"] for r in data if r["Failed"]]

        ax.scatter(ok_x, ok_y, s=12, c=color, alpha=0.7, label="Success")
        if fl_x:
            ax.scatter(fl_x, fl_y, s=50, c="red", marker="x", linewidths=2, label="FAIL (504)")

        ax.set_ylabel("Latency (ms)")
        ax.set_title(title, fontsize=11)
        ax.set_ylim(0, 12000)
        ax.axhline(y=5000, color="orange", linestyle=":", alpha=0.5, label="5s default limit")
        ax.axhline(y=10000, color="red", linestyle=":", alpha=0.4, label="10s ?timeout limit")
        ax.legend(loc="upper right", fontsize=8)

        # Round separators
        for rnd in range(1, 11):
            ax.axvline(x=rnd * 22 + 0.5, color="gray", linestyle=":", alpha=0.2)

    axes[-1].set_xlabel("Request Sequence Number", fontsize=12)
    fig.suptitle("Latency Timeline: V1 → V2 → V3 (All 220 Reads per Test)",
                 fontsize=14, fontweight="bold")
    fig.tight_layout()
    path = os.path.join(OUT_DIR, "graph_v3_timeline.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {path}")


def fig4_latency_distribution(s1, s2, s3):
    """Latency histogram for V1 and V3 (V2 omitted since all OK were <60ms)."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))

    # V1
    ax1.hist(s1["ok_latencies"], bins=40, color=C_V1, alpha=0.8, edgecolor="white")
    ax1.set_title(f"V1: {s1['rate']:.1f}% success\nAvg={s1['avg']:.0f}ms, Max={s1['max']}ms",
                  fontsize=11)
    ax1.set_xlabel("Latency (ms)")
    ax1.set_ylabel("Count")
    ax1.set_xlim(0, 10500)
    ax1.axvline(x=5000, color="orange", linestyle="--", alpha=0.7, label="5s limit")
    ax1.legend()

    # V3
    ax2.hist(s3["ok_latencies"], bins=40, color=C_V3, alpha=0.8, edgecolor="white")
    ax2.set_title(f"V3: {s3['rate']:.1f}% success\nAvg={s3['avg']:.0f}ms, Max={s3['max']}ms\n"
                  f"{s3['recovered_5_10']} reads recovered in 5-10s band",
                  fontsize=11)
    ax2.set_xlabel("Latency (ms)")
    ax2.set_ylabel("Count")
    ax2.set_xlim(0, 10500)
    ax2.axvline(x=5000, color="orange", linestyle="--", alpha=0.7, label="5s old limit")
    ax2.axvline(x=10000, color="red", linestyle="--", alpha=0.5, label="10s new limit")

    # Highlight recovery band
    ax2.axvspan(5000, 10000, alpha=0.08, color="green", label="Recovery band (5-10s)")
    ax2.legend(fontsize=9)

    fig.suptitle("Latency Distribution: V1 vs V3 (Successes Only)", fontsize=14, fontweight="bold")
    fig.tight_layout()
    path = os.path.join(OUT_DIR, "graph_v3_latency_distribution.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {path}")


def fig5_summary_dashboard(s1, s2, s3):
    """4-panel summary dashboard."""
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))

    labels = ["V1", "V2", "V3"]
    colors = [C_V1, C_V2, C_V3]

    # Panel 1: Success rate
    ax = axes[0, 0]
    rates = [s1["rate"], s2["rate"], s3["rate"]]
    bars = ax.bar(labels, rates, color=colors, alpha=0.85, width=0.5, edgecolor="white")
    ax.set_ylabel("Success Rate (%)")
    ax.set_title("Success Rate", fontsize=12, fontweight="bold")
    ax.set_ylim(85, 100)
    ax.axhline(y=95, color="gray", linestyle="--", alpha=0.4)
    for bar, rate in zip(bars, rates):
        ax.text(bar.get_x() + bar.get_width()/2, rate + 0.3, f"{rate:.1f}%",
                ha="center", fontsize=11, fontweight="bold")

    # Panel 2: Average latency
    ax = axes[0, 1]
    avgs = [s1["avg"], s2["avg"], s3["avg"]]
    bars = ax.bar(labels, avgs, color=colors, alpha=0.85, width=0.5, edgecolor="white")
    ax.set_ylabel("Avg Latency (ms)")
    ax.set_title("Avg Success Latency", fontsize=12, fontweight="bold")
    for bar, avg in zip(bars, avgs):
        ax.text(bar.get_x() + bar.get_width()/2, avg + 30, f"{avg:.0f}ms",
                ha="center", fontsize=11, fontweight="bold")

    # Panel 3: P95 / P99 latency
    ax = axes[1, 0]
    x = np.arange(3)
    w = 0.3
    p95s = [s1["p95"], s2["p95"], s3["p95"]]
    p99s = [s1["p99"], s2["p99"], s3["p99"]]
    ax.bar(x - w/2, p95s, w, label="P95", color=colors, alpha=0.6, edgecolor="white")
    ax.bar(x + w/2, p99s, w, label="P99", color=colors, alpha=0.9, edgecolor="white")
    ax.set_ylabel("Latency (ms)")
    ax.set_title("P95 / P99 Latency", fontsize=12, fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.legend()
    for i, (p95, p99) in enumerate(zip(p95s, p99s)):
        ax.text(i - w/2, p95 + 50, f"{p95}", ha="center", fontsize=9)
        ax.text(i + w/2, p99 + 50, f"{p99}", ha="center", fontsize=9)

    # Panel 4: Recovery analysis (V3 specific)
    ax = axes[1, 1]
    categories = ["<100ms\n(instant)", "100-2500ms\n(1 retransmit)", "2500-5000ms\n(2 retransmit)",
                   "5000-10000ms\n(recovered)", "FAIL\n(>10s)"]
    v3_counts = [
        len([l for l in s3["ok_latencies"] if l < 100]),
        len([l for l in s3["ok_latencies"] if 100 <= l < 2500]),
        len([l for l in s3["ok_latencies"] if 2500 <= l < 5000]),
        len([l for l in s3["ok_latencies"] if 5000 <= l <= 10000]),
        s3["fail"],
    ]
    bar_colors = ["#4CAF50", "#8BC34A", "#FFC107", "#FF9800", "#F44336"]
    bars = ax.bar(range(len(categories)), v3_counts, color=bar_colors, alpha=0.85, edgecolor="white")
    ax.set_title("V3 Response Breakdown", fontsize=12, fontweight="bold")
    ax.set_xticks(range(len(categories)))
    ax.set_xticklabels(categories, fontsize=9)
    ax.set_ylabel("Count")
    for bar, count in zip(bars, v3_counts):
        ax.text(bar.get_x() + bar.get_width()/2, count + 1, str(count),
                ha="center", fontsize=11, fontweight="bold")

    fig.suptitle("V1 → V2 → V3 Optimization Dashboard\n"
                 "ESP32-C6 Thread + LwM2M over CoAP (22 resources × 10 rounds = 220 reads)",
                 fontsize=14, fontweight="bold")
    fig.tight_layout()
    path = os.path.join(OUT_DIR, "graph_v3_dashboard.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {path}")


def print_text_report(s1, s2, s3):
    """Print a text-based comparison report."""
    print("\n" + "=" * 70)
    print("  3-WAY COMPARISON REPORT: V1 → V2 → V3")
    print("=" * 70)

    header = f"{'Metric':<30} {'V1':>10} {'V2':>10} {'V3':>10}"
    print(header)
    print("-" * 70)

    def row(label, v1, v2, v3, fmt=""):
        if fmt:
            print(f"{label:<30} {v1:>10{fmt}} {v2:>10{fmt}} {v3:>10{fmt}}")
        else:
            print(f"{label:<30} {str(v1):>10} {str(v2):>10} {str(v3):>10}")

    row("Total Reads", s1["total"], s2["total"], s3["total"])
    row("Successful", s1["ok"], s2["ok"], s3["ok"])
    row("Failed", s1["fail"], s2["fail"], s3["fail"])
    row("Success Rate", f"{s1['rate']:.1f}%", f"{s2['rate']:.1f}%", f"{s3['rate']:.1f}%")
    print("-" * 70)
    row("Avg Latency (OK)", f"{s1['avg']:.0f}ms", f"{s2['avg']:.0f}ms", f"{s3['avg']:.0f}ms")
    row("Median Latency", f"{s1['median']}ms", f"{s2['median']}ms", f"{s3['median']}ms")
    row("P95 Latency", f"{s1['p95']}ms", f"{s2['p95']}ms", f"{s3['p95']}ms")
    row("P99 Latency", f"{s1['p99']}ms", f"{s2['p99']}ms", f"{s3['p99']}ms")
    row("Max OK Latency", f"{s1['max']}ms", f"{s2['max']}ms", f"{s3['max']}ms")
    print("-" * 70)
    row("Reads >500ms", s1["slow_500"], s2["slow_500"], s3["slow_500"])
    row("Reads >5000ms (OK)", s1["slow_5000"], s2["slow_5000"], s3["slow_5000"])
    row("Recovered 5-10s band", s1["recovered_5_10"], s2["recovered_5_10"], s3["recovered_5_10"])

    print("\n" + "=" * 70)
    print("  CONFIGURATION COMPARISON")
    print("=" * 70)
    configs = [
        ("Californium ACK_TIMEOUT", "2500ms", "10000ms", "2500ms"),
        ("Californium MAX_RETRANSMIT", "4", "4", "4"),
        ("Leshan API ?timeout=", "default(5s)", "default(5s)", "10s"),
        ("LwM2M Log Level", "DBG", "INF", "INF"),
        ("Engine Buffers", "12", "16", "16"),
        ("PS Script $rid fix", "N/A", "N/A", "Yes (${rid})"),
    ]
    print(f"{'Parameter':<30} {'V1':>14} {'V2':>14} {'V3':>14}")
    print("-" * 72)
    for name, c1, c2, c3 in configs:
        print(f"{name:<30} {c1:>14} {c2:>14} {c3:>14}")

    print("\n" + "=" * 70)
    print("  KEY INSIGHT")
    print("=" * 70)
    print(f"  V3 recovered {s3['recovered_5_10']} reads in the 5-10s band that")
    print(f"  would have been FAILURES with V1's default 5s API timeout.")
    improvement = s3["rate"] - s1["rate"]
    print(f"  Net improvement: V1({s1['rate']:.1f}%) → V3({s3['rate']:.1f}%) = +{improvement:.1f}pp")
    print(f"  Failures reduced: {s1['fail']} → {s3['fail']} ({s1['fail'] - s3['fail']} fewer)")
    print("=" * 70)


def main():
    print("Loading CSV files...")
    v1 = load_csv(CSV_V1)
    v2 = load_csv(CSV_V2)
    v3 = load_csv(CSV_V3)
    print(f"  V1: {len(v1)} rows, V2: {len(v2)} rows, V3: {len(v3)} rows")

    s1 = stats(v1)
    s2 = stats(v2)
    s3 = stats(v3)

    print_text_report(s1, s2, s3)

    print("\nGenerating comparison graphs...")
    fig1_success_rate_bar(s1, s2, s3)
    fig2_round_failures(v1, v2, v3)
    fig3_latency_timeline(v1, v2, v3)
    fig4_latency_distribution(s1, s2, s3)
    fig5_summary_dashboard(s1, s2, s3)

    print("\nAll graphs saved to results/ directory.")


if __name__ == "__main__":
    main()
