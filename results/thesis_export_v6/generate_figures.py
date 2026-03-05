#!/usr/bin/env python3
"""
generate_figures.py — Thesis Export v6 (v0.18.0)
Generates 5 figures for the thesis documenting the v0.18.0 changes.

Usage:
    cd results/thesis_export_v6
    python generate_figures.py

Output: figuras/*.png
"""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import os

OUT = os.path.join(os.path.dirname(__file__), "figuras")
os.makedirs(OUT, exist_ok=True)

DPI = 150


# ─────────────────────────────────────────────────────────────────────
# 1. Push flow comparison: THRESH_CHECK vs PUSH_FIELD
# ─────────────────────────────────────────────────────────────────────
def fig_push_flow():
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 7))

    # v0.17.0 THRESH_CHECK flow
    steps_old = [
        "meter_read_all()\nmemset(0) + field_mask",
        "coverage ≥ 50%?",
        "sanity_check()\nV/f ranges",
        "THRESH_CHECK:\nfield_mask bit?",
        "Δ = |new - last_notified|",
        "Δ ≥ threshold\nOR force?",
        "lwm2m_set_f64()\nnotify_observer()",
        "Update\nlast_notified[i]",
    ]
    colors_old = ["#4ECDC4", "#FFD93D", "#FFD93D", "#FF6B6B",
                  "#FF6B6B", "#FF6B6B", "#4ECDC4", "#FF6B6B"]

    for i, (step, color) in enumerate(zip(steps_old, colors_old)):
        y = 0.9 - i * 0.11
        ax1.add_patch(mpatches.FancyBboxPatch(
            (0.1, y - 0.04), 0.8, 0.08, boxstyle="round,pad=0.01",
            facecolor=color, edgecolor="black", alpha=0.8))
        ax1.text(0.5, y, step, ha="center", va="center", fontsize=8,
                fontweight="bold")
        if i < len(steps_old) - 1:
            ax1.annotate("", xy=(0.5, y - 0.05), xytext=(0.5, y - 0.07),
                        arrowprops=dict(arrowstyle="->", color="gray"))

    ax1.set_xlim(0, 1)
    ax1.set_ylim(0, 1)
    ax1.set_title("v0.17.0 — THRESH_CHECK\n(8 pasos, 6 statics, 6 thresholds)",
                  fontsize=11, fontweight="bold", color="#E74C3C")
    ax1.axis("off")

    # v0.18.0 PUSH_FIELD flow
    steps_new = [
        "meter_read_all()\nmemset(0) + field_mask",
        "coverage ≥ 50%?",
        "sanity_check()\nV/f ranges",
        "PUSH_FIELD:\nfield_mask bit?",
        "lwm2m_set_f64()\nnotify_observer()",
        "LwM2M observe engine\npmin/pmax filter",
        "CoAP notification\n→ TB Edge",
    ]
    colors_new = ["#4ECDC4", "#FFD93D", "#FFD93D", "#2ECC71",
                  "#2ECC71", "#3498DB", "#3498DB"]

    for i, (step, color) in enumerate(zip(steps_new, colors_new)):
        y = 0.88 - i * 0.12
        ax2.add_patch(mpatches.FancyBboxPatch(
            (0.1, y - 0.045), 0.8, 0.09, boxstyle="round,pad=0.01",
            facecolor=color, edgecolor="black", alpha=0.8))
        ax2.text(0.5, y, step, ha="center", va="center", fontsize=8.5,
                fontweight="bold")
        if i < len(steps_new) - 1:
            ax2.annotate("", xy=(0.5, y - 0.05), xytext=(0.5, y - 0.07),
                        arrowprops=dict(arrowstyle="->", color="gray"))

    ax2.set_xlim(0, 1)
    ax2.set_ylim(0, 1)
    ax2.set_title("v0.18.0 — PUSH_FIELD\n(7 pasos, 0 statics, server controls rate)",
                  fontsize=11, fontweight="bold", color="#27AE60")
    ax2.axis("off")

    # Legend
    legend_elements = [
        mpatches.Patch(facecolor="#4ECDC4", label="Común (ambas versiones)"),
        mpatches.Patch(facecolor="#FFD93D", label="Validación (preservada)"),
        mpatches.Patch(facecolor="#FF6B6B", label="Threshold logic (eliminada)"),
        mpatches.Patch(facecolor="#2ECC71", label="Push simplificado (nuevo)"),
        mpatches.Patch(facecolor="#3498DB", label="Server rate control (nuevo)"),
    ]
    fig.legend(handles=legend_elements, loc="lower center", ncol=3,
              fontsize=9, frameon=True)

    plt.suptitle("Evolución del sistema de notificación LwM2M",
                fontsize=14, fontweight="bold", y=0.98)
    plt.tight_layout(rect=[0, 0.06, 1, 0.95])
    plt.savefig(os.path.join(OUT, "fig_push_flow_v018.png"), dpi=DPI,
                bbox_inches="tight")
    plt.close()
    print("  ✓ fig_push_flow_v018.png")


