#!/usr/bin/env python3
"""
graph_v014_v0141_comparison.py — Before/After: v0.14.0 vs v0.14.1
====================================================================
Thesis: Tesis_jsgiraldod_2026_rev_final

Compares benchmark results before and after the Object 4 ConnMon version
fix that enabled RSSI/LQI telemetry reporting.

Key change in v0.14.1:
  - CONFIG_LWM2M_CONNMON_OBJECT_VERSION_1_0 → _1_3
  - Fixes LwM2M Object 4 version mismatch with TB profile paths (/4_1.3/...)
  - Enables radioSignalStrength (RSSI) and linkQuality (LQI) reporting
  - Result: 14/16 → 16/16 telemetry keys reporting

Usage:
  python tools/graph_v014_v0141_comparison.py \\
      results/benchmark_v014 \\
      results/benchmark_v0141 \\
      --output results/comparison_v014_v0141
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

V140_COLOR = "#78909C"   # blue-grey
V141_COLOR = "#AB47BC"   # purple (distinct from the teal used for v0.14 earlier)
IMPROVE_COLOR = "#66BB6A"
REGRESS_COLOR = "#EF5350"
RSSI_HIGHLIGHT = "#FF7043"  # orange-red for RSSI/LQI highlight

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


# ═══════════════════════════════════════════════════════════════════
# Graph 1: Keys Reporting (highlight RSSI/LQI fix)
# ═══════════════════════════════════════════════════════════════════

def plot_keys_reporting(v140, v141, output_dir, fmt):
    """Bar chart: keys reporting — highlight 14→16 fix."""
    plt.rcParams.update(STYLE)
    fig, ax = plt.subplots(figsize=(12, 6))

    scenarios = [s for s in SCENARIO_ORDER
                 if s in v140.get("scenarios", {}) or s in v141.get("scenarios", {})]
    x = np.arange(len(scenarios))
    w = 0.35

    vals_140 = []
    vals_141 = []
    for s in scenarios:
        s140 = v140.get("scenarios", {}).get(s, {}).get("aggregate", {})
        s141 = v141.get("scenarios", {}).get(s, {}).get("aggregate", {})
        vals_140.append(_safe(s140.get("total_keys_reporting")))
        vals_141.append(_safe(s141.get("total_keys_reporting")))

    bars1 = ax.bar(x - w/2, vals_140, w, label="v0.14.0 (Object 4 v1.0)",
                   color=V140_COLOR, edgecolor="white")
    bars2 = ax.bar(x + w/2, vals_141, w, label="v0.14.1 (Object 4 v1.3)",
                   color=V141_COLOR, edgecolor="white")

    for bar in bars1:
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2, h + 0.15, f"{int(h)}/16",
                ha="center", va="bottom", fontsize=10, fontweight="bold")
    for bar in bars2:
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2, h + 0.15, f"{int(h)}/16",
                ha="center", va="bottom", fontsize=10, fontweight="bold",
                color=IMPROVE_COLOR if h >= 16 else "black")

    # Annotate the fix
    for i, (v1, v2) in enumerate(zip(vals_140, vals_141)):
        if v2 > v1:
            ax.annotate(f"+{int(v2-v1)} keys\n(RSSI+LQI)",
                        xy=(x[i] + w/2, v2 + 0.5),
                        ha="center", fontsize=9, fontweight="bold",
                        color=IMPROVE_COLOR)

    ax.set_xticks(x)
    ax.set_xticklabels([SCENARIO_LABELS.get(s, s) for s in scenarios])
    ax.set_ylabel("Llaves Reportando (de 16)")
    ax.set_title("Cobertura de Telemetria: v0.14.0 vs v0.14.1\n"
                 "(Fix: Object 4 ConnMon v1.0 → v1.3)")
    ax.axhline(y=16, color="gray", linestyle="--", alpha=0.5, label="Objetivo (16)")
    ax.set_ylim(0, 19)
    ax.legend(loc="lower right")

    path = os.path.join(output_dir, f"comparison_keys_v0141.{fmt}")
    fig.savefig(path)
    plt.close(fig)
    print(f"  [1/6] {path}")
    return path


# ═══════════════════════════════════════════════════════════════════
# Graph 2: Per-Key Heatmap (RSSI/LQI highlighted)
# ═══════════════════════════════════════════════════════════════════

def plot_per_key_heatmap(v140, v141, output_dir, fmt):
    """Dual heatmap: per-key samples, highlighting RSSI/LQI rows."""
    plt.rcParams.update(STYLE)
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(18, 8), sharey=True)

    scenarios = [s for s in SCENARIO_ORDER
                 if s in v140.get("scenarios", {}) or s in v141.get("scenarios", {})]
    keys = TELEMETRY_KEYS_DISPLAY
    short_keys = [KEY_SHORT.get(k, k) for k in keys]

    def build_matrix(data):
        matrix = np.zeros((len(keys), len(scenarios)))
        for j, s in enumerate(scenarios):
            per_key = data.get("scenarios", {}).get(s, {}).get("per_key", {})
            for i, k in enumerate(keys):
                matrix[i, j] = per_key.get(k, {}).get("samples", 0)
        return matrix

    mat140 = build_matrix(v140)
    mat141 = build_matrix(v141)

    vmax = max(mat140.max(), mat141.max()) if mat140.max() > 0 or mat141.max() > 0 else 1

    im1 = ax1.imshow(mat140, cmap="YlOrRd", aspect="auto", vmin=0, vmax=vmax)
    ax1.set_xticks(range(len(scenarios)))
    ax1.set_xticklabels([SCENARIO_LABELS.get(s, s) for s in scenarios], fontsize=9)
    ax1.set_yticks(range(len(keys)))
    ax1.set_yticklabels(short_keys, fontsize=9)
    ax1.set_title("v0.14.0 — Muestras por Llave", fontsize=12)

    # Annotate cells for ax1
    rssi_idx = keys.index("radioSignalStrength")
    lqi_idx = keys.index("linkQuality")

    for i in range(len(keys)):
        for j in range(len(scenarios)):
            val = int(mat140[i, j])
            color = "white" if val > vmax * 0.6 else "black"
            if val == 0 and i in [rssi_idx, lqi_idx]:
                color = RSSI_HIGHLIGHT
                ax1.text(j, i, "0 !", ha="center", va="center", fontsize=8,
                         color=color, fontweight="bold")
            else:
                ax1.text(j, i, str(val), ha="center", va="center", fontsize=8,
                         color=color)

    im2 = ax2.imshow(mat141, cmap="YlOrRd", aspect="auto", vmin=0, vmax=vmax)
    ax2.set_xticks(range(len(scenarios)))
    ax2.set_xticklabels([SCENARIO_LABELS.get(s, s) for s in scenarios], fontsize=9)
    ax2.set_yticks(range(len(keys)))
    ax2.set_yticklabels(short_keys, fontsize=9)
    ax2.set_title("v0.14.1 — Muestras por Llave", fontsize=12)

    for i in range(len(keys)):
        for j in range(len(scenarios)):
            val = int(mat141[i, j])
            color = "white" if val > vmax * 0.6 else "black"
            if i in [rssi_idx, lqi_idx] and val > 0:
                ax2.text(j, i, str(val), ha="center", va="center", fontsize=8,
                         color=color, fontweight="bold")
            else:
                ax2.text(j, i, str(val), ha="center", va="center", fontsize=8,
                         color=color)

    # Highlight RSSI/LQI rows on both axes (after both have yticklabels set)
    for ax in [ax1, ax2]:
        labels = ax.get_yticklabels()
        for idx in [rssi_idx, lqi_idx]:
            if idx < len(labels):
                labels[idx].set_color(RSSI_HIGHLIGHT)
                labels[idx].set_fontweight("bold")

    fig.colorbar(im2, ax=[ax1, ax2], shrink=0.8, label="Muestras")
    fig.suptitle("Cobertura de Muestras por Llave: v0.14.0 vs v0.14.1\n"
                 "Fix: LwM2M Object 4 v1.0 → v1.3 (habilita RSSI + LQI)",
                 fontsize=14, y=1.02)
    fig.tight_layout()

    path = os.path.join(output_dir, f"comparison_heatmap_v0141.{fmt}")
    fig.savefig(path)
    plt.close(fig)
    print(f"  [2/6] {path}")
    return path


# ═══════════════════════════════════════════════════════════════════
# Graph 3: RSSI/LQI Dedicated Analysis
# ═══════════════════════════════════════════════════════════════════

def plot_rssi_lqi_detail(v141, output_dir, fmt):
    """Detail view: RSSI and LQI stats across scenarios (v0.14.1 only)."""
    plt.rcParams.update(STYLE)
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

    scenarios = [s for s in SCENARIO_ORDER if s in v141.get("scenarios", {})]
    x = np.arange(len(scenarios))

    rssi_avg = []
    rssi_min = []
    rssi_max = []
    rssi_samples = []
    lqi_avg = []
    lqi_min = []
    lqi_max = []
    lqi_samples = []

    for s in scenarios:
        pk = v141.get("scenarios", {}).get(s, {}).get("per_key", {})
        rssi = pk.get("radioSignalStrength", {})
        lqi = pk.get("linkQuality", {})

        rssi_avg.append(_safe(rssi.get("iat_avg_s")))
        rssi_samples.append(_safe(rssi.get("samples")))
        rssi_min.append(_safe(rssi.get("value_min"), -100))
        rssi_max.append(_safe(rssi.get("value_max"), -50))

        lqi_avg.append(_safe(lqi.get("iat_avg_s")))
        lqi_samples.append(_safe(lqi.get("samples")))
        lqi_min.append(_safe(lqi.get("value_min"), 0))
        lqi_max.append(_safe(lqi.get("value_max"), 100))

    # RSSI subplot
    bars_rssi = ax1.bar(x, rssi_samples, color="#42A5F5", edgecolor="white", width=0.6)
    for bar, n in zip(bars_rssi, rssi_samples):
        ax1.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.3,
                 str(int(n)), ha="center", va="bottom", fontsize=10, fontweight="bold")
    ax1.set_xticks(x)
    ax1.set_xticklabels([SCENARIO_LABELS.get(s, s) for s in scenarios])
    ax1.set_ylabel("Muestras (300s)")
    ax1.set_title("Radio Signal Strength (RSSI)\nMuestras por Escenario")

    # LQI subplot
    bars_lqi = ax2.bar(x, lqi_samples, color="#AB47BC", edgecolor="white", width=0.6)
    for bar, n in zip(bars_lqi, lqi_samples):
        ax2.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.3,
                 str(int(n)), ha="center", va="bottom", fontsize=10, fontweight="bold")
    ax2.set_xticks(x)
    ax2.set_xticklabels([SCENARIO_LABELS.get(s, s) for s in scenarios])
    ax2.set_ylabel("Muestras (300s)")
    ax2.set_title("Link Quality Indicator (LQI)\nMuestras por Escenario")

    fig.suptitle("RSSI & LQI — Habilitados en v0.14.1 (Object 4 v1.3)", fontsize=14)
    fig.tight_layout()

    path = os.path.join(output_dir, f"rssi_lqi_detail_v0141.{fmt}")
    fig.savefig(path)
    plt.close(fig)
    print(f"  [3/6] {path}")
    return path


# ═══════════════════════════════════════════════════════════════════
# Graph 4: Throughput Comparison
# ═══════════════════════════════════════════════════════════════════

def plot_throughput_comparison(v140, v141, output_dir, fmt):
    """Bar chart: throughput (msgs/s) — now with 16 keys contributing."""
    plt.rcParams.update(STYLE)
    fig, ax = plt.subplots(figsize=(12, 6))

    scenarios = [s for s in SCENARIO_ORDER
                 if s in v140.get("scenarios", {}) or s in v141.get("scenarios", {})]
    x = np.arange(len(scenarios))
    w = 0.35

    vals_140 = []
    vals_141 = []
    for s in scenarios:
        s140 = v140.get("scenarios", {}).get(s, {}).get("aggregate", {})
        s141 = v141.get("scenarios", {}).get(s, {}).get("aggregate", {})
        vals_140.append(_safe(s140.get("overall_throughput_msgs_per_sec")))
        vals_141.append(_safe(s141.get("overall_throughput_msgs_per_sec")))

    bars1 = ax.bar(x - w/2, vals_140, w, label="v0.14.0 (14 keys)",
                   color=V140_COLOR, edgecolor="white")
    bars2 = ax.bar(x + w/2, vals_141, w, label="v0.14.1 (16 keys)",
                   color=V141_COLOR, edgecolor="white")

    for bar in bars1:
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2, h + 0.002, f"{h:.3f}",
                ha="center", va="bottom", fontsize=9)
    for bar in bars2:
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2, h + 0.002, f"{h:.3f}",
                ha="center", va="bottom", fontsize=9)

    for i, (v1, v2) in enumerate(zip(vals_140, vals_141)):
        if v1 > 0:
            pct = (v2 - v1) / v1 * 100
            color = IMPROVE_COLOR if pct >= 0 else REGRESS_COLOR
            sign = "+" if pct >= 0 else ""
            ax.annotate(f"{sign}{pct:.1f}%", xy=(x[i], max(v1, v2) + 0.015),
                        ha="center", fontsize=9, fontweight="bold", color=color)

    ax.set_xticks(x)
    ax.set_xticklabels([SCENARIO_LABELS.get(s, s) for s in scenarios])
    ax.set_ylabel("Throughput (msgs/s)")
    ax.set_title("Throughput: v0.14.0 vs v0.14.1\n(+2 keys: RSSI + LQI)")
    ax.legend()
    ax.set_ylim(0, max(max(vals_140, default=0), max(vals_141, default=0)) * 1.3)

    path = os.path.join(output_dir, f"comparison_throughput_v0141.{fmt}")
    fig.savefig(path)
    plt.close(fig)
    print(f"  [4/6] {path}")
    return path


# ═══════════════════════════════════════════════════════════════════
# Graph 5: Total Samples Comparison
# ═══════════════════════════════════════════════════════════════════

def plot_samples_comparison(v140, v141, output_dir, fmt):
    """Bar chart: total messages — shows increase from RSSI+LQI."""
    plt.rcParams.update(STYLE)
    fig, ax = plt.subplots(figsize=(12, 6))

    scenarios = [s for s in SCENARIO_ORDER
                 if s in v140.get("scenarios", {}) or s in v141.get("scenarios", {})]
    x = np.arange(len(scenarios))
    w = 0.35

    vals_140 = []
    vals_141 = []
    for s in scenarios:
        s140 = v140.get("scenarios", {}).get(s, {}).get("aggregate", {})
        s141 = v141.get("scenarios", {}).get(s, {}).get("aggregate", {})
        vals_140.append(_safe(s140.get("total_messages")))
        vals_141.append(_safe(s141.get("total_messages")))

    bars1 = ax.bar(x - w/2, vals_140, w, label="v0.14.0 (14 keys)",
                   color=V140_COLOR, edgecolor="white")
    bars2 = ax.bar(x + w/2, vals_141, w, label="v0.14.1 (16 keys)",
                   color=V141_COLOR, edgecolor="white")

    for bar in bars1:
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2, h + 0.5, f"{int(h)}",
                ha="center", va="bottom", fontsize=9)
    for bar in bars2:
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2, h + 0.5, f"{int(h)}",
                ha="center", va="bottom", fontsize=9)

    for i, (v1, v2) in enumerate(zip(vals_140, vals_141)):
        if v1 > 0:
            pct = (v2 - v1) / v1 * 100
            color = IMPROVE_COLOR if pct >= 0 else REGRESS_COLOR
            sign = "+" if pct >= 0 else ""
            ax.annotate(f"{sign}{pct:.1f}%", xy=(x[i], max(v1, v2) + 3),
                        ha="center", fontsize=9, fontweight="bold", color=color)

    ax.set_xticks(x)
    ax.set_xticklabels([SCENARIO_LABELS.get(s, s) for s in scenarios])
    ax.set_ylabel("Total Mensajes (300s)")
    ax.set_title("Muestras Totales: v0.14.0 vs v0.14.1\n(+2 keys: RSSI + LQI)")
    ax.legend()

    path = os.path.join(output_dir, f"comparison_samples_v0141.{fmt}")
    fig.savefig(path)
    plt.close(fig)
    print(f"  [5/6] {path}")
    return path


# ═══════════════════════════════════════════════════════════════════
# Graph 6: Improvement Summary
# ═══════════════════════════════════════════════════════════════════

def plot_improvement_summary(v140, v141, output_dir, fmt):
    """Horizontal bar chart: % improvement per metric per scenario."""
    plt.rcParams.update(STYLE)
    fig, ax = plt.subplots(figsize=(12, 8))

    metrics = []

    for s in SCENARIO_ORDER:
        s140 = v140.get("scenarios", {}).get(s, {}).get("aggregate", {})
        s141 = v141.get("scenarios", {}).get(s, {}).get("aggregate", {})
        if not s140 or not s141:
            continue

        label = s.upper() if s != "baseline" else "BASE"

        # Throughput (higher = better)
        t1 = _safe(s140.get("overall_throughput_msgs_per_sec"))
        t2 = _safe(s141.get("overall_throughput_msgs_per_sec"))
        if t1 > 0:
            metrics.append((f"{label}: Throughput", (t2 - t1) / t1 * 100))

        # Messages (higher = better)
        m1 = _safe(s140.get("total_messages"))
        m2 = _safe(s141.get("total_messages"))
        if m1 > 0:
            metrics.append((f"{label}: Muestras", (m2 - m1) / m1 * 100))

        # Keys reporting (higher = better)
        k1 = _safe(s140.get("total_keys_reporting"))
        k2 = _safe(s141.get("total_keys_reporting"))
        if k1 > 0:
            metrics.append((f"{label}: Llaves", (k2 - k1) / k1 * 100))

    if not metrics:
        print("  [6/6] No metrics to compare")
        return None

    labels, values = zip(*metrics)
    colors = [IMPROVE_COLOR if v >= 0 else REGRESS_COLOR for v in values]

    y_pos = np.arange(len(labels))
    ax.barh(y_pos, values, color=colors, edgecolor="white", height=0.6)

    for i, (lbl, val) in enumerate(zip(labels, values)):
        sign = "+" if val >= 0 else ""
        ax.text(val + (0.5 if val >= 0 else -0.5), i, f"{sign}{val:.1f}%",
                va="center", fontsize=9, fontweight="bold")

    ax.set_yticks(y_pos)
    ax.set_yticklabels(labels)
    ax.set_xlabel("Mejora (%)")
    ax.set_title("Resumen de Mejoras: v0.14.0 → v0.14.1\n"
                 "(Fix Object 4 ConnMon: RSSI + LQI habilitados)")
    ax.axvline(x=0, color="black", linewidth=0.8)
    ax.invert_yaxis()

    path = os.path.join(output_dir, f"comparison_improvement_v0141.{fmt}")
    fig.savefig(path)
    plt.close(fig)
    print(f"  [6/6] {path}")
    return path


# ═══════════════════════════════════════════════════════════════════
# Text Report
# ═══════════════════════════════════════════════════════════════════

def generate_text_report(v140, v141, output_dir):
    """Generate detailed text comparison report."""
    lines = []
    lines.append("=" * 80)
    lines.append("REPORTE COMPARATIVO: Firmware v0.14.0 vs v0.14.1")
    lines.append("Tesis_jsgiraldod_2026_rev_final")
    lines.append(f"Generado: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 80)
    lines.append("")
    lines.append("CAMBIO EN v0.14.1:")
    lines.append("  - CONFIG_LWM2M_CONNMON_OBJECT_VERSION_1_0=y  -->  _1_3=y")
    lines.append("")
    lines.append("CAUSA RAIZ:")
    lines.append("  El dispositivo registraba el Object 4 (Connectivity Monitor)")
    lines.append("  como version 1.0, pero el perfil de ThingsBoard Edge usa rutas")
    lines.append("  con version 1.3 (/4_1.3/0/2 y /4_1.3/0/3). Esta discrepancia")
    lines.append("  de version LwM2M provocaba que las solicitudes 'observe' del")
    lines.append("  servidor nunca coincidieran con el registro del dispositivo,")
    lines.append("  resultando en 0 muestras de RSSI y LQI en TODOS los benchmarks")
    lines.append("  anteriores (v0.13.0 y v0.14.0).")
    lines.append("")
    lines.append("RESULTADO:")
    lines.append("  - Llaves reportando: 14/16 --> 16/16")
    lines.append("  - radioSignalStrength (RSSI): 0 muestras --> reportando dBm")
    lines.append("  - linkQuality (LQI): 0 muestras --> reportando %")
    lines.append("")

    header = f"{'Metrica':<25} {'v0.14.0':>12} {'v0.14.1':>12} {'Cambio':>12}"

    for s in SCENARIO_ORDER:
        s140 = v140.get("scenarios", {}).get(s, {}).get("aggregate", {})
        s141 = v141.get("scenarios", {}).get(s, {}).get("aggregate", {})
        if not s140 and not s141:
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
            v1 = s140.get(key)
            v2 = s141.get(key)
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
        lines.append(f"  {'Llave':<22} {'v0.14.0':>10} {'v0.14.1':>10} {'Cambio':>10}")
        lines.append("  " + "-" * 54)

        pk140 = v140.get("scenarios", {}).get(s, {}).get("per_key", {})
        pk141 = v141.get("scenarios", {}).get(s, {}).get("per_key", {})

        for k in TELEMETRY_KEYS_DISPLAY:
            n1 = pk140.get(k, {}).get("samples", 0)
            n2 = pk141.get(k, {}).get("samples", 0)
            if n1 == 0 and n2 == 0:
                change = "-"
            elif n1 == 0:
                change = "FIXED!"
            else:
                pct = (n2 - n1) / n1 * 100
                change = f"{'+' if pct >= 0 else ''}{pct:.0f}%"
            marker = " <<<" if k in ("radioSignalStrength", "linkQuality") else ""
            lines.append(f"  {k:<22} {n1:>10} {n2:>10} {change:>10}{marker}")

        lines.append("")

    report = "\n".join(lines)
    path = os.path.join(output_dir, "comparison_report_v0141.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n  Report: {path}")
    print()
    print(report)
    return path


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Compare benchmark results: v0.14.0 vs v0.14.1 (RSSI/LQI fix)"
    )
    parser.add_argument("v140_dir", help="Directory with v0.14.0 benchmark results")
    parser.add_argument("v141_dir", help="Directory with v0.14.1 benchmark results")
    parser.add_argument("--output", "-o", default=None,
                        help="Output directory (default: results/comparison_v014_v0141)")
    parser.add_argument("--format", "-f", default="png", choices=["png", "pdf", "svg"],
                        help="Image format (default: png)")

    args = parser.parse_args()

    if args.output is None:
        args.output = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "results", "comparison_v014_v0141"
        )

    os.makedirs(args.output, exist_ok=True)

    print("=" * 60)
    print("  COMPARACION v0.14.0 vs v0.14.1 (RSSI/LQI Fix)")
    print("=" * 60)
    print(f"  v0.14.0: {args.v140_dir}")
    print(f"  v0.14.1: {args.v141_dir}")
    print(f"  Output:  {args.output}")
    print(f"  Format:  {args.format}")
    print("=" * 60)

    v140 = load_summary(args.v140_dir)
    v141 = load_summary(args.v141_dir)

    print(f"\n  v0.14.0 scenarios: {list(v140.get('scenarios', {}).keys())}")
    print(f"  v0.14.1 scenarios: {list(v141.get('scenarios', {}).keys())}")

    if not HAS_MPL:
        print("\n  WARNING: matplotlib not available, generating text report only")
        generate_text_report(v140, v141, args.output)
        return

    print("\n  Generating comparison graphs...")
    files = []
    files.append(plot_keys_reporting(v140, v141, args.output, args.format))
    files.append(plot_per_key_heatmap(v140, v141, args.output, args.format))
    files.append(plot_rssi_lqi_detail(v141, args.output, args.format))
    files.append(plot_throughput_comparison(v140, v141, args.output, args.format))
    files.append(plot_samples_comparison(v140, v141, args.output, args.format))
    files.append(plot_improvement_summary(v140, v141, args.output, args.format))

    generate_text_report(v140, v141, args.output)

    print(f"\n  Done! {len([f for f in files if f])} graphs + report generated.")
    print(f"  Output: {args.output}")


if __name__ == "__main__":
    main()
