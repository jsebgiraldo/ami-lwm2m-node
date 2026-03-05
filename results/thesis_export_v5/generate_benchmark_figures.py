#!/usr/bin/env python3
"""
generate_benchmark_figures.py
Genera figuras de benchmark para thesis_export_v5 (v0.17.0).

Lee los CSVs y JSON del benchmark y genera graficas de:
  1. Throughput comparativo por escenario
  2. IAT (Inter-Arrival Time) por recurso y escenario
  3. Timeline de telemetria cruda
  4. Overhead CoAP y RSSI/LQI
  5. Completitud por recurso (heatmap)

Uso:
  python generate_benchmark_figures.py [--benchmark-dir <path>]

Si no se especifica --benchmark-dir, busca el directorio mas reciente
en results/benchmark/.
"""

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np

# ── Configuracion visual ──────────────────────────────────────────
plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["DejaVu Sans", "Arial", "Helvetica"],
    "font.size": 10,
    "axes.titlesize": 12,
    "axes.labelsize": 11,
    "xtick.labelsize": 9,
    "ytick.labelsize": 9,
    "legend.fontsize": 9,
    "figure.dpi": 150,
    "figure.figsize": (10, 6),
    "axes.grid": True,
    "grid.alpha": 0.3,
})

SCENARIO_COLORS = {
    "baseline": "#2196F3",
    "1s": "#FF5722",
    "5s": "#4CAF50",
}

SCENARIO_LABELS = {
    "baseline": "Baseline (pmin=15s)",
    "1s": "Agresivo (pmin=1s)",
    "5s": "Moderado (pmin=5s)",
}

SCENARIO_ORDER = ["baseline", "1s", "5s"]

# Grupos de llaves para graficas por-recurso
KEY_GROUPS = {
    "Potencia": ["activePower", "reactivePower", "apparentPower", "powerFactor"],
    "Totales": ["totalActivePower", "totalReactivePower", "totalApparentPower", "totalPowerFactor"],
    "Energia": ["activeEnergy", "reactiveEnergy", "apparentEnergy"],
    "Red": ["voltage", "current", "frequency"],
    "Radio": ["radioSignalStrength", "linkQuality"],
}


def find_latest_benchmark_dir(base_dir):
    """Find the most recent benchmark output directory."""
    benchmark_base = os.path.join(base_dir, "..", "benchmark")
    if not os.path.isdir(benchmark_base):
        print(f"ERROR: No existe {benchmark_base}")
        sys.exit(1)
    dirs = sorted([
        d for d in os.listdir(benchmark_base)
        if os.path.isdir(os.path.join(benchmark_base, d))
    ])
    if not dirs:
        print("ERROR: No hay directorios de benchmark")
        sys.exit(1)
    return os.path.join(benchmark_base, dirs[-1])


def load_summary(benchmark_dir):
    """Load benchmark_summary.json."""
    path = os.path.join(benchmark_dir, "benchmark_summary.json")
    if not os.path.isfile(path):
        print(f"ERROR: No existe {path}")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_per_key_csv(benchmark_dir, scenario):
    """Load per_key_<scenario>.csv into list of dicts."""
    path = os.path.join(benchmark_dir, f"per_key_{scenario}.csv")
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


def load_raw_ts_csv(benchmark_dir, scenario):
    """Load raw_ts_<scenario>.csv into list of dicts."""
    path = os.path.join(benchmark_dir, f"raw_ts_{scenario}.csv")
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            row["timestamp_ms"] = int(row["timestamp_ms"])
            row["value"] = float(row["value"]) if row["value"] else None
            rows.append(row)
        return rows