# ─────────────────────────────────────────────────────────────────────
# 2. Architecture: 5-layer protection
# ─────────────────────────────────────────────────────────────────────
def fig_architecture():
    fig, ax = plt.subplots(figsize=(12, 7))

    layers = [
        ("L5", "consecutive_meter_failures\n5 fallos → log crítico", "#E8D5B7", 0.85),
        ("L4", "PUSH_FIELD(bit_idx)\nOmite campos sin bit activo", "#B8E6B8", 0.68),
        ("L3", "readings_sanity_check()\nV ∈ [50,500]V  f ∈ [40,70]Hz", "#FFD5D5", 0.51),
        ("L2", "MIN_READ_PERCENT = 50%\nDescarta lectura si cobertura < 50%", "#D5E8FF", 0.34),
        ("L1", "meter_read_all()\nmemset(0) + field_mask por OBIS", "#E8E8E8", 0.17),
    ]

    for label, text, color, y in layers:
        width = 0.7
        height = 0.12
        x = 0.15
        ax.add_patch(mpatches.FancyBboxPatch(
            (x, y), width, height, boxstyle="round,pad=0.015",
            facecolor=color, edgecolor="black", linewidth=2, alpha=0.9))
        ax.text(x + 0.02, y + height / 2, label, ha="left", va="center",
               fontsize=14, fontweight="bold", color="#333")
        ax.text(x + width / 2 + 0.02, y + height / 2, text,
               ha="center", va="center", fontsize=10)

    # Arrow: data flows upward
    ax.annotate("Datos del\nmedidor", xy=(0.5, 0.14), xytext=(0.5, 0.03),
               arrowprops=dict(arrowstyle="->, head_width=0.4",
                              color="#2C3E50", lw=2),
               fontsize=11, ha="center", fontweight="bold", color="#2C3E50")

    ax.annotate("LwM2M\n→ TB Edge", xy=(0.5, 1.0), xytext=(0.5, 0.98),
               fontsize=11, ha="center", fontweight="bold", color="#27AE60")

    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1.05)
    ax.set_title("Arquitectura de protección de datos — v0.18.0 (5 capas)",
                fontsize=14, fontweight="bold")
    ax.axis("off")
    plt.tight_layout()
    plt.savefig(os.path.join(OUT, "fig_architecture_v018.png"), dpi=DPI,
                bbox_inches="tight")
    plt.close()
    print("  ✓ fig_architecture_v018.png")


