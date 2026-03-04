#!/usr/bin/env python3
"""
graph_v013_v014_comparison.py — Before/After comparison: v0.13 vs v0.14
=========================================================================
Thesis: Tesis_jsgiraldod_2026_rev_final

Compares benchmark results from firmware v0.13.0 (baseline) and v0.14.0
(optimized) side by side. Generates publication-quality comparison graphs.

Changes in v0.14.0:
  - DLMS poll: 30s → 15s
  - Force-notify for unchanged values (nextafter trick)
  - RSSI/LQI nudge for Object 4
  - Single-phase pre-skip (Kconfig)
  - Dedicated DLMS poll thread
  - Notify all 27 resources

Usage:
  python tools/graph_v013_v014_comparison.py \\
      results/benchmark/20260303_184204 \\
      results/benchmark_v014 \\
      --output results/comparison_v013_v014
"""

import argparse
import json
import os
import sys
from datetime import datetime

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker
    import numpy as np
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

# ─── Constants ───
SCENARIO_ORDER = ["baseline", "1s", "5s", "10s"]
SCENARIO_LABELS = {
    "baseline": "Baseline\n(prod)",
    "1s": "Agresivo\n(1s)",
    "5s": "Medio\n(5s)",
    "10s": "Relajado\n(10s)",
}

TELEMETRY_KEYS_DISPLAY = [
    "voltage", "current", "activePower", "activeEnergy",
    "reactivePower", "apparentPower", "powerFactor",
    "totalActivePower", "totalReactivePower", "totalApparentPower",
    "totalPowerFactor", "reactiveEnergy", "apparentEnergy",
    "frequency", "radioSignalStrength", "linkQuality",
]

KEY_SHORT = {
    "voltage": "V", "current": "I", "activePower": "P",
    "activeEnergy": "Ea", "reactivePower": "Q",
    "apparentPower": "S", "powerFactor": "PF",
    "totalActivePower": "Pt", "totalReactivePower": "Qt",
    "totalApparentPower": "St", "totalPowerFactor": "PFt",
    "reactiveEnergy": "Eq", "apparentEnergy": "Es",
    "frequency": "f", "radioSignalStrength": "RSSI",
    "linkQuality": "LQI",
}

V13_COLOR = "#78909C"   # blue-grey
V14_COLOR = "#26A69A"   # teal
IMPROVE_COLOR = "#66BB6A"
REGRESS_COLOR = "#EF5350"

STYLE = {
    "figure.figsize": (14, 7),
    "font.size": 11,
    "font.family": "serif",
    "axes.titlesize": 14,
    "axes.labelsize": 12,
    "xtick.labelsize": 10,
    "ytick.labelsize": 10,
    "legend.fontsize": 10,
    "figure.dpi": 150,
    "savefig.dpi": 300,
    "savefig.bbox": "tight",
    "axes.grid": True,
    "grid.alpha": 0.3,
    "axes.spines.top": False,
    "axes.spines.right": False,
}


def load_summary(path):
    """Load benchmark_summary.json from a results directory."""
    fpath = os.path.join(path, "benchmark_summary.json")
    if not os.path.exists(fpath):
        print(f"ERROR: {fpath} not found")
        sys.exit(1)
    with open(fpath, "r", encoding="utf-8") as f:
        return json.load(f)


def _safe(val, default=0):
    return val if val is not None else default


