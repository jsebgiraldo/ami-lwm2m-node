"""
Generate thesis figures for firmware v0.17.0 — Field-Mask Validation
Outputs PNGs to figuras/ directory
"""

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
import numpy as np
import os

OUT = os.path.join(os.path.dirname(__file__), 'figuras')
os.makedirs(OUT, exist_ok=True)

# Thesis-quality settings
plt.rcParams.update({
    'font.size': 11,
    'font.family': 'serif',
    'figure.dpi': 200,
    'savefig.dpi': 200,
    'axes.grid': True,
    'grid.alpha': 0.3,
})


def fig_field_mask_bitmap():
    """Visual representation of the 27-bit field_mask bitmask."""
    fig, ax = plt.subplots(figsize=(14, 5))

    groups = [
        ('Fase R', range(0, 6), '#2196F3'),
        ('Fase S', range(6, 12), '#4CAF50'),
        ('Fase T', range(12, 18), '#FF9800'),
        ('Totales', range(18, 22), '#9C27B0'),
        ('Energía', range(22, 25), '#F44336'),
        ('Otros', range(25, 27), '#607D8B'),
    ]

    labels = [
        'V_R', 'I_R', 'P_R', 'Q_R', 'S_R', 'PF_R',
        'V_S', 'I_S', 'P_S', 'Q_S', 'S_S', 'PF_S',
        'V_T', 'I_T', 'P_T', 'Q_T', 'S_T', 'PF_T',
        'P_tot', 'Q_tot', 'S_tot', 'PF_tot',
        'E_act', 'E_react', 'E_app',
        'freq', 'I_N',
    ]

    # Simulate a partial read: bits 0-5, 18-21, 22-24, 25 ON; 6-17, 26 OFF
    # (single-phase scenario: phases S/T skipped)
    mask_example = 0b0010_0111_1111_1100_0000_0011_1111
    # bits: 0-5 ON, 6-17 OFF, 18-24 ON, 25 ON, 26 OFF

    for i in range(27):
        bit_on = bool(mask_example & (1 << i))
        # Find group color
        color = '#CCCCCC'
        for gname, grange, gcol in groups:
            if i in grange:
                color = gcol if bit_on else '#E0E0E0'
                break

        x = i % 9
        y = 2 - (i // 9)

        rect = FancyBboxPatch((x * 1.5, y * 1.4), 1.3, 1.1,
                              boxstyle="round,pad=0.05",
                              facecolor=color,
                              edgecolor='black' if bit_on else '#999999',
                              linewidth=2 if bit_on else 0.5,
                              alpha=0.9 if bit_on else 0.4)
        ax.add_patch(rect)

        # Bit number
        ax.text(x * 1.5 + 0.65, y * 1.4 + 0.75, f'bit {i}',
                ha='center', va='center', fontsize=7, color='white' if bit_on else '#666666',
                fontweight='bold' if bit_on else 'normal')
        # Label
        ax.text(x * 1.5 + 0.65, y * 1.4 + 0.35, labels[i],
                ha='center', va='center', fontsize=8, color='white' if bit_on else '#999999',
                fontfamily='monospace')

    # Legend
    legend_patches = [mpatches.Patch(color=c, label=n) for n, _, c in groups]
    legend_patches.append(mpatches.Patch(facecolor='#E0E0E0', edgecolor='#999',
                                         label='No leído (bit OFF)'))
    ax.legend(handles=legend_patches, loc='upper right', fontsize=8, ncol=2)

    ax.set_xlim(-0.3, 14)
    ax.set_ylim(-0.5, 4.5)
    ax.set_aspect('equal')
    ax.axis('off')
    ax.set_title('field_mask: Ejemplo monofásico (Fases S/T omitidas)',
                 fontsize=13, fontweight='bold', pad=15)

    fig.savefig(os.path.join(OUT, 'fig_field_mask.png'))
    plt.close(fig)
    print('  -> fig_field_mask.png')


def fig_protection_layers():
    """6-layer defense architecture diagram."""
    fig, ax = plt.subplots(figsize=(12, 7))

    layers = [
        ('L1: meter_read_all()', 'memset(0) + field_mask por OBIS', '#E3F2FD', '#1565C0'),
        ('L2: MIN_READ_PERCENT', 'Descarta si cobertura < 50%', '#E8F5E9', '#2E7D32'),
        ('L3: readings_sanity_check()', 'V ∈ [50,500]V · f ∈ [40,70]Hz', '#FFF3E0', '#E65100'),
        ('L4: THRESH_CHECK(bit_idx)', 'Omite campos sin bit activo', '#F3E5F5', '#6A1B9A'),
        ('L5: last_good por campo', 'Solo actualiza campos leídos', '#FFEBEE', '#B71C1C'),
        ('L6: consecutive_failures', 'Log crítico tras 5 fallos', '#ECEFF1', '#37474F'),
    ]

    box_h = 0.85
    gap = 0.15
    total_h = len(layers) * (box_h + gap)

    for i, (title, desc, bg, border) in enumerate(layers):
        y = total_h - i * (box_h + gap)
        w = 10

        rect = FancyBboxPatch((1, y), w, box_h,
                              boxstyle="round,pad=0.1",
                              facecolor=bg, edgecolor=border, linewidth=2.5)
        ax.add_patch(rect)

        ax.text(1.4, y + box_h * 0.6, title, fontsize=12, fontweight='bold',
                color=border, va='center')
        ax.text(1.4, y + box_h * 0.25, desc, fontsize=10, color='#333333',
                va='center')

        # Arrow between layers (except last)
        if i < len(layers) - 1:
            ax.annotate('', xy=(6, y - gap + 0.02), xytext=(6, y - 0.02),
                        arrowprops=dict(arrowstyle='->', color='#666666', lw=1.5))

    # Side labels
    ax.text(0.3, total_h - 0.5 * (box_h + gap), '← Medidor\n   DLMS',
            ha='center', va='center', fontsize=9, color='#1565C0', fontweight='bold')
    ax.text(12.2, total_h - 4.5 * (box_h + gap), 'Servidor →\n LwM2M',
            ha='center', va='center', fontsize=9, color='#6A1B9A', fontweight='bold')

    # "DATOS REALES" label at bottom
    ax.text(6, -0.3, 'Solo datos reales del medidor llegan al servidor',
            ha='center', va='center', fontsize=11, fontweight='bold', color='#2E7D32',
            bbox=dict(boxstyle='round,pad=0.3', facecolor='#E8F5E9', edgecolor='#2E7D32'))

    ax.set_xlim(-0.5, 13)
    ax.set_ylim(-1, total_h + 1)
    ax.axis('off')
    ax.set_title('Arquitectura de Protección Multicapa — v0.17.0',
                 fontsize=14, fontweight='bold', pad=15)

    fig.savefig(os.path.join(OUT, 'fig_protection_layers.png'))
    plt.close(fig)
    print('  -> fig_protection_layers.png')


def fig_memory_comparison():
    """Bar chart: Flash and RAM usage v0.16.0 vs v0.17.0."""
    fig, axes = plt.subplots(1, 2, figsize=(11, 5))

    versions = ['v0.16.0', 'v0.17.0']
    colors = ['#42A5F5', '#66BB6A']

    # Flash
    flash_total = 4_194_176
    flash_used = [709_636, 644_200]
    flash_pct = [u / flash_total * 100 for u in flash_used]

    bars = axes[0].bar(versions, flash_pct, color=colors, edgecolor='black', linewidth=0.8, width=0.5)
    axes[0].set_ylabel('% Usado')
    axes[0].set_title('Flash (4,194,176 B)', fontweight='bold')
    axes[0].set_ylim(0, 25)
    for bar, pct, used in zip(bars, flash_pct, flash_used):
        axes[0].text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.5,
                     f'{pct:.2f}%\n({used:,} B)', ha='center', va='bottom', fontsize=9)

    # Delta annotation
    delta_flash = flash_used[1] - flash_used[0]
    axes[0].annotate(f'Δ = {delta_flash:,} B\n({delta_flash/flash_total*100:+.2f} pp)',
                     xy=(1, flash_pct[1]), xytext=(1.35, flash_pct[0]),
                     fontsize=9, color='#2E7D32', fontweight='bold',
                     arrowprops=dict(arrowstyle='->', color='#2E7D32', lw=1.5))

    # RAM
    ram_total = 488_976
    ram_used = [312_068, 317_248]
    ram_pct = [u / ram_total * 100 for u in ram_used]

    bars = axes[1].bar(versions, ram_pct, color=colors, edgecolor='black', linewidth=0.8, width=0.5)
    axes[1].set_ylabel('% Usado')
    axes[1].set_title('RAM (488,976 B)', fontweight='bold')
    axes[1].set_ylim(0, 80)
    for bar, pct, used in zip(bars, ram_pct, ram_used):
        axes[1].text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.5,
                     f'{pct:.2f}%\n({used:,} B)', ha='center', va='bottom', fontsize=9)

    delta_ram = ram_used[1] - ram_used[0]
    axes[1].annotate(f'Δ = +{delta_ram:,} B\n({delta_ram/ram_total*100:+.2f} pp)',
                     xy=(1, ram_pct[1]), xytext=(1.35, ram_pct[0] - 5),
                     fontsize=9, color='#C62828', fontweight='bold',
                     arrowprops=dict(arrowstyle='->', color='#C62828', lw=1.5))

    fig.suptitle('Comparación de Memoria — v0.16.0 vs v0.17.0', fontsize=13, fontweight='bold')
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    fig.savefig(os.path.join(OUT, 'fig_memory_comparison.png'))
    plt.close(fig)
    print('  -> fig_memory_comparison.png')