# ─────────────────────────────────────────────────────────────────────
# 3. Rate control diagram
# ─────────────────────────────────────────────────────────────────────
def fig_rate_control():
    fig, ax = plt.subplots(figsize=(12, 6))

    # Three columns: Firmware | LwM2M Engine | Server
    cols = [
        (0.05, 0.3, "Firmware\n(ESP32-C6)", "#E8F5E9"),
        (0.37, 0.26, "LwM2M Observe\nEngine (Zephyr)", "#E3F2FD"),
        (0.67, 0.28, "Servidor\n(TB Edge)", "#FFF3E0"),
    ]

    for x, w, title, color in cols:
        ax.add_patch(mpatches.FancyBboxPatch(
            (x, 0.15), w, 0.7, boxstyle="round,pad=0.02",
            facecolor=color, edgecolor="black", linewidth=2, alpha=0.7))
        ax.text(x + w / 2, 0.9, title, ha="center", va="center",
               fontsize=12, fontweight="bold")

    # Firmware actions
    fw_items = [
        "DLMS poll cada 15s",
        "meter_read_all()",
        "sanity_check()",
        "PUSH_FIELD × 27",
        "set_f64 + notify",
    ]
    for i, item in enumerate(fw_items):
        y = 0.75 - i * 0.12
        ax.text(0.2, y, f"• {item}", ha="center", va="center", fontsize=9)

    # Engine actions
    eng_items = [
        "Recibe notify",
        "t < pmin → suprime",
        "t > pmax → fuerza",
        "pmin ≤ t → envía",
    ]
    for i, item in enumerate(eng_items):
        y = 0.72 - i * 0.13
        ax.text(0.5, y, f"• {item}", ha="center", va="center", fontsize=9)

    # Server actions
    srv_items = [
        "Configura pmin/pmax",
        "Recibe CoAP notify",
        "Almacena telemetría",
        "Dashboard + alertas",
    ]
    for i, item in enumerate(srv_items):
        y = 0.72 - i * 0.13
        ax.text(0.81, y, f"• {item}", ha="center", va="center", fontsize=9)

    # Arrows between columns
    ax.annotate("", xy=(0.37, 0.5), xytext=(0.35, 0.5),
               arrowprops=dict(arrowstyle="->, head_width=0.3",
                              color="#4CAF50", lw=3))
    ax.text(0.36, 0.53, "notify", ha="center", fontsize=8, color="#4CAF50",
           fontweight="bold")

    ax.annotate("", xy=(0.67, 0.5), xytext=(0.63, 0.5),
               arrowprops=dict(arrowstyle="->, head_width=0.3",
                              color="#2196F3", lw=3))
    ax.text(0.65, 0.53, "CoAP", ha="center", fontsize=8, color="#2196F3",
           fontweight="bold")

    # Feedback arrow: server → engine (pmin/pmax config)
    ax.annotate("", xy=(0.63, 0.25), xytext=(0.67, 0.25),
               arrowprops=dict(arrowstyle="->, head_width=0.3",
                              color="#FF9800", lw=2, linestyle="dashed"))
    ax.text(0.65, 0.21, "pmin/pmax", ha="center", fontsize=8,
           color="#FF9800", fontweight="bold")

    ax.set_xlim(0, 1)
    ax.set_ylim(0.05, 1.0)
    ax.set_title("Control de rate de notificaciones — enfoque LwM2M estándar (v0.18.0)",
                fontsize=13, fontweight="bold")
    ax.axis("off")
    plt.tight_layout()
    plt.savefig(os.path.join(OUT, "fig_rate_control.png"), dpi=DPI,
                bbox_inches="tight")
    plt.close()
    print("  ✓ fig_rate_control.png")