def plot_throughput_comparison(v13, v14, output_dir, fmt):
    """Bar chart: throughput (msgs/s) side by side per scenario."""
    plt.rcParams.update(STYLE)
    fig, ax = plt.subplots(figsize=(12, 6))

    scenarios = [s for s in SCENARIO_ORDER
                 if s in v13.get("scenarios", {}) or s in v14.get("scenarios", {})]
    x = np.arange(len(scenarios))
    w = 0.35

    vals_13 = []
    vals_14 = []
    for s in scenarios:
        s13 = v13.get("scenarios", {}).get(s, {}).get("aggregate", {})
        s14 = v14.get("scenarios", {}).get(s, {}).get("aggregate", {})
        vals_13.append(_safe(s13.get("overall_throughput_msgs_per_sec")))
        vals_14.append(_safe(s14.get("overall_throughput_msgs_per_sec")))

    bars1 = ax.bar(x - w/2, vals_13, w, label="v0.13.0", color=V13_COLOR, edgecolor="white")
    bars2 = ax.bar(x + w/2, vals_14, w, label="v0.14.0", color=V14_COLOR, edgecolor="white")

    # Value labels
    for bar in bars1:
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2, h + 0.002, f"{h:.3f}",
                ha="center", va="bottom", fontsize=9)
    for bar in bars2:
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2, h + 0.002, f"{h:.3f}",
                ha="center", va="bottom", fontsize=9)

    # Improvement arrows
    for i, (v1, v2) in enumerate(zip(vals_13, vals_14)):
        if v1 > 0:
            pct = (v2 - v1) / v1 * 100
            color = IMPROVE_COLOR if pct >= 0 else REGRESS_COLOR
            sign = "+" if pct >= 0 else ""
            ax.annotate(f"{sign}{pct:.1f}%", xy=(x[i], max(v1, v2) + 0.015),
                        ha="center", fontsize=9, fontweight="bold", color=color)

    ax.set_xticks(x)
    ax.set_xticklabels([SCENARIO_LABELS.get(s, s) for s in scenarios])
    ax.set_ylabel("Throughput (msgs/s)")
    ax.set_title("Throughput: v0.13.0 vs v0.14.0")
    ax.legend()
    ax.set_ylim(0, max(max(vals_13), max(vals_14)) * 1.3)

    path = os.path.join(output_dir, f"comparison_throughput.{fmt}")
    fig.savefig(path)
    plt.close(fig)
    print(f"  [1/6] {path}")
    return path


def plot_samples_comparison(v13, v14, output_dir, fmt):
    """Bar chart: total messages per scenario."""
    plt.rcParams.update(STYLE)
    fig, ax = plt.subplots(figsize=(12, 6))

    scenarios = [s for s in SCENARIO_ORDER
                 if s in v13.get("scenarios", {}) or s in v14.get("scenarios", {})]
    x = np.arange(len(scenarios))
    w = 0.35

    vals_13 = []
    vals_14 = []
    for s in scenarios:
        s13 = v13.get("scenarios", {}).get(s, {}).get("aggregate", {})
        s14 = v14.get("scenarios", {}).get(s, {}).get("aggregate", {})
        vals_13.append(_safe(s13.get("total_messages")))
        vals_14.append(_safe(s14.get("total_messages")))

    bars1 = ax.bar(x - w/2, vals_13, w, label="v0.13.0", color=V13_COLOR, edgecolor="white")
    bars2 = ax.bar(x + w/2, vals_14, w, label="v0.14.0", color=V14_COLOR, edgecolor="white")

    for bar in bars1:
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2, h + 0.5, f"{int(h)}",
                ha="center", va="bottom", fontsize=9)
    for bar in bars2:
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2, h + 0.5, f"{int(h)}",
                ha="center", va="bottom", fontsize=9)

    for i, (v1, v2) in enumerate(zip(vals_13, vals_14)):
        if v1 > 0:
            pct = (v2 - v1) / v1 * 100
            color = IMPROVE_COLOR if pct >= 0 else REGRESS_COLOR
            sign = "+" if pct >= 0 else ""
            ax.annotate(f"{sign}{pct:.1f}%", xy=(x[i], max(v1, v2) + 3),
                        ha="center", fontsize=9, fontweight="bold", color=color)

    ax.set_xticks(x)
    ax.set_xticklabels([SCENARIO_LABELS.get(s, s) for s in scenarios])
    ax.set_ylabel("Total Mensajes (300s)")
    ax.set_title("Muestras Totales: v0.13.0 vs v0.14.0")
    ax.legend()

    path = os.path.join(output_dir, f"comparison_samples.{fmt}")
    fig.savefig(path)
    plt.close(fig)
    print(f"  [2/6] {path}")
    return path


