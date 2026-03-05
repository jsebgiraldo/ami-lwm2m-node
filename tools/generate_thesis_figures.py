#!/usr/bin/env python3
"""
generate_thesis_figures.py — Generador de figuras para tesis
=============================================================
Tesis: Tesis_jsgiraldod_2026_rev_final
Firmware: v0.18.0 (PUSH_FIELD + pmin/pmax delegación al motor LwM2M)

Lee benchmark_summary.json y raw_ts_*.csv de results/benchmark/
y genera figuras de calidad para publicación.

Uso:
  python generate_thesis_figures.py                          # Ultimo benchmark
  python generate_thesis_figures.py --input results/benchmark/20260304_212754
  python generate_thesis_figures.py --output results/thesis_figures
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np

# ═══════════════════════════════════════════════════════════════════
# Style Configuration — Publication Ready
# ═══════════════════════════════════════════════════════════════════

plt.rcParams.update({
    "figure.figsize": (10, 6),
    "figure.dpi": 150,
    "savefig.dpi": 300,
    "font.size": 11,
    "axes.titlesize": 13,
    "axes.labelsize": 12,
    "xtick.labelsize": 10,
    "ytick.labelsize": 10,
    "legend.fontsize": 10,
    "axes.grid": True,
    "grid.alpha": 0.3,
    "axes.spines.top": False,
    "axes.spines.right": False,
})

# Scenario colors (color-blind safe palette)
COLORS = {
    "baseline": "#2196F3",   # blue
    "1s": "#F44336",         # red
    "5s": "#4CAF50",         # green
}

SCENARIO_LABELS = {
    "baseline": "Producción\n(G1: 15/30s, G2: 60/300s)",
    "1s": "Agresivo\n(pmin=1, pmax=1)",
    "5s": "Medio\n(pmin=5, pmax=5)",
}

SCENARIO_LABELS_SHORT = {
    "baseline": "Producción",
    "1s": "Agresivo (1s)",
    "5s": "Medio (5s)",
}

SCENARIO_ORDER = ["baseline", "1s", "5s"]

# Key categories for grouping
GRUPO1_KEYS = ["voltage", "current", "activePower", "activeEnergy"]
GRUPO2_KEYS = [
    "reactivePower", "apparentPower", "powerFactor",
    "totalActivePower", "totalReactivePower", "totalApparentPower",
    "totalPowerFactor", "reactiveEnergy", "apparentEnergy", "frequency",
]
RADIO_KEYS = ["radioSignalStrength", "linkQuality"]
ALL_KEYS = GRUPO1_KEYS + GRUPO2_KEYS + RADIO_KEYS

KEY_LABELS_SHORT = {
    "voltage": "V",
    "current": "I",
    "activePower": "P",
    "reactivePower": "Q",
    "apparentPower": "S",
    "powerFactor": "PF",
    "totalActivePower": "Ptot",
    "totalReactivePower": "Qtot",
    "totalApparentPower": "Stot",
    "totalPowerFactor": "PFtot",
    "activeEnergy": "Ea",
    "reactiveEnergy": "Er",
    "apparentEnergy": "Es",
    "frequency": "f",
    "radioSignalStrength": "RSSI",
    "linkQuality": "LQI",
}


def load_benchmark(input_dir):
    """Load benchmark_summary.json and raw time series."""
    summary_path = os.path.join(input_dir, "benchmark_summary.json")
    if not os.path.exists(summary_path):
        print(f"ERROR: No se encontro {summary_path}")
        sys.exit(1)
    with open(summary_path, "r", encoding="utf-8") as f:
        summary = json.load(f)

    # Load raw time series
    raw_ts = {}
    for scenario in SCENARIO_ORDER:
        csv_path = os.path.join(input_dir, f"raw_ts_{scenario}.csv")
        if os.path.exists(csv_path):
            samples = []
            with open(csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    samples.append({
                        "ts": int(row["timestamp_ms"]),
                        "key": row["key"],
                        "value": row["value"],
                    })
            raw_ts[scenario] = samples
    return summary, raw_ts


# ═══════════════════════════════════════════════════════════════════
# Figure 1: Throughput & Message Count per Scenario (Dual-Axis Bar)
# ═══════════════════════════════════════════════════════════════════

def fig_throughput(summary, output_dir):
    """Bar chart: total messages + throughput per scenario."""
    scenarios = summary.get("scenarios", {})
    names = [s for s in SCENARIO_ORDER if s in scenarios]

    msgs = [scenarios[s]["aggregate"]["total_messages"] for s in names]
    thr = [scenarios[s]["aggregate"]["overall_throughput_msgs_per_sec"] for s in names]
    colors = [COLORS[s] for s in names]
    labels = [SCENARIO_LABELS_SHORT[s] for s in names]

    fig, ax1 = plt.subplots(figsize=(8, 5))
    x = np.arange(len(names))
    w = 0.4

    bars = ax1.bar(x, msgs, w, color=colors, alpha=0.85, edgecolor="white",
                   linewidth=1.5, label="Mensajes totales")
    ax1.set_ylabel("Mensajes totales (300s ventana)")
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels)

    # Value labels on bars
    for bar, val in zip(bars, msgs):
        ax1.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 10,
                 str(val), ha="center", va="bottom", fontweight="bold", fontsize=11)

    ax2 = ax1.twinx()
    ax2.plot(x, thr, "ko-", markersize=8, linewidth=2, label="Throughput")
    for xi, ti in zip(x, thr):
        ax2.annotate(f"{ti:.2f}", (xi, ti), textcoords="offset points",
                     xytext=(0, 10), ha="center", fontsize=10)
    ax2.set_ylabel("Throughput (msgs/s)")
    ax2.spines["right"].set_visible(True)

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left")

    ax1.set_title("Rendimiento del nodo AMI — Mensajes por escenario LwM2M")
    fig.tight_layout()
    path = os.path.join(output_dir, "fig_throughput.png")
    fig.savefig(path)
    plt.close(fig)
    return path


# ═══════════════════════════════════════════════════════════════════
# Figure 2: Per-Key Message Count (Grouped Bar)
# ═══════════════════════════════════════════════════════════════════

def fig_per_key_messages(summary, output_dir):
    """Grouped bar chart: message count per key for each scenario."""
    scenarios = summary.get("scenarios", {})
    names = [s for s in SCENARIO_ORDER if s in scenarios]

    fig, ax = plt.subplots(figsize=(14, 6))
    x = np.arange(len(ALL_KEYS))
    n_scenarios = len(names)
    w = 0.8 / n_scenarios

    for i, s in enumerate(names):
        per_key = scenarios[s].get("per_key", {})
        counts = [per_key.get(k, {}).get("samples", 0) for k in ALL_KEYS]
        offset = (i - n_scenarios / 2 + 0.5) * w
        bars = ax.bar(x + offset, counts, w, color=COLORS[s], alpha=0.85,
                      label=SCENARIO_LABELS_SHORT[s], edgecolor="white", linewidth=0.5)

    ax.set_ylabel("Mensajes recibidos (300s)")
    ax.set_xticks(x)
    ax.set_xticklabels([KEY_LABELS_SHORT[k] for k in ALL_KEYS], rotation=45, ha="right")
    ax.legend()

    # Vertical separators between groups
    ax.axvline(x=len(GRUPO1_KEYS) - 0.5, color="gray", linestyle="--", alpha=0.4)
    ax.axvline(x=len(GRUPO1_KEYS) + len(GRUPO2_KEYS) - 0.5,
               color="gray", linestyle="--", alpha=0.4)
    # Group labels
    ax.text(len(GRUPO1_KEYS) / 2 - 0.5, ax.get_ylim()[1] * 0.95,
            "Grupo 1", ha="center", fontsize=9, style="italic", color="gray")
    ax.text(len(GRUPO1_KEYS) + len(GRUPO2_KEYS) / 2 - 0.5, ax.get_ylim()[1] * 0.95,
            "Grupo 2", ha="center", fontsize=9, style="italic", color="gray")
    ax.text(len(GRUPO1_KEYS) + len(GRUPO2_KEYS) + len(RADIO_KEYS) / 2 - 0.5,
            ax.get_ylim()[1] * 0.95,
            "Radio", ha="center", fontsize=9, style="italic", color="gray")

    ax.set_title("Mensajes por recurso LwM2M — Comparación de escenarios")
    fig.tight_layout()
    path = os.path.join(output_dir, "fig_per_key_messages.png")
    fig.savefig(path)
    plt.close(fig)
    return path


# ═══════════════════════════════════════════════════════════════════
# Figure 3: IAT Distribution per Scenario (Box Plot)
# ═══════════════════════════════════════════════════════════════════

def fig_iat_boxplot(summary, raw_ts, output_dir):
    """Box plot of inter-arrival times per scenario, computed from raw data."""
    scenarios = summary.get("scenarios", {})
    names = [s for s in SCENARIO_ORDER if s in scenarios and s in raw_ts]

    fig, ax = plt.subplots(figsize=(10, 6))
    box_data = []
    box_labels = []
    box_colors = []

    for s in names:
        # Compute IATs from raw time series
        by_key = {}
        for sample in raw_ts[s]:
            k = sample["key"]
            by_key.setdefault(k, []).append(sample["ts"])

        all_iats = []
        for k, timestamps in by_key.items():
            timestamps.sort()
            for i in range(1, len(timestamps)):
                iat = (timestamps[i] - timestamps[i - 1]) / 1000.0
                if iat > 0:
                    all_iats.append(iat)

        if all_iats:
            box_data.append(all_iats)
            box_labels.append(SCENARIO_LABELS_SHORT[s])
            box_colors.append(COLORS[s])

    if not box_data:
        plt.close(fig)
        return None

    bp = ax.boxplot(box_data, tick_labels=box_labels, patch_artist=True,
                    showfliers=True, flierprops=dict(marker=".", markersize=3, alpha=0.3),
                    medianprops=dict(color="black", linewidth=2),
                    whiskerprops=dict(linewidth=1.2),
                    capprops=dict(linewidth=1.2))

    for patch, color in zip(bp["boxes"], box_colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.7)

    ax.set_ylabel("Inter-Arrival Time (s)")
    ax.set_title("Distribución del tiempo inter-arribo (IAT) por escenario")
    ax.set_yscale("log")
    ax.yaxis.set_major_formatter(ticker.ScalarFormatter())
    ax.yaxis.set_minor_formatter(ticker.NullFormatter())

    # Add median annotations
    for i, data in enumerate(box_data, 1):
        med = np.median(data)
        ax.annotate(f"med={med:.1f}s", xy=(i, med),
                    xytext=(15, 5), textcoords="offset points",
                    fontsize=9, color="black",
                    arrowprops=dict(arrowstyle="-", color="gray", lw=0.5))

    fig.tight_layout()
    path = os.path.join(output_dir, "fig_iat_boxplot.png")
    fig.savefig(path)
    plt.close(fig)
    return path


# ═══════════════════════════════════════════════════════════════════
# Figure 4: Completeness Heatmap (Key × Scenario)
# ═══════════════════════════════════════════════════════════════════

def fig_completeness_heatmap(summary, output_dir):
    """Heatmap showing message count per key × scenario."""
    scenarios = summary.get("scenarios", {})
    names = [s for s in SCENARIO_ORDER if s in scenarios]

    # Build matrix
    matrix = []
    for k in ALL_KEYS:
        row = []
        for s in names:
            n = scenarios[s].get("per_key", {}).get(k, {}).get("samples", 0)
            row.append(n)
        matrix.append(row)
    matrix = np.array(matrix, dtype=float)

    fig, ax = plt.subplots(figsize=(8, 9))
    im = ax.imshow(matrix, aspect="auto", cmap="YlOrRd", interpolation="nearest")

    ax.set_xticks(np.arange(len(names)))
    ax.set_xticklabels([SCENARIO_LABELS_SHORT[s] for s in names])
    ax.set_yticks(np.arange(len(ALL_KEYS)))
    ax.set_yticklabels([KEY_LABELS_SHORT[k] for k in ALL_KEYS])

    # Annotate cells
    for i in range(len(ALL_KEYS)):
        for j in range(len(names)):
            val = int(matrix[i, j])
            text_color = "white" if val > matrix.max() * 0.6 else "black"
            ax.text(j, i, str(val), ha="center", va="center",
                    fontsize=10, fontweight="bold", color=text_color)

    # Group separators
    ax.axhline(y=len(GRUPO1_KEYS) - 0.5, color="white", linewidth=2)
    ax.axhline(y=len(GRUPO1_KEYS) + len(GRUPO2_KEYS) - 0.5,
               color="white", linewidth=2)

    # Group labels on the right
    ax.text(len(names) + 0.3, len(GRUPO1_KEYS) / 2 - 0.5, "G1",
            va="center", fontsize=10, style="italic", color="gray")
    ax.text(len(names) + 0.3, len(GRUPO1_KEYS) + len(GRUPO2_KEYS) / 2 - 0.5,
            "G2", va="center", fontsize=10, style="italic", color="gray")
    ax.text(len(names) + 0.3,
            len(GRUPO1_KEYS) + len(GRUPO2_KEYS) + len(RADIO_KEYS) / 2 - 0.5,
            "Radio", va="center", fontsize=10, style="italic", color="gray")

    cbar = plt.colorbar(im, ax=ax, shrink=0.8, label="Mensajes recibidos")
    ax.set_title("Cobertura de telemetría — Mensajes por recurso y escenario")
    fig.tight_layout()
    path = os.path.join(output_dir, "fig_completeness_heatmap.png")
    fig.savefig(path)
    plt.close(fig)
    return path


# ═══════════════════════════════════════════════════════════════════
# Figure 5: RSSI & LQI Time Series
# ═══════════════════════════════════════════════════════════════════

def fig_rssi_lqi_timeline(summary, raw_ts, output_dir):
    """Time series plot of RSSI and LQI from the baseline scenario."""
    # Use baseline for stable radio assessment; fallback to first available
    for prefer in ["baseline", "1s", "5s"]:
        if prefer in raw_ts:
            scenario = prefer
            break
    else:
        return None

    samples = raw_ts[scenario]
    rssi_ts = [(s["ts"], float(s["value"])) for s in samples
               if s["key"] == "radioSignalStrength"]
    lqi_ts = [(s["ts"], float(s["value"])) for s in samples
              if s["key"] == "linkQuality"]

    if not rssi_ts and not lqi_ts:
        return None

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), sharex=True)

    # RSSI
    if rssi_ts:
        t0 = rssi_ts[0][0]
        rssi_t = [(ts - t0) / 1000.0 for ts, _ in rssi_ts]
        rssi_v = [v for _, v in rssi_ts]
        ax1.plot(rssi_t, rssi_v, "o-", color="#1976D2", markersize=5,
                 linewidth=1.5, label="RSSI")
        ax1.axhline(np.mean(rssi_v), color="#1976D2", linestyle="--",
                    alpha=0.5, label=f"Media: {np.mean(rssi_v):.1f} dBm")
        ax1.fill_between(rssi_t,
                         np.mean(rssi_v) - np.std(rssi_v),
                         np.mean(rssi_v) + np.std(rssi_v),
                         alpha=0.15, color="#1976D2", label=f"±1σ ({np.std(rssi_v):.1f})")
        ax1.set_ylabel("RSSI (dBm)")
        ax1.legend(loc="lower left")
        ax1.set_title(f"Estabilidad del canal radio — Escenario: {SCENARIO_LABELS_SHORT[scenario]}")

    # LQI
    if lqi_ts:
        t0 = lqi_ts[0][0] if not rssi_ts else rssi_ts[0][0]
        lqi_t = [(ts - t0) / 1000.0 for ts, _ in lqi_ts]
        lqi_v = [v for _, v in lqi_ts]
        ax2.plot(lqi_t, lqi_v, "s-", color="#388E3C", markersize=5,
                 linewidth=1.5, label="LQI")
        ax2.axhline(np.mean(lqi_v), color="#388E3C", linestyle="--",
                    alpha=0.5, label=f"Media: {np.mean(lqi_v):.1f}%")
        ax2.fill_between(lqi_t,
                         np.mean(lqi_v) - np.std(lqi_v),
                         np.mean(lqi_v) + np.std(lqi_v),
                         alpha=0.15, color="#388E3C", label=f"±1σ ({np.std(lqi_v):.1f})")
        ax2.set_ylabel("LQI (%)")
        ax2.set_xlabel("Tiempo (s)")
        ax2.legend(loc="lower left")

    fig.tight_layout()
    path = os.path.join(output_dir, "fig_rssi_lqi_timeline.png")
    fig.savefig(path)
    plt.close(fig)
    return path


# ═══════════════════════════════════════════════════════════════════
# Figure 6: CoAP Overhead Comparison
# ═══════════════════════════════════════════════════════════════════

def fig_coap_overhead(summary, output_dir):
    """Bar chart of estimated CoAP overhead per scenario."""
    scenarios = summary.get("scenarios", {})
    names = [s for s in SCENARIO_ORDER if s in scenarios]

    coap_kb = [scenarios[s]["aggregate"]["estimated_coap_bytes"] / 1024 for s in names]
    coap_bps = [scenarios[s]["aggregate"]["estimated_coap_bps"] for s in names]
    colors = [COLORS[s] for s in names]
    labels = [SCENARIO_LABELS_SHORT[s] for s in names]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    # KB total
    bars1 = ax1.bar(labels, coap_kb, color=colors, alpha=0.85,
                    edgecolor="white", linewidth=1.5)
    for bar, val in zip(bars1, coap_kb):
        ax1.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.5,
                 f"{val:.1f}", ha="center", va="bottom", fontweight="bold")
    ax1.set_ylabel("Overhead CoAP (KB)")
    ax1.set_title("Tráfico CoAP total (300s)")

    # bps
    bars2 = ax2.bar(labels, coap_bps, color=colors, alpha=0.85,
                    edgecolor="white", linewidth=1.5)
    for bar, val in zip(bars2, coap_bps):
        ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 10,
                 f"{val:.0f}", ha="center", va="bottom", fontweight="bold")
    ax2.set_ylabel("Tasa estimada (bps)")
    ax2.set_title("Ancho de banda CoAP estimado")

    fig.suptitle("Sobrecarga de protocolo CoAP/LwM2M — IEEE 802.15.4", fontsize=13)
    fig.tight_layout()
    path = os.path.join(output_dir, "fig_coap_overhead.png")
    fig.savefig(path)
    plt.close(fig)
    return path


# ═══════════════════════════════════════════════════════════════════
# Figure 7: IAT per Key Heatmap
# ═══════════════════════════════════════════════════════════════════

def fig_iat_heatmap(summary, output_dir):
    """Heatmap of average IAT per key × scenario."""
    scenarios = summary.get("scenarios", {})
    names = [s for s in SCENARIO_ORDER if s in scenarios]

    matrix = []
    for k in ALL_KEYS:
        row = []
        for s in names:
            iat = scenarios[s].get("per_key", {}).get(k, {}).get("iat_avg_s")
            row.append(iat if iat is not None else float("nan"))
        matrix.append(row)
    matrix = np.array(matrix)

    fig, ax = plt.subplots(figsize=(8, 9))
    # Use masked array for NaN handling
    masked = np.ma.masked_invalid(matrix)
    im = ax.imshow(masked, aspect="auto", cmap="viridis_r", interpolation="nearest")

    ax.set_xticks(np.arange(len(names)))
    ax.set_xticklabels([SCENARIO_LABELS_SHORT[s] for s in names])
    ax.set_yticks(np.arange(len(ALL_KEYS)))
    ax.set_yticklabels([KEY_LABELS_SHORT[k] for k in ALL_KEYS])

    # Annotate cells
    for i in range(len(ALL_KEYS)):
        for j in range(len(names)):
            val = matrix[i, j]
            if np.isnan(val):
                ax.text(j, i, "—", ha="center", va="center",
                        fontsize=10, color="gray")
            else:
                text_color = "white" if val > np.nanmax(matrix) * 0.5 else "black"
                ax.text(j, i, f"{val:.1f}", ha="center", va="center",
                        fontsize=9, fontweight="bold", color=text_color)

    # Group separators
    ax.axhline(y=len(GRUPO1_KEYS) - 0.5, color="white", linewidth=2)
    ax.axhline(y=len(GRUPO1_KEYS) + len(GRUPO2_KEYS) - 0.5,
               color="white", linewidth=2)

    cbar = plt.colorbar(im, ax=ax, shrink=0.8, label="IAT promedio (s)")
    ax.set_title("Tiempo inter-arribo promedio (IAT) por recurso y escenario")
    fig.tight_layout()
    path = os.path.join(output_dir, "fig_iat_heatmap.png")
    fig.savefig(path)
    plt.close(fig)
    return path


# ═══════════════════════════════════════════════════════════════════
# Figure 8: Aggregate Summary Dashboard
# ═══════════════════════════════════════════════════════════════════

def fig_summary_dashboard(summary, output_dir):
    """Multi-panel summary figure for thesis chapter opening."""
    scenarios = summary.get("scenarios", {})
    names = [s for s in SCENARIO_ORDER if s in scenarios]
    colors = [COLORS[s] for s in names]
    labels = [SCENARIO_LABELS_SHORT[s] for s in names]

    fig, axes = plt.subplots(2, 2, figsize=(12, 9))

    # Panel A: Messages
    msgs = [scenarios[s]["aggregate"]["total_messages"] for s in names]
    axes[0, 0].bar(labels, msgs, color=colors, alpha=0.85)
    for i, v in enumerate(msgs):
        axes[0, 0].text(i, v + 5, str(v), ha="center", fontweight="bold")
    axes[0, 0].set_title("(a) Total de mensajes")
    axes[0, 0].set_ylabel("Mensajes")

    # Panel B: Throughput
    thr = [scenarios[s]["aggregate"]["overall_throughput_msgs_per_sec"] for s in names]
    axes[0, 1].bar(labels, thr, color=colors, alpha=0.85)
    for i, v in enumerate(thr):
        axes[0, 1].text(i, v + 0.02, f"{v:.2f}", ha="center", fontweight="bold")
    axes[0, 1].set_title("(b) Throughput")
    axes[0, 1].set_ylabel("msgs/s")

    # Panel C: IAT average
    iats = [scenarios[s]["aggregate"].get("iat_global_avg_s", 0) or 0 for s in names]
    axes[1, 0].bar(labels, iats, color=colors, alpha=0.85)
    for i, v in enumerate(iats):
        axes[1, 0].text(i, v + 0.3, f"{v:.1f}s", ha="center", fontweight="bold")
    axes[1, 0].set_title("(c) IAT promedio global")
    axes[1, 0].set_ylabel("IAT (s)")

    # Panel D: CoAP overhead
    coap = [scenarios[s]["aggregate"]["estimated_coap_bytes"] / 1024 for s in names]
    axes[1, 1].bar(labels, coap, color=colors, alpha=0.85)
    for i, v in enumerate(coap):
        axes[1, 1].text(i, v + 0.3, f"{v:.1f}K", ha="center", fontweight="bold")
    axes[1, 1].set_title("(d) Overhead CoAP")
    axes[1, 1].set_ylabel("KB (300s)")

    fig.suptitle(
        "Resumen de rendimiento — Nodo AMI v0.18.0 sobre Thread/IEEE 802.15.4",
        fontsize=14, fontweight="bold"
    )
    fig.tight_layout()
    path = os.path.join(output_dir, "fig_summary_dashboard.png")
    fig.savefig(path)
    plt.close(fig)
    return path


# ═══════════════════════════════════════════════════════════════════
# Figure 9: Time Series — Voltage and Active Power (1s scenario)
# ═══════════════════════════════════════════════════════════════════

def fig_meter_timeseries(summary, raw_ts, output_dir):
    """Time series of key meter values from the scenario with most data."""
    # Pick scenario with most samples
    best_scenario = None
    best_count = 0
    for s, samples in raw_ts.items():
        count = len(samples)
        if count > best_count:
            best_count = count
            best_scenario = s

    if not best_scenario:
        return None

    samples = raw_ts[best_scenario]
    keys_to_plot = ["activePower", "apparentPower", "frequency"]
    existing_keys = []

    fig, axes = plt.subplots(len(keys_to_plot), 1, figsize=(14, 3.5 * len(keys_to_plot)),
                             sharex=True)
    if len(keys_to_plot) == 1:
        axes = [axes]

    t0 = min(s["ts"] for s in samples) if samples else 0

    for idx, key in enumerate(keys_to_plot):
        key_samples = [(s["ts"], s["value"]) for s in samples if s["key"] == key]
        if not key_samples:
            axes[idx].text(0.5, 0.5, f"{key}: sin datos", transform=axes[idx].transAxes,
                          ha="center", va="center", fontsize=12, color="gray")
            continue
        existing_keys.append(key)
        ts_vals = sorted(key_samples, key=lambda x: x[0])
        t = [(ts - t0) / 1000.0 for ts, _ in ts_vals]
        v = []
        for _, val in ts_vals:
            try:
                v.append(float(val))
            except (ValueError, TypeError):
                v.append(float("nan"))

        axes[idx].plot(t, v, ".-", color=COLORS.get(best_scenario, "#333"),
                       markersize=3, linewidth=1)
        axes[idx].set_ylabel(key)
        axes[idx].set_title(f"{key} — {len(key_samples)} muestras")

    axes[-1].set_xlabel("Tiempo (s)")
    fig.suptitle(
        f"Series temporales de medición — Escenario: {SCENARIO_LABELS_SHORT.get(best_scenario, best_scenario)}",
        fontsize=13, fontweight="bold"
    )
    fig.tight_layout()
    path = os.path.join(output_dir, "fig_meter_timeseries.png")
    fig.savefig(path)
    plt.close(fig)
    return path


# ═══════════════════════════════════════════════════════════════════
# Figure 10: Keys Reporting Coverage
# ═══════════════════════════════════════════════════════════════════

def fig_keys_reporting(summary, output_dir):
    """Stacked bar showing keys reporting vs missing per scenario."""
    scenarios = summary.get("scenarios", {})
    names = [s for s in SCENARIO_ORDER if s in scenarios]
    labels = [SCENARIO_LABELS_SHORT[s] for s in names]

    reporting = [scenarios[s]["aggregate"]["total_keys_reporting"] for s in names]
    missing = [scenarios[s]["aggregate"]["total_keys_expected"] - r for r, s
               in zip(reporting, names)]

    fig, ax = plt.subplots(figsize=(8, 5))
    x = np.arange(len(names))
    ax.bar(x, reporting, color="#4CAF50", alpha=0.85, label="Reportando")
    ax.bar(x, missing, bottom=reporting, color="#F44336", alpha=0.6, label="Sin datos")

    for i in range(len(names)):
        ax.text(i, reporting[i] / 2, str(reporting[i]),
                ha="center", va="center", fontweight="bold", color="white")
        if missing[i] > 0:
            ax.text(i, reporting[i] + missing[i] / 2, str(missing[i]),
                    ha="center", va="center", fontweight="bold", color="white")

    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_ylabel("Recursos LwM2M")
    ax.set_title("Cobertura de recursos — Claves con datos vs sin datos")
    ax.legend()
    ax.set_ylim(0, 18)
    fig.tight_layout()
    path = os.path.join(output_dir, "fig_keys_reporting.png")
    fig.savefig(path)
    plt.close(fig)
    return path


# ═══════════════════════════════════════════════════════════════════
# LaTeX Table Generation
# ═══════════════════════════════════════════════════════════════════

def generate_latex_tables(summary, output_dir):
    """Generate LaTeX tables for direct inclusion in thesis."""
    scenarios = summary.get("scenarios", {})
    names = [s for s in SCENARIO_ORDER if s in scenarios]

    lines = []

    # Table 1: Aggregate metrics
    lines.append(r"% ═══════════════════════════════════════════════════════════")
    lines.append(r"% Tabla: Metricas agregadas por escenario")
    lines.append(r"% Generada automaticamente — v0.18.0")
    lines.append(r"% ═══════════════════════════════════════════════════════════")
    lines.append(r"\begin{table}[htbp]")
    lines.append(r"\centering")
    lines.append(r"\caption{Rendimiento del nodo AMI bajo diferentes configuraciones de observación LwM2M}")
    lines.append(r"\label{tab:benchmark-aggregate}")
    lines.append(r"\footnotesize")
    lines.append(r"\begin{tabular}{lrrrrrrr}")
    lines.append(r"\toprule")
    lines.append(
        r"\textbf{Escenario} & \textbf{Msgs} & \textbf{Keys} & "
        r"\textbf{Msgs/s} & \textbf{IAT (s)} & "
        r"\textbf{CoAP (KB)} & \textbf{RSSI} & \textbf{LQI} \\"
    )
    lines.append(r"\midrule")

    for s in names:
        agg = scenarios[s]["aggregate"]
        label = SCENARIO_LABELS_SHORT[s].replace("(", "\\textrm{(").replace(")", ")}")
        rssi = agg.get("rssi_avg_dBm")
        lqi = agg.get("lqi_avg_pct")
        iat = agg.get("iat_global_avg_s")
        iat_str = f"{iat:.1f}" if iat else "--"
        rssi_str = f"{rssi:.1f}" if rssi else "--"
        lqi_str = f"{lqi:.1f}" if lqi else "--"
        coap_kb = agg['estimated_coap_bytes'] / 1024
        lines.append(
            f"{label} & "
            f"{agg['total_messages']} & "
            f"{agg['total_keys_reporting']}/{agg['total_keys_expected']} & "
            f"{agg['overall_throughput_msgs_per_sec']:.3f} & "
            f"{iat_str} & "
            f"{coap_kb:.1f} & "
            f"{rssi_str} & "
            f"{lqi_str} \\\\"
        )

    lines.append(r"\bottomrule")
    lines.append(r"\end{tabular}")
    lines.append(r"\end{table}")
    lines.append("")

    # Table 2: Per-key samples
    lines.append(r"\begin{table}[htbp]")
    lines.append(r"\centering")
    lines.append(r"\caption{Mensajes recibidos por recurso LwM2M en ventana de 300\,s}")
    lines.append(r"\label{tab:benchmark-per-key}")
    lines.append(r"\footnotesize")
    cols = "l" + "r" * len(names)
    lines.append(r"\begin{tabular}{" + cols + "}")
    lines.append(r"\toprule")
    header = r"\textbf{Recurso}"
    for s in names:
        header += f" & \\textbf{{{SCENARIO_LABELS_SHORT[s]}}}"
    header += r" \\"
    lines.append(header)
    lines.append(r"\midrule")

    for k in ALL_KEYS:
        row = KEY_LABELS_SHORT[k]
        for s in names:
            n = scenarios[s].get("per_key", {}).get(k, {}).get("samples", 0)
            row += f" & {n}"
        row += r" \\"
        lines.append(row)

    lines.append(r"\bottomrule")
    lines.append(r"\end{tabular}")
    lines.append(r"\end{table}")

    path = os.path.join(output_dir, "latex_tables.tex")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════

def find_latest_benchmark(base_dir):
    """Find the most recent benchmark directory."""
    benchmark_dir = os.path.join(base_dir, "results", "benchmark")
    if not os.path.isdir(benchmark_dir):
        return None
    dirs = sorted([
        d for d in os.listdir(benchmark_dir)
        if os.path.isdir(os.path.join(benchmark_dir, d))
    ])
    if not dirs:
        return None
    return os.path.join(benchmark_dir, dirs[-1])


def main():
    parser = argparse.ArgumentParser(
        description="Generador de figuras para tesis — v0.18.0")
    parser.add_argument("--input", "-i", default=None,
                        help="Directorio de benchmark (default: ultimo)")
    parser.add_argument("--output", "-o", default=None,
                        help="Directorio de salida (default: results/thesis_figures)")
    args = parser.parse_args()

    # Find input directory
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    if args.input:
        input_dir = args.input
    else:
        input_dir = find_latest_benchmark(base_dir)
    if not input_dir or not os.path.isdir(input_dir):
        print(f"ERROR: Directorio de benchmark no encontrado: {input_dir}")
        sys.exit(1)

    # Output directory
    if args.output:
        output_dir = args.output
    else:
        output_dir = os.path.join(base_dir, "results", "thesis_figures")
    os.makedirs(output_dir, exist_ok=True)

    print("=" * 60)
    print("  GENERADOR DE FIGURAS — Tesis v0.18.0")
    print("=" * 60)
    print(f"  Input  : {input_dir}")
    print(f"  Output : {output_dir}")

    # Load data
    print("\n  Cargando datos...")
    summary, raw_ts = load_benchmark(input_dir)
    n_scenarios = len(summary.get("scenarios", {}))
    n_raw = sum(len(v) for v in raw_ts.values())
    print(f"  Escenarios: {n_scenarios}")
    print(f"  Muestras raw: {n_raw}")

    # Generate all figures
    print("\n  Generando figuras...")
    results = []

    generators = [
        ("fig_throughput", fig_throughput, (summary, output_dir)),
        ("fig_per_key_messages", fig_per_key_messages, (summary, output_dir)),
        ("fig_iat_boxplot", fig_iat_boxplot, (summary, raw_ts, output_dir)),
        ("fig_completeness_heatmap", fig_completeness_heatmap, (summary, output_dir)),
        ("fig_rssi_lqi_timeline", fig_rssi_lqi_timeline, (summary, raw_ts, output_dir)),
        ("fig_coap_overhead", fig_coap_overhead, (summary, output_dir)),
        ("fig_iat_heatmap", fig_iat_heatmap, (summary, output_dir)),
        ("fig_summary_dashboard", fig_summary_dashboard, (summary, output_dir)),
        ("fig_meter_timeseries", fig_meter_timeseries, (summary, raw_ts, output_dir)),
        ("fig_keys_reporting", fig_keys_reporting, (summary, output_dir)),
    ]

    for name, func, args_tuple in generators:
        try:
            path = func(*args_tuple)
            if path:
                size_kb = os.path.getsize(path) / 1024
                print(f"    ✓ {name:30s} ({size_kb:.0f} KB)")
                results.append((name, path))
            else:
                print(f"    ○ {name:30s} (sin datos suficientes)")
        except Exception as e:
            print(f"    ✗ {name:30s} ERROR: {e}")
            import traceback
            traceback.print_exc()

    # Generate LaTeX tables
    print("\n  Generando tablas LaTeX...")
    try:
        tex_path = generate_latex_tables(summary, output_dir)
        print(f"    ✓ latex_tables.tex")
        results.append(("latex_tables", tex_path))
    except Exception as e:
        print(f"    ✗ latex_tables ERROR: {e}")

    # Summary
    print(f"\n{'=' * 60}")
    print(f"  FIGURAS GENERADAS: {len(results)}")
    print(f"  Directorio: {output_dir}")
    print(f"{'=' * 60}")
    for name, path in results:
        fname = os.path.basename(path)
        size_kb = os.path.getsize(path) / 1024
        print(f"    {fname:40s} {size_kb:6.1f} KB")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