# ─────────────────────────────────────────────────────────────────────
# 4. Test summary
# ─────────────────────────────────────────────────────────────────────
def fig_test_summary():
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    # Bar chart: test counts per suite
    suites = ["HDLC", "COSEM", "DLMS\nLogic"]
    counts = [29, 43, 45]
    colors = ["#3498DB", "#E74C3C", "#2ECC71"]

    bars = ax1.bar(suites, counts, color=colors, edgecolor="black", width=0.6)
    for bar, count in zip(bars, counts):
        ax1.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.5,
                str(count), ha="center", va="bottom", fontweight="bold", fontsize=12)

    ax1.set_ylabel("Número de tests")
    ax1.set_title("Tests por suite — v0.18.0", fontweight="bold")
    ax1.set_ylim(0, 55)
    ax1.axhline(y=sum(counts), color="gray", linestyle="--", alpha=0.5)
    ax1.text(2.3, sum(counts) + 0.5, f"Total: {sum(counts)}", ha="right",
            fontsize=10, color="gray")

    # Evolution chart: tests across versions
    versions = ["v0.13", "v0.14", "v0.15", "v0.16", "v0.17", "v0.18"]
    test_counts = [72, 72, 101, 111, 118, 117]

    ax2.plot(versions, test_counts, "o-", color="#8E44AD", linewidth=2,
            markersize=8, markerfacecolor="white", markeredgewidth=2)
    for v, c in zip(versions, test_counts):
        ax2.text(v, c + 1.5, str(c), ha="center", fontsize=9, fontweight="bold")

    ax2.set_ylabel("Total tests")
    ax2.set_title("Evolución de tests por versión", fontweight="bold")
    ax2.set_ylim(60, 130)
    ax2.grid(axis="y", alpha=0.3)

    plt.suptitle("Unit Test Suite — AMI LwM2M Node v0.18.0 (117/117 PASS)",
                fontsize=13, fontweight="bold")
    plt.tight_layout(rect=[0, 0, 1, 0.93])
    plt.savefig(os.path.join(OUT, "fig_test_summary_v018.png"), dpi=DPI,
                bbox_inches="tight")
    plt.close()
    print("  ✓ fig_test_summary_v018.png")


# ─────────────────────────────────────────────────────────────────────
# 5. Traffic reduction comparison
# ─────────────────────────────────────────────────────────────────────
def fig_traffic_reduction():
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5.5))

    # Left: v0.17.0 traffic breakdown (aggressive scenario)
    labels_old = [
        "RSSI + LQI\n(nudge)",
        "Active/Reactive/\nApparent Power",
        "Voltage/Freq/\nEnergy/Other",
    ]
    sizes_old = [174, 60, 12]
    colors_old = ["#E74C3C", "#F39C12", "#3498DB"]
    explode_old = (0.05, 0, 0)

    wedges1, texts1, autotexts1 = ax1.pie(
        sizes_old, labels=labels_old, colors=colors_old, explode=explode_old,
        autopct="%1.1f%%", startangle=90, textprops={"fontsize": 9})
    ax1.set_title("v0.17.0 — Agresivo (1s)\n246 msgs / 300s",
                  fontweight="bold", fontsize=11, color="#E74C3C")

    # Right: v0.18.0 expected traffic
    labels_new = [
        "RSSI + LQI\n(solo cambios)",
        "Todos los campos\n(rate=pmin/pmax)",
    ]
    sizes_new = [5, 80]
    colors_new = ["#2ECC71", "#3498DB"]
    explode_new = (0.05, 0)

    wedges2, texts2, autotexts2 = ax2.pie(
        sizes_new, labels=labels_new, colors=colors_new, explode=explode_new,
        autopct="%1.1f%%", startangle=90, textprops={"fontsize": 10})
    ax2.set_title("v0.18.0 — Estimado (pmin=15s)\n~85 msgs / 300s",
                  fontweight="bold", fontsize=11, color="#27AE60")

    # Summary text
    fig.text(0.5, 0.02,
            "Reducción estimada: 246 → ~85 msgs (−65%)  |  "
            "RSSI/LQI: 174 → ~5 msgs (−97%)  |  "
            "Rate real controlado por servidor",
            ha="center", fontsize=10, style="italic",
            bbox=dict(boxstyle="round", facecolor="#F0F0F0", alpha=0.8))

    plt.suptitle("Impacto en tráfico: v0.17.0 vs v0.18.0",
                fontsize=14, fontweight="bold")
    plt.tight_layout(rect=[0, 0.07, 1, 0.93])
    plt.savefig(os.path.join(OUT, "fig_traffic_reduction.png"), dpi=DPI,
                bbox_inches="tight")
    plt.close()
    print("  ✓ fig_traffic_reduction.png")


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Generating thesis figures for v0.18.0...")
    fig_push_flow()
    fig_architecture()
    fig_rate_control()
    fig_test_summary()
    fig_traffic_reduction()
    print(f"\nAll 5 figures saved to {OUT}/")