def fig1_throughput_comparison(summary, output_dir):
    """Bar chart comparing throughput, messages, and completeness across scenarios."""
    fig, axes = plt.subplots(1, 3, figsize=(14, 5))

    scenarios = []
    msgs = []
    throughputs = []
    completeness = []
    colors = []

    for name in SCENARIO_ORDER:
        if name not in summary.get("scenarios", {}):
            continue
        agg = summary["scenarios"][name].get("aggregate", {})
        scenarios.append(SCENARIO_LABELS.get(name, name))
        msgs.append(agg.get("total_messages", 0))
        throughputs.append(agg.get("overall_throughput_msgs_per_sec", 0))
        completeness.append(agg.get("overall_completeness_pct", 0))
        colors.append(SCENARIO_COLORS.get(name, "#999"))

    x = np.arange(len(scenarios))
    width = 0.6

    # Total messages
    ax = axes[0]
    bars = ax.bar(x, msgs, width, color=colors, edgecolor="white", linewidth=0.8)
    ax.set_title("Total de Mensajes")
    ax.set_ylabel("Mensajes")
    ax.set_xticks(x)
    ax.set_xticklabels(scenarios, rotation=15, ha="right", fontsize=8)
    for bar, val in zip(bars, msgs):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1,
                str(val), ha="center", va="bottom", fontsize=9, fontweight="bold")

    # Throughput
    ax = axes[1]
    bars = ax.bar(x, throughputs, width, color=colors, edgecolor="white", linewidth=0.8)
    ax.set_title("Throughput")
    ax.set_ylabel("msgs/s")
    ax.set_xticks(x)
    ax.set_xticklabels(scenarios, rotation=15, ha="right", fontsize=8)
    for bar, val in zip(bars, throughputs):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.01,
                f"{val:.3f}", ha="center", va="bottom", fontsize=9, fontweight="bold")

    # Completeness
    ax = axes[2]
    bars = ax.bar(x, completeness, width, color=colors, edgecolor="white", linewidth=0.8)
    ax.set_title("Completitud")
    ax.set_ylabel("%")
    ax.set_xticks(x)
    ax.set_xticklabels(scenarios, rotation=15, ha="right", fontsize=8)
    for bar, val in zip(bars, completeness):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1,
                f"{val:.1f}%", ha="center", va="bottom", fontsize=9, fontweight="bold")

    fig.suptitle("Rendimiento LwM2M por Escenario de Observacion — v0.17.0",
                 fontsize=13, fontweight="bold", y=1.02)
    fig.tight_layout()
    path = os.path.join(output_dir, "fig_benchmark_throughput.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  [1/5] {path} ({os.path.getsize(path)/1024:.1f} KB)")
    return path


