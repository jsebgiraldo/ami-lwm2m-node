#!/usr/bin/env python3
"""
graph_benchmark.py — Generate thesis-ready graphs from benchmark results
=========================================================================
Thesis: Tesis_jsgiraldod_2026_rev_final

Reads benchmark_summary.json and produces publication-quality plots:
  1. Throughput comparison bar chart
  2. Inter-arrival time box plots per scenario
  3. Completeness heatmap (key x scenario)
  4. RSSI/LQI stability over scenarios
  5. CoAP overhead comparison
  6. Per-key IAT timeline (if raw CSV available)

Usage:
  python graph_benchmark.py results/benchmark/YYYYMMDD_HHMMSS
  python graph_benchmark.py results/benchmark/YYYYMMDD_HHMMSS --format pdf
  python graph_benchmark.py results/benchmark/YYYYMMDD_HHMMSS --no-show
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime

try:
    import matplotlib
    matplotlib.use("Agg")  # Non-interactive backend
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker
    import numpy as np
    HAS_MPL = True
except ImportError:
    HAS_MPL = False


# Scenario display order and colors
SCENARIO_ORDER = ["baseline", "1s", "5s", "10s"]
SCENARIO_LABELS = {
    "baseline": "Baseline\n(15/30s, 60/300s)",
    "1s": "Agresivo\n(1s)",
    "5s": "Medio\n(5s)",
    "10s": "Relajado\n(10s)",
}
SCENARIO_COLORS = {
    "baseline": "#2196F3",
    "1s": "#F44336",
    "5s": "#FF9800",
    "10s": "#4CAF50",
}

# Telemetry keys in display order
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


def load_summary(result_dir):
    """Load benchmark_summary.json."""
    path = os.path.join(result_dir, "benchmark_summary.json")
    if not os.path.exists(path):
        print(f"ERROR: {path} not found")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_raw_csv(result_dir, scenario_name):
    """Load raw time series CSV for a scenario."""
    path = os.path.join(result_dir, f"raw_ts_{scenario_name}.csv")
    if not os.path.exists(path):
        return None
    rows = []
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            r["timestamp_ms"] = int(r["timestamp_ms"])
            rows.append(r)
    return rows


def get_scenarios(summary):
    """Get available scenarios in display order."""
    available = [s for s in SCENARIO_ORDER if s in summary.get("scenarios", {})]
    return available


def fig_throughput(summary, scenarios, output_dir, fmt):
    """Bar chart: Total throughput (msgs/s) per scenario."""
    fig, ax = plt.subplots(figsize=(8, 5))

    labels = [SCENARIO_LABELS.get(s, s) for s in scenarios]
    values = []
    for s in scenarios:
        agg = summary["scenarios"][s].get("aggregate", {})
        values.append(agg.get("overall_throughput_msgs_per_sec", 0))

    colors = [SCENARIO_COLORS.get(s, "#999") for s in scenarios]
    bars = ax.bar(labels, values, color=colors, edgecolor="black", linewidth=0.5)

    for bar, val in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.01,
                f"{val:.3f}", ha="center", va="bottom", fontsize=10, fontweight="bold")

    ax.set_ylabel("Mensajes / segundo", fontsize=12)
    ax.set_title("Throughput por Escenario de Observacion LwM2M", fontsize=13, fontweight="bold")
    ax.set_ylim(0, max(values) * 1.2 if values else 1)
    ax.grid(axis="y", alpha=0.3)

    path = os.path.join(output_dir, f"fig_throughput.{fmt}")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def fig_completeness(summary, scenarios, output_dir, fmt):
    """Heatmap: Data completeness (%) per key per scenario."""
    keys = TELEMETRY_KEYS_DISPLAY
    short_keys = [KEY_SHORT.get(k, k) for k in keys]

    data = []
    for s in scenarios:
        row = []
        per_key = summary["scenarios"][s].get("per_key", {})
        for k in keys:
            km = per_key.get(k, {})
            row.append(km.get("completeness_pct", 0))
        data.append(row)

    data = np.array(data)

    fig, ax = plt.subplots(figsize=(12, 4))
    im = ax.imshow(data, cmap="RdYlGn", vmin=0, vmax=150, aspect="auto")

    ax.set_xticks(range(len(short_keys)))
    ax.set_xticklabels(short_keys, rotation=45, ha="right", fontsize=9)
    ax.set_yticks(range(len(scenarios)))
    ax.set_yticklabels([SCENARIO_LABELS.get(s, s).replace("\n", " ") for s in scenarios], fontsize=10)

    # Annotate cells
    for i in range(len(scenarios)):
        for j in range(len(keys)):
            val = data[i, j]
            color = "white" if val < 30 or val > 120 else "black"
            ax.text(j, i, f"{val:.0f}", ha="center", va="center", fontsize=7, color=color)

    ax.set_title("Completitud de Datos (%) por Recurso y Escenario", fontsize=13, fontweight="bold")
    fig.colorbar(im, ax=ax, label="Completitud (%)", shrink=0.8)

    path = os.path.join(output_dir, f"fig_completeness.{fmt}")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def fig_iat_boxplot(summary, scenarios, output_dir, fmt, result_dir):
    """Box plot: Inter-arrival time distribution per scenario."""
    fig, ax = plt.subplots(figsize=(10, 5))

    box_data = []
    labels = []
    colors = []

    for s in scenarios:
        raw = load_raw_csv(result_dir, s)
        if raw is None:
            continue

        # Compute IATs from raw data
        by_key = {}
        for r in raw:
            k = r["key"]
            by_key.setdefault(k, []).append(r["timestamp_ms"])

        iats = []
        for k, timestamps in by_key.items():
            timestamps.sort()
            for i in range(1, len(timestamps)):
                delta_s = (timestamps[i] - timestamps[i - 1]) / 1000.0
                if delta_s < 600:  # filter outliers > 10 min
                    iats.append(delta_s)

        if iats:
            box_data.append(iats)
            labels.append(SCENARIO_LABELS.get(s, s).replace("\n", " "))
            colors.append(SCENARIO_COLORS.get(s, "#999"))

    if not box_data:
        plt.close(fig)
        return None

    bp = ax.boxplot(box_data, labels=labels, patch_artist=True, showfliers=False,
                    medianprops=dict(color="black", linewidth=2))

    for patch, color in zip(bp["boxes"], colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.7)

    ax.set_ylabel("Inter-Arrival Time (s)", fontsize=12)
    ax.set_title("Distribucion de Tiempos Entre Notificaciones LwM2M", fontsize=13, fontweight="bold")
    ax.grid(axis="y", alpha=0.3)

    path = os.path.join(output_dir, f"fig_iat_boxplot.{fmt}")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def fig_coap_overhead(summary, scenarios, output_dir, fmt):
    """Stacked bar: Estimated CoAP overhead per scenario."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    labels = [SCENARIO_LABELS.get(s, s).replace("\n", " ") for s in scenarios]
    bytes_kb = []
    bps_vals = []
    for s in scenarios:
        agg = summary["scenarios"][s].get("aggregate", {})
        bytes_kb.append(agg.get("estimated_coap_bytes", 0) / 1024)
        bps_vals.append(agg.get("estimated_coap_bps", 0))

    colors = [SCENARIO_COLORS.get(s, "#999") for s in scenarios]

    # KB total
    bars1 = ax1.bar(labels, bytes_kb, color=colors, edgecolor="black", linewidth=0.5)
    for bar, val in zip(bars1, bytes_kb):
        ax1.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.5,
                 f"{val:.1f}", ha="center", va="bottom", fontsize=9)
    ax1.set_ylabel("KB totales")
    ax1.set_title("Overhead CoAP Estimado (KB)", fontweight="bold")
    ax1.grid(axis="y", alpha=0.3)

    # bps
    bars2 = ax2.bar(labels, bps_vals, color=colors, edgecolor="black", linewidth=0.5)
    for bar, val in zip(bars2, bps_vals):
        ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1,
                 f"{val:.0f}", ha="center", va="bottom", fontsize=9)
    ax2.set_ylabel("bits/segundo")
    ax2.set_title("Tasa de datos CoAP (bps)", fontweight="bold")
    ax2.grid(axis="y", alpha=0.3)

    path = os.path.join(output_dir, f"fig_coap_overhead.{fmt}")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def fig_rssi_lqi(summary, scenarios, output_dir, fmt):
    """Dual-axis chart: RSSI (dBm) and LQI (%) per scenario."""
    fig, ax1 = plt.subplots(figsize=(8, 5))

    x = range(len(scenarios))
    labels = [SCENARIO_LABELS.get(s, s).replace("\n", " ") for s in scenarios]

    rssi_vals = []
    lqi_vals = []
    rssi_std = []
    lqi_std = []

    for s in scenarios:
        agg = summary["scenarios"][s].get("aggregate", {})
        rssi_vals.append(agg.get("rssi_avg_dBm") or 0)
        lqi_vals.append(agg.get("lqi_avg_pct") or 0)
        rssi_std.append(agg.get("rssi_stddev_dBm") or 0)
        lqi_std.append(agg.get("lqi_stddev_pct") or 0)

    # RSSI on left axis
    ax1.bar([i - 0.2 for i in x], [-v for v in rssi_vals], width=0.35,
            color="#2196F3", alpha=0.8, label="RSSI (|dBm|)", edgecolor="black", linewidth=0.5)
    ax1.set_ylabel("|RSSI| (dBm)", color="#2196F3", fontsize=12)
    ax1.set_xticks(list(x))
    ax1.set_xticklabels(labels, fontsize=9)

    # LQI on right axis
    ax2 = ax1.twinx()
    ax2.bar([i + 0.2 for i in x], lqi_vals, width=0.35,
            color="#4CAF50", alpha=0.8, label="LQI (%)", edgecolor="black", linewidth=0.5)
    ax2.set_ylabel("LQI (%)", color="#4CAF50", fontsize=12)
    ax2.set_ylim(0, 100)

    ax1.set_title("Estabilidad de Enlace Radio por Escenario", fontsize=13, fontweight="bold")

    # Combined legend
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper right")

    path = os.path.join(output_dir, f"fig_rssi_lqi.{fmt}")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def fig_iat_per_key(summary, scenarios, output_dir, fmt):
    """Grouped bar chart: Average IAT per key per scenario."""
    keys = TELEMETRY_KEYS_DISPLAY
    short_keys = [KEY_SHORT.get(k, k) for k in keys]

    fig, ax = plt.subplots(figsize=(14, 6))

    n_scenarios = len(scenarios)
    width = 0.8 / n_scenarios
    x = np.arange(len(keys))

    for i, s in enumerate(scenarios):
        per_key = summary["scenarios"][s].get("per_key", {})
        vals = []
        for k in keys:
            km = per_key.get(k, {})
            vals.append(km.get("iat_avg_s") or 0)

        offset = (i - n_scenarios / 2 + 0.5) * width
        ax.bar(x + offset, vals, width, label=SCENARIO_LABELS.get(s, s).replace("\n", " "),
               color=SCENARIO_COLORS.get(s, "#999"), alpha=0.8, edgecolor="black", linewidth=0.3)

    ax.set_xticks(x)
    ax.set_xticklabels(short_keys, rotation=45, ha="right", fontsize=9)
    ax.set_ylabel("IAT Promedio (s)", fontsize=12)
    ax.set_title("Tiempo Entre Notificaciones por Recurso y Escenario", fontsize=13, fontweight="bold")
    ax.legend(fontsize=9)
    ax.grid(axis="y", alpha=0.3)

    path = os.path.join(output_dir, f"fig_iat_per_key.{fmt}")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def main():
    parser = argparse.ArgumentParser(description="Generate benchmark graphs for thesis")
    parser.add_argument("result_dir", help="Path to benchmark results directory")
    parser.add_argument("--format", "-f", default="png", choices=["png", "pdf", "svg"],
                        help="Output image format (default: png)")
    parser.add_argument("--no-show", action="store_true", help="Don't display plots")
    args = parser.parse_args()

    if not HAS_MPL:
        print("ERROR: matplotlib and numpy are required.")
        print("  pip install matplotlib numpy")
        sys.exit(1)

    summary = load_summary(args.result_dir)
    scenarios = get_scenarios(summary)

    if not scenarios:
        print("ERROR: No scenarios found in summary")
        sys.exit(1)

    print(f"Escenarios disponibles: {', '.join(scenarios)}")
    print(f"Formato: {args.format}")

    graphs = []

    print("\n[1/6] Throughput comparison...")
    g = fig_throughput(summary, scenarios, args.result_dir, args.format)
    graphs.append(g)
    print(f"  -> {g}")

    print("[2/6] Completeness heatmap...")
    g = fig_completeness(summary, scenarios, args.result_dir, args.format)
    graphs.append(g)
    print(f"  -> {g}")

    print("[3/6] IAT box plot...")
    g = fig_iat_boxplot(summary, scenarios, args.result_dir, args.format, args.result_dir)
    if g:
        graphs.append(g)
        print(f"  -> {g}")
    else:
        print("  -> (skipped, no raw CSV data)")

    print("[4/6] CoAP overhead...")
    g = fig_coap_overhead(summary, scenarios, args.result_dir, args.format)
    graphs.append(g)
    print(f"  -> {g}")

    print("[5/6] RSSI/LQI stability...")
    g = fig_rssi_lqi(summary, scenarios, args.result_dir, args.format)
    graphs.append(g)
    print(f"  -> {g}")

    print("[6/6] IAT per key...")
    g = fig_iat_per_key(summary, scenarios, args.result_dir, args.format)
    graphs.append(g)
    print(f"  -> {g}")

    print(f"\n{'=' * 50}")
    print(f"  {len(graphs)} graficos generados en {args.result_dir}")
    print(f"{'=' * 50}")
    for g in graphs:
        if g:
            print(f"  {os.path.basename(g)}")


if __name__ == "__main__":
    main()