def plot_iat_comparison(v13, v14, output_dir, fmt):
    """Bar chart: average IAT per scenario."""
    plt.rcParams.update(STYLE)
    fig, ax = plt.subplots(figsize=(12, 6))

    scenarios = [s for s in SCENARIO_ORDER
                 if s in v13.get("scenarios", {}) or s in v14.get("scenarios", {})]
    x = np.arange(len(scenarios))
    w = 0.35

    vals_13 = []
    vals_14 = []
    for s in scenarios:
        s13 = v13.get("scenarios", {}).get(s, {}).get("aggregate", {})
        s14 = v14.get("scenarios", {}).get(s, {}).get("aggregate", {})
        vals_13.append(_safe(s13.get("iat_global_avg_s")))
        vals_14.append(_safe(s14.get("iat_global_avg_s")))

    bars1 = ax.bar(x - w/2, vals_13, w, label="v0.13.0", color=V13_COLOR, edgecolor="white")
    bars2 = ax.bar(x + w/2, vals_14, w, label="v0.14.0", color=V14_COLOR, edgecolor="white")

    for bar in bars1:
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2, h + 0.3, f"{h:.1f}s",
                ha="center", va="bottom", fontsize=9)
    for bar in bars2:
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2, h + 0.3, f"{h:.1f}s",
                ha="center", va="bottom", fontsize=9)

    for i, (v1, v2) in enumerate(zip(vals_13, vals_14)):
        if v1 > 0:
            pct = (v2 - v1) / v1 * 100
            color = IMPROVE_COLOR if pct <= 0 else REGRESS_COLOR  # lower IAT = better
            sign = "+" if pct >= 0 else ""
            ax.annotate(f"{sign}{pct:.1f}%", xy=(x[i], max(v1, v2) + 2),
                        ha="center", fontsize=9, fontweight="bold", color=color)

    ax.set_xticks(x)
    ax.set_xticklabels([SCENARIO_LABELS.get(s, s) for s in scenarios])
    ax.set_ylabel("IAT Promedio (s)")
    ax.set_title("Inter-Arrival Time Promedio: v0.13.0 vs v0.14.0")
    ax.legend()

    path = os.path.join(output_dir, f"comparison_iat.{fmt}")
    fig.savefig(path)
    plt.close(fig)
    print(f"  [3/6] {path}")
    return path


def plot_keys_reporting(v13, v14, output_dir, fmt):
    """Bar chart: number of keys reporting per scenario."""
    plt.rcParams.update(STYLE)
    fig, ax = plt.subplots(figsize=(12, 6))

    scenarios = [s for s in SCENARIO_ORDER
                 if s in v13.get("scenarios", {}) or s in v14.get("scenarios", {})]
    x = np.arange(len(scenarios))
    w = 0.35

    vals_13 = []
    vals_14 = []
    for s in scenarios:
        s13 = v13.get("scenarios", {}).get(s, {}).get("aggregate", {})
        s14 = v14.get("scenarios", {}).get(s, {}).get("aggregate", {})
        vals_13.append(_safe(s13.get("total_keys_reporting")))
        vals_14.append(_safe(s14.get("total_keys_reporting")))

    bars1 = ax.bar(x - w/2, vals_13, w, label="v0.13.0", color=V13_COLOR, edgecolor="white")
    bars2 = ax.bar(x + w/2, vals_14, w, label="v0.14.0", color=V14_COLOR, edgecolor="white")

    for bar in bars1:
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2, h + 0.1, f"{int(h)}/16",
                ha="center", va="bottom", fontsize=9)
    for bar in bars2:
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2, h + 0.1, f"{int(h)}/16",
                ha="center", va="bottom", fontsize=9)

    ax.set_xticks(x)
    ax.set_xticklabels([SCENARIO_LABELS.get(s, s) for s in scenarios])
    ax.set_ylabel("Llaves Reportando (de 16)")
    ax.set_title("Cobertura de Telemetria: v0.13.0 vs v0.14.0")
    ax.axhline(y=16, color="gray", linestyle="--", alpha=0.5, label="Objetivo (16)")
    ax.set_ylim(0, 18)
    ax.legend()

    path = os.path.join(output_dir, f"comparison_keys.{fmt}")
    fig.savefig(path)
    plt.close(fig)
    print(f"  [4/6] {path}")
    return path