def fig_validation_flowchart():
    """Flowchart of the data validation pipeline."""
    fig, ax = plt.subplots(figsize=(10, 12))

    def draw_box(x, y, w, h, text, color='#E3F2FD', border='#1565C0', fontsize=10):
        rect = FancyBboxPatch((x - w/2, y - h/2), w, h,
                              boxstyle="round,pad=0.15",
                              facecolor=color, edgecolor=border, linewidth=2)
        ax.add_patch(rect)
        ax.text(x, y, text, ha='center', va='center', fontsize=fontsize,
                fontweight='bold', color=border, wrap=True)

    def draw_diamond(x, y, text, color='#FFF3E0', border='#E65100'):
        diamond = plt.Polygon([(x, y+0.6), (x+1.8, y), (x, y-0.6), (x-1.8, y)],
                              facecolor=color, edgecolor=border, linewidth=2)
        ax.add_patch(diamond)
        ax.text(x, y, text, ha='center', va='center', fontsize=9,
                fontweight='bold', color=border)

    def arrow(x1, y1, x2, y2, label='', color='#333'):
        ax.annotate('', xy=(x2, y2), xytext=(x1, y1),
                    arrowprops=dict(arrowstyle='->', color=color, lw=2))
        if label:
            mx, my = (x1+x2)/2, (y1+y2)/2
            ax.text(mx + 0.3, my, label, fontsize=8, color=color, fontweight='bold')

    cx = 5  # Center x

    # Start
    draw_box(cx, 11, 4, 0.7, 'Medidor DLMS (RS-485)', '#E8EAF6', '#283593')
    arrow(cx, 10.65, cx, 10.1)

    # meter_read_all
    draw_box(cx, 9.7, 4.5, 0.7, 'meter_read_all()\nmemset(readings, 0)', '#E3F2FD', '#1565C0')
    arrow(cx, 9.35, cx, 8.8)

    # Loop OBIS
    draw_box(cx, 8.4, 5, 0.7, 'Para cada OBIS[i]: leer valor\n'
             'OK → field_mask |= (1<<i)  ·  FAIL → bit OFF', '#E8F5E9', '#2E7D32')
    arrow(cx, 8.05, cx, 7.4)

    # Decision: coverage
    draw_diamond(cx, 7, '¿read_count ≥ 50%\nde read_target?')
    arrow(cx, 6.4, cx, 5.7, 'SÍ', '#2E7D32')
    arrow(cx + 1.8, 7, 8.5, 7, '', '#C62828')
    draw_box(8.5, 7, 2.2, 0.5, 'DESCARTA\nvalid=false', '#FFEBEE', '#C62828', fontsize=9)

    # Sanity check
    draw_box(cx, 5.3, 4.5, 0.7, 'readings_sanity_check()\nV∈[50,500] · f∈[40,70]', '#FFF3E0', '#E65100')
    arrow(cx, 4.95, cx, 4.3)

    # Decision: sanity
    draw_diamond(cx, 3.9, '¿Pasa sanity\ncheck?')
    arrow(cx, 3.3, cx, 2.6, 'SÍ', '#2E7D32')
    arrow(cx + 1.8, 3.9, 8.5, 3.9, '', '#C62828')
    draw_box(8.5, 3.9, 2.2, 0.5, 'DESCARTA\nlog warning', '#FFEBEE', '#C62828', fontsize=9)

    # THRESH_CHECK per field
    draw_box(cx, 2.2, 5, 0.7, 'THRESH_CHECK × 27 campos\nbit OFF → skip  ·  bit ON → evalúa Δ',
             '#F3E5F5', '#6A1B9A')
    arrow(cx, 1.85, cx, 1.3)

    # LwM2M
    draw_box(cx, 0.9, 4, 0.7, 'Servidor LwM2M\n(ThingsBoard Edge)', '#E8F5E9', '#2E7D32')

    ax.set_xlim(0, 11)
    ax.set_ylim(0, 12)
    ax.axis('off')
    ax.set_title('Flujo de Validación de Lecturas — v0.17.0',
                 fontsize=14, fontweight='bold', pad=15)

    fig.savefig(os.path.join(OUT, 'fig_validation_flow.png'))
    plt.close(fig)
    print('  -> fig_validation_flow.png')