def fig2_iat_per_resource(per_key_data, output_dir):
    """Grouped bar chart of IAT avg per key across scenarios."""
    # Collect keys that have data in any scenario
    all_keys = []
    for scenario in SCENARIO_ORDER:
        for row in per_key_data.get(scenario, []):
            if row["key"] not in all_keys:
                all_keys.append(row["key"])

    # Filter to keys with meaningful IAT data
    power_keys = [k for k in all_keys if k not in ("voltage", "current", "powerFactor", "totalPowerFactor")]

    if not power_keys:
        print("  [2/5] SKIP: No hay datos IAT por recurso")
        return None

    fig, ax = plt.subplots(figsize=(14, 6))
    x = np.arange(len(power_keys))
    n_scenarios = sum(1 for s in SCENARIO_ORDER if s in per_key_data)
    width = 0.8 / max(n_scenarios, 1)

    for i, scenario in enumerate(SCENARIO_ORDER):
        if scenario not in per_key_data:
            continue
        key_map = {row["key"]: row for row in per_key_data[scenario]}
        iats = []
        for key in power_keys:
            row = key_map.get(key, {})
            val = row.get("iat_avg_s", "")
            iats.append(float(val) if val else 0)

        offset = (i - n_scenarios / 2 + 0.5) * width
        bars = ax.bar(x + offset, iats, width,
                      label=SCENARIO_LABELS.get(scenario, scenario),
                      color=SCENARIO_COLORS.get(scenario, "#999"),
                      edgecolor="white", linewidth=0.5)

    ax.set_xlabel("Recurso LwM2M")
    ax.set_ylabel("IAT Promedio (s)")
    ax.set_title("Inter-Arrival Time Promedio por Recurso y Escenario — v0.17.0",
                 fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels(power_keys, rotation=45, ha="right", fontsize=8)
    ax.legend(loc="upper right")

    fig.tight_layout()
    path = os.path.join(output_dir, "fig_benchmark_iat.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  [2/5] {path} ({os.path.getsize(path)/1024:.1f} KB)")
    return path


def fig3_timeline(raw_data, output_dir):
    """Timeline scatter plot of raw telemetry for each scenario."""
    n_scenarios = sum(1 for s in SCENARIO_ORDER if s in raw_data and raw_data[s])
    if n_scenarios == 0:
        print("  [3/5] SKIP: No hay datos timeseries")
        return None

    fig, axes = plt.subplots(n_scenarios, 1, figsize=(14, 4 * n_scenarios),
                             squeeze=False)

    idx = 0
    for scenario in SCENARIO_ORDER:
        if scenario not in raw_data or not raw_data[scenario]:
            continue
        ax = axes[idx, 0]
        rows = raw_data[scenario]

        # Group by key
        by_key = defaultdict(list)
        for row in rows:
            by_key[row["key"]].append(row["timestamp_ms"])

        # Assign y-index to each key
        keys_sorted = sorted(by_key.keys())
        key_to_y = {k: i for i, k in enumerate(keys_sorted)}

        for key, timestamps in by_key.items():
            y = key_to_y[key]
            ts_sec = [(t - min(timestamps)) / 1000 for t in timestamps]
            ax.scatter(ts_sec, [y] * len(ts_sec), s=8, alpha=0.7,
                       color=SCENARIO_COLORS.get(scenario, "#999"),
                       marker="|", linewidths=1.5)

        ax.set_yticks(range(len(keys_sorted)))
        ax.set_yticklabels(keys_sorted, fontsize=8)
        ax.set_xlabel("Tiempo relativo (s)")
        ax.set_title(f"Timeline de Telemetria — {SCENARIO_LABELS.get(scenario, scenario)}",
                     fontweight="bold")
        ax.set_xlim(left=-5)
        idx += 1

    fig.suptitle("Distribucion Temporal de Mensajes LwM2M — v0.17.0",
                 fontsize=13, fontweight="bold", y=1.01)
    fig.tight_layout()
    path = os.path.join(output_dir, "fig_benchmark_timeline.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  [3/5] {path} ({os.path.getsize(path)/1024:.1f} KB)")
    return path


def fig4_coap_rssi(summary, output_dir):
    """Side-by-side: CoAP overhead and RSSI/LQI per scenario."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    scenarios = []
    coap_kb = []
    coap_bps = []
    rssi = []
    lqi = []
    colors = []

    for name in SCENARIO_ORDER:
        if name not in summary.get("scenarios", {}):
            continue
        agg = summary["scenarios"][name].get("aggregate", {})
        scenarios.append(SCENARIO_LABELS.get(name, name))
        coap_kb.append(agg.get("estimated_coap_bytes", 0) / 1024)
        coap_bps.append(agg.get("estimated_coap_bps", 0))
        rssi.append(agg.get("rssi_avg_dBm", 0) or 0)
        lqi.append(agg.get("lqi_avg_pct", 0) or 0)
        colors.append(SCENARIO_COLORS.get(name, "#999"))

    x = np.arange(len(scenarios))
    width = 0.35

    # CoAP overhead
    bars1 = ax1.bar(x - width / 2, coap_kb, width, label="CoAP (KB)",
                    color=colors, edgecolor="white", linewidth=0.8)
    ax1_twin = ax1.twinx()
    bars2 = ax1_twin.bar(x + width / 2, coap_bps, width, label="CoAP (bps)",
                         color=[c + "80" for c in colors],
                         edgecolor="white", linewidth=0.8, alpha=0.6)
    ax1.set_title("Overhead CoAP Estimado")
    ax1.set_ylabel("KB totales")
    ax1_twin.set_ylabel("bps")
    ax1.set_xticks(x)
    ax1.set_xticklabels(scenarios, rotation=15, ha="right", fontsize=8)
    ax1.legend(loc="upper left")
    ax1_twin.legend(loc="upper right")

    for bar, val in zip(bars1, coap_kb):
        ax1.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.1,
                 f"{val:.1f}", ha="center", va="bottom", fontsize=8)

    # RSSI/LQI
    bars3 = ax2.bar(x - width / 2, rssi, width, label="RSSI (dBm)",
                    color="#E91E63", edgecolor="white", linewidth=0.8)
    ax2_twin = ax2.twinx()
    bars4 = ax2_twin.bar(x + width / 2, lqi, width, label="LQI (%)",
                         color="#00BCD4", edgecolor="white", linewidth=0.8)
    ax2.set_title("Calidad del Enlace Radio")
    ax2.set_ylabel("RSSI (dBm)")
    ax2_twin.set_ylabel("LQI (%)")
    ax2.set_xticks(x)
    ax2.set_xticklabels(scenarios, rotation=15, ha="right", fontsize=8)
    ax2.legend(loc="upper left")
    ax2_twin.legend(loc="upper right")

    fig.suptitle("Overhead de Red y Calidad de Enlace — v0.17.0",
                 fontsize=13, fontweight="bold", y=1.02)
    fig.tight_layout()
    path = os.path.join(output_dir, "fig_benchmark_coap_rssi.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  [4/5] {path} ({os.path.getsize(path)/1024:.1f} KB)")
    return path


def fig5_completeness_heatmap(per_key_data, output_dir):
    """Heatmap of completeness % per key per scenario."""
    # Collect all keys
    all_keys = []
    for scenario in SCENARIO_ORDER:
        for row in per_key_data.get(scenario, []):
            if row["key"] not in all_keys:
                all_keys.append(row["key"])

    if not all_keys:
        print("  [5/5] SKIP: No hay datos para heatmap")
        return None

    scenarios_present = [s for s in SCENARIO_ORDER if s in per_key_data]
    matrix = np.zeros((len(all_keys), len(scenarios_present)))

    for j, scenario in enumerate(scenarios_present):
        key_map = {row["key"]: row for row in per_key_data[scenario]}
        for i, key in enumerate(all_keys):
            row = key_map.get(key, {})
            val = row.get("samples", "0")
            matrix[i, j] = int(val) if val else 0

    fig, ax = plt.subplots(figsize=(8, max(6, len(all_keys) * 0.4)))

    im = ax.imshow(matrix, cmap="YlOrRd", aspect="auto", interpolation="nearest")

    ax.set_xticks(range(len(scenarios_present)))
    ax.set_xticklabels([SCENARIO_LABELS.get(s, s) for s in scenarios_present],
                       rotation=15, ha="right", fontsize=9)
    ax.set_yticks(range(len(all_keys)))
    ax.set_yticklabels(all_keys, fontsize=9)
    ax.set_title("Muestras Recibidas por Recurso y Escenario — v0.17.0",
                 fontweight="bold", pad=15)

    # Annotate cells
    for i in range(len(all_keys)):
        for j in range(len(scenarios_present)):
            val = int(matrix[i, j])
            text_color = "white" if val > matrix.max() * 0.6 else "black"
            ax.text(j, i, str(val), ha="center", va="center",
                    fontsize=9, fontweight="bold", color=text_color)

    cbar = fig.colorbar(im, ax=ax, shrink=0.8)
    cbar.set_label("Muestras")

    fig.tight_layout()
    path = os.path.join(output_dir, "fig_benchmark_completeness.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  [5/5] {path} ({os.path.getsize(path)/1024:.1f} KB)")
    return path


def main():
    parser = argparse.ArgumentParser(description="Genera figuras de benchmark")
    parser.add_argument("--benchmark-dir", type=str, default=None,
                        help="Directorio con los resultados del benchmark")
    args = parser.parse_args()

    # Resolve paths
    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_dir = os.path.join(script_dir, "figuras")
    os.makedirs(output_dir, exist_ok=True)

    if args.benchmark_dir:
        benchmark_dir = args.benchmark_dir
    else:
        benchmark_dir = find_latest_benchmark_dir(script_dir)

    print(f"\n{'=' * 60}")
    print(f"  GENERADOR DE FIGURAS DE BENCHMARK")
    print(f"  Benchmark dir: {benchmark_dir}")
    print(f"  Output dir:    {output_dir}")
    print(f"{'=' * 60}\n")

    # Load data
    print("  Cargando datos...")
    summary = load_summary(benchmark_dir)

    per_key_data = {}
    raw_data = {}
    for scenario in SCENARIO_ORDER:
        pk = load_per_key_csv(benchmark_dir, scenario)
        if pk:
            per_key_data[scenario] = pk
        rt = load_raw_ts_csv(benchmark_dir, scenario)
        if rt:
            raw_data[scenario] = rt

    scenarios_found = list(per_key_data.keys())
    print(f"  Escenarios con datos: {scenarios_found}")
    print(f"  Generando figuras...\n")

    # Generate figures
    paths = []
    paths.append(fig1_throughput_comparison(summary, output_dir))
    paths.append(fig2_iat_per_resource(per_key_data, output_dir))
    paths.append(fig3_timeline(raw_data, output_dir))
    paths.append(fig4_coap_rssi(summary, output_dir))
    paths.append(fig5_completeness_heatmap(per_key_data, output_dir))

    generated = [p for p in paths if p]
    print(f"\n{'=' * 60}")
    print(f"  COMPLETADO: {len(generated)}/5 figuras generadas")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