def plot_per_key_heatmap(v13, v14, output_dir, fmt):
    """Dual heatmap: per-key sample counts for v0.13 and v0.14."""
    plt.rcParams.update(STYLE)
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(18, 8), sharey=True)

    scenarios = [s for s in SCENARIO_ORDER
                 if s in v13.get("scenarios", {}) or s in v14.get("scenarios", {})]
    keys = TELEMETRY_KEYS_DISPLAY
    short_keys = [KEY_SHORT.get(k, k) for k in keys]

    def build_matrix(data):
        matrix = np.zeros((len(keys), len(scenarios)))
        for j, s in enumerate(scenarios):
            per_key = data.get("scenarios", {}).get(s, {}).get("per_key", {})
            for i, k in enumerate(keys):
                matrix[i, j] = per_key.get(k, {}).get("samples", 0)
        return matrix

    mat13 = build_matrix(v13)
    mat14 = build_matrix(v14)

    vmax = max(mat13.max(), mat14.max()) if mat13.max() > 0 or mat14.max() > 0 else 1

    im1 = ax1.imshow(mat13, cmap="YlOrRd", aspect="auto", vmin=0, vmax=vmax)
    ax1.set_xticks(range(len(scenarios)))
    ax1.set_xticklabels([SCENARIO_LABELS.get(s, s) for s in scenarios], fontsize=9)
    ax1.set_yticks(range(len(keys)))
    ax1.set_yticklabels(short_keys, fontsize=9)
    ax1.set_title("v0.13.0 — Muestras por Llave", fontsize=12)

    # Annotate cells
    for i in range(len(keys)):
        for j in range(len(scenarios)):
            val = int(mat13[i, j])
            color = "white" if val > vmax * 0.6 else "black"
            ax1.text(j, i, str(val), ha="center", va="center", fontsize=8, color=color)

    im2 = ax2.imshow(mat14, cmap="YlOrRd", aspect="auto", vmin=0, vmax=vmax)
    ax2.set_xticks(range(len(scenarios)))
    ax2.set_xticklabels([SCENARIO_LABELS.get(s, s) for s in scenarios], fontsize=9)
    ax2.set_title("v0.14.0 — Muestras por Llave", fontsize=12)

    for i in range(len(keys)):
        for j in range(len(scenarios)):
            val = int(mat14[i, j])
            color = "white" if val > vmax * 0.6 else "black"
            ax2.text(j, i, str(val), ha="center", va="center", fontsize=8, color=color)

    fig.colorbar(im2, ax=[ax1, ax2], shrink=0.8, label="Muestras")
    fig.suptitle("Cobertura de Muestras por Llave y Escenario", fontsize=14, y=1.02)
    fig.tight_layout()

    path = os.path.join(output_dir, f"comparison_heatmap.{fmt}")
    fig.savefig(path)
    plt.close(fig)
    print(f"  [5/6] {path}")
    return path