def fig_test_summary():
    """Horizontal bar chart of test suite results."""
    fig, ax = plt.subplots(figsize=(8, 4))

    suites = ['HDLC\n(CRC, tramas, parsing)', 'COSEM\n(AARQ, AARE, GET)', 'DLMS Logic\n(field_mask, sanity)']
    counts = [29, 43, 46]
    colors = ['#42A5F5', '#66BB6A', '#AB47BC']

    bars = ax.barh(suites, counts, color=colors, edgecolor='black', linewidth=0.8, height=0.5)

    for bar, count in zip(bars, counts):
        ax.text(bar.get_width() + 0.5, bar.get_y() + bar.get_height()/2,
                f'{count}/{count} PASS', va='center', fontsize=11, fontweight='bold',
                color='#2E7D32')

    ax.set_xlabel('Número de tests')
    ax.set_xlim(0, 55)
    ax.set_title(f'Tests Unitarios v0.17.0 — 118/118 PASS', fontsize=13, fontweight='bold')

    # Total bar at bottom
    ax.axvline(x=118, color='#2E7D32', linestyle='--', alpha=0.5, label='Total: 118')

    fig.tight_layout()
    fig.savefig(os.path.join(OUT, 'fig_test_summary.png'))
    plt.close(fig)
    print('  -> fig_test_summary.png')


if __name__ == '__main__':
    print('Generating thesis figures for v0.17.0...')
    fig_field_mask_bitmap()
    fig_protection_layers()
    fig_memory_comparison()
    fig_validation_flowchart()
    fig_test_summary()
    print(f'\nDone! {len(os.listdir(OUT))} figures in {OUT}')