def plot_improvement_summary(v13, v14, output_dir, fmt):
    """Horizontal bar chart showing % improvement across all metrics."""
    plt.rcParams.update(STYLE)
    fig, ax = plt.subplots(figsize=(12, 8))

    metrics = []

    # Gather improvements per scenario
    for s in SCENARIO_ORDER:
        s13 = v13.get("scenarios", {}).get(s, {}).get("aggregate", {})
        s14 = v14.get("scenarios", {}).get(s, {}).get("aggregate", {})
        if not s13 or not s14:
            continue

        label = s.upper() if s != "baseline" else "BASE"

        # Throughput (higher = better)
        t13 = _safe(s13.get("overall_throughput_msgs_per_sec"))
        t14 = _safe(s14.get("overall_throughput_msgs_per_sec"))
        if t13 > 0:
            metrics.append((f"{label}: Throughput", (t14 - t13) / t13 * 100))

        # Messages (higher = better)
        m13 = _safe(s13.get("total_messages"))
        m14 = _safe(s14.get("total_messages"))
        if m13 > 0:
            metrics.append((f"{label}: Muestras", (m14 - m13) / m13 * 100))

        # IAT (lower = better, show negative as improvement)
        i13 = _safe(s13.get("iat_global_avg_s"))
        i14 = _safe(s14.get("iat_global_avg_s"))
        if i13 > 0:
            metrics.append((f"{label}: IAT", -(i14 - i13) / i13 * 100))

        # Keys reporting (higher = better)
        k13 = _safe(s13.get("total_keys_reporting"))
        k14 = _safe(s14.get("total_keys_reporting"))
        if k13 > 0:
            metrics.append((f"{label}: Llaves", (k14 - k13) / k13 * 100))

    if not metrics:
        print("  [6/6] No metrics to compare")
        return None

    labels, values = zip(*metrics)
    colors = [IMPROVE_COLOR if v >= 0 else REGRESS_COLOR for v in values]

    y_pos = np.arange(len(labels))
    ax.barh(y_pos, values, color=colors, edgecolor="white", height=0.6)

    for i, (lbl, val) in enumerate(zip(labels, values)):
        sign = "+" if val >= 0 else ""
        ax.text(val + (1 if val >= 0 else -1), i, f"{sign}{val:.1f}%",
                va="center", fontsize=9, fontweight="bold")

    ax.set_yticks(y_pos)
    ax.set_yticklabels(labels)
    ax.set_xlabel("Mejora (%)")
    ax.set_title("Resumen de Mejoras: v0.13.0 → v0.14.0")
    ax.axvline(x=0, color="black", linewidth=0.8)
    ax.invert_yaxis()

    path = os.path.join(output_dir, f"comparison_improvement.{fmt}")
    fig.savefig(path)
    plt.close(fig)
    print(f"  [6/6] {path}")
    return path


def generate_text_report(v13, v14, output_dir):
    """Generate a text comparison report."""
    lines = []
    lines.append("=" * 80)
    lines.append("REPORTE COMPARATIVO: Firmware v0.13.0 vs v0.14.0")
    lines.append("Tesis_jsgiraldod_2026_rev_final")
    lines.append(f"Generado: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 80)
    lines.append("")

    lines.append("CAMBIOS EN v0.14.0:")
    lines.append("  1. DLMS poll interval: 30s → 15s")
    lines.append("  2. Force-notify para valores sin cambio (nextafter)")
    lines.append("  3. RSSI/LQI nudge (±1 alternante)")
    lines.append("  4. Single-phase pre-skip (CONFIG_AMI_SINGLE_PHASE)")
    lines.append("  5. Thread dedicado para poll DLMS")
    lines.append("  6. Notify todos los 27 recursos")
    lines.append("")

    # Per-scenario comparison
    header = (
        f"{'Metrica':<25} {'v0.13.0':>12} {'v0.14.0':>12} {'Cambio':>12}"
    )

    for s in SCENARIO_ORDER:
        s13 = v13.get("scenarios", {}).get(s, {}).get("aggregate", {})
        s14 = v14.get("scenarios", {}).get(s, {}).get("aggregate", {})
        if not s13 and not s14:
            continue

        lines.append("-" * 80)
        lines.append(f"ESCENARIO: {s.upper()}")
        lines.append("-" * 80)
        lines.append(header)
        lines.append("-" * 65)

        comparisons = [
            ("Mensajes totales", "total_messages", True),
            ("Llaves reportando", "total_keys_reporting", True),
            ("Throughput (msgs/s)", "overall_throughput_msgs_per_sec", True),
            ("IAT promedio (s)", "iat_global_avg_s", False),
            ("IAT min (s)", "iat_global_min_s", False),
            ("IAT max (s)", "iat_global_max_s", False),
            ("CoAP bytes", "estimated_coap_bytes", True),
        ]

        for label, key, higher_better in comparisons:
            v1 = s13.get(key)
            v2 = s14.get(key)
            if v1 is None and v2 is None:
                continue
            v1_str = f"{v1:.3f}" if isinstance(v1, float) else str(v1) if v1 is not None else "-"
            v2_str = f"{v2:.3f}" if isinstance(v2, float) else str(v2) if v2 is not None else "-"

            if v1 and v2 and v1 != 0:
                pct = (v2 - v1) / abs(v1) * 100
                sign = "+" if pct >= 0 else ""
                change = f"{sign}{pct:.1f}%"
            else:
                change = "n/a"

            lines.append(f"  {label:<23} {v1_str:>12} {v2_str:>12} {change:>12}")

        # Per-key comparison
        lines.append("")
        lines.append(f"  {'Llave':<20} {'v13 muestras':>14} {'v14 muestras':>14} {'Cambio':>10}")
        lines.append("  " + "-" * 60)

        pk13 = v13.get("scenarios", {}).get(s, {}).get("per_key", {})
        pk14 = v14.get("scenarios", {}).get(s, {}).get("per_key", {})

        for k in TELEMETRY_KEYS_DISPLAY:
            n13 = pk13.get(k, {}).get("samples", 0)
            n14 = pk14.get(k, {}).get("samples", 0)
            if n13 == 0 and n14 == 0:
                change = "-"
            elif n13 == 0:
                change = "NEW!"
            else:
                pct = (n14 - n13) / n13 * 100
                change = f"{'+' if pct >= 0 else ''}{pct:.0f}%"
            lines.append(f"  {k:<20} {n13:>14} {n14:>14} {change:>10}")

        lines.append("")

    report = "\n".join(lines)

    path = os.path.join(output_dir, "comparison_report.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n  Report: {path}")

    # Also print to stdout
    print()
    print(report)
    return path


def main():
    parser = argparse.ArgumentParser(
        description="Compare benchmark results: v0.13.0 vs v0.14.0"
    )
    parser.add_argument("v13_dir", help="Directory with v0.13.0 benchmark results")
    parser.add_argument("v14_dir", help="Directory with v0.14.0 benchmark results")
    parser.add_argument("--output", "-o", default=None,
                        help="Output directory (default: results/comparison_v013_v014)")
    parser.add_argument("--format", "-f", default="png", choices=["png", "pdf", "svg"],
                        help="Image format (default: png)")

    args = parser.parse_args()

    # Default output
    if args.output is None:
        args.output = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "results", "comparison_v013_v014"
        )

    os.makedirs(args.output, exist_ok=True)

    print("=" * 60)
    print("  COMPARACION v0.13.0 vs v0.14.0")
    print("=" * 60)
    print(f"  v0.13.0: {args.v13_dir}")
    print(f"  v0.14.0: {args.v14_dir}")
    print(f"  Output:  {args.output}")
    print(f"  Format:  {args.format}")
    print("=" * 60)

    v13 = load_summary(args.v13_dir)
    v14 = load_summary(args.v14_dir)

    print(f"\n  v0.13.0 scenarios: {list(v13.get('scenarios', {}).keys())}")
    print(f"  v0.14.0 scenarios: {list(v14.get('scenarios', {}).keys())}")
    print(f"  v0.13.0 DLMS poll: {v13.get('dlms_poll_interval_s')}s")
    print(f"  v0.14.0 DLMS poll: {v14.get('dlms_poll_interval_s')}s")

    if not HAS_MPL:
        print("\n  WARNING: matplotlib not available, generating text report only")
        generate_text_report(v13, v14, args.output)
        return

    print("\n  Generating comparison graphs...")
    files = []
    files.append(plot_throughput_comparison(v13, v14, args.output, args.format))
    files.append(plot_samples_comparison(v13, v14, args.output, args.format))
    files.append(plot_iat_comparison(v13, v14, args.output, args.format))
    files.append(plot_keys_reporting(v13, v14, args.output, args.format))
    files.append(plot_per_key_heatmap(v13, v14, args.output, args.format))
    files.append(plot_improvement_summary(v13, v14, args.output, args.format))

    generate_text_report(v13, v14, args.output)

    print(f"\n  Comparison complete! {len([f for f in files if f])} graphs + report generated.")
    print(f"  Output: {args.output}")


if __name__ == "__main__":
    main()
