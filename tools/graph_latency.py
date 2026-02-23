#!/usr/bin/env python3
"""
LwM2M Read Latency — Thesis Graph Generator
=============================================
Reads CSV data from the test suite and generates publication-quality
graphs for thesis analysis of CoAP-over-Thread read latency.

Graphs produced:
  1. Bar chart: Average latency per LwM2M Object (with error bars)
  2. Box plot: Latency distribution per Object
  3. Line chart: Sequential request number vs latency (congestion visibility)
  4. CDF: Cumulative Distribution Function of all latencies
  5. Heatmap: Resource × Round latency matrix
  6. Bar chart: Average latency per individual Resource
  7. Summary statistics table (saved as image)

Usage:
    python tools/graph_latency.py results/latency_XXXX.csv
    python tools/graph_latency.py results/latency_XXXX.csv --lang es
"""

import sys
import os
import argparse
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')  # Non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from matplotlib.colors import LinearSegmentedColormap

# ── Configuration ──────────────────────────────────────────────────────────────

THESIS_STYLE = {
    'figure.figsize': (10, 6),
    'font.size': 11,
    'font.family': 'serif',
    'axes.titlesize': 13,
    'axes.labelsize': 12,
    'xtick.labelsize': 10,
    'ytick.labelsize': 10,
    'legend.fontsize': 10,
    'figure.dpi': 150,
    'savefig.dpi': 300,
    'savefig.bbox': 'tight',
    'axes.grid': True,
    'grid.alpha': 0.3,
    'axes.spines.top': False,
    'axes.spines.right': False,
}

# Color palette (colorblind-friendly, thesis-appropriate)
OBJECT_COLORS = {
    'LwM2M Server':        '#1f77b4',
    'Device':              '#2ca02c',
    'Conn Monitor':        '#d62728',
    'Power Meter':         '#9467bd',
    'Thread Network':      '#ff7f0e',
    'Thread Commission':   '#8c564b',
    'Thread Neighbor':     '#e377c2',
    'Thread CLI':          '#7f7f7f',
    'Thread Diag':         '#17becf',
}

# Bilingual labels
LABELS = {
    'en': {
        'title_bar_obj':     'Average Read Latency per LwM2M Object',
        'title_box_obj':     'Read Latency Distribution per LwM2M Object',
        'title_seq':         'Read Latency vs Request Sequence Number',
        'title_cdf':         'Cumulative Distribution of Read Latency (CDF)',
        'title_heatmap':     'Read Latency Heatmap (Resource × Round)',
        'title_bar_res':     'Average Read Latency per Resource',
        'title_stats':       'Summary Statistics',
        'xlabel_latency':    'Latency (ms)',
        'ylabel_latency':    'Latency (ms)',
        'xlabel_object':     'LwM2M Object',
        'xlabel_resource':   'Resource',
        'xlabel_seq':        'Request Sequence Number',
        'ylabel_cdf':        'Cumulative Probability',
        'ylabel_round':      'Test Round',
        'legend_object':     'Object',
        'median':            'Median',
        'mean':              'Mean',
        'all_requests':      'All Requests',
        'failed':            'Failed',
    },
    'es': {
        'title_bar_obj':     'Latencia Promedio de Lectura por Objeto LwM2M',
        'title_box_obj':     'Distribución de Latencia por Objeto LwM2M',
        'title_seq':         'Latencia vs Número de Secuencia de Solicitud',
        'title_cdf':         'Distribución Acumulada de Latencia (CDF)',
        'title_heatmap':     'Mapa de Calor de Latencia (Recurso × Ronda)',
        'title_bar_res':     'Latencia Promedio por Recurso',
        'title_stats':       'Estadísticas Resumen',
        'xlabel_latency':    'Latencia (ms)',
        'ylabel_latency':    'Latencia (ms)',
        'xlabel_object':     'Objeto LwM2M',
        'xlabel_resource':   'Recurso',
        'xlabel_seq':        'Número de Secuencia de Solicitud',
        'ylabel_cdf':        'Probabilidad Acumulada',
        'ylabel_round':      'Ronda de Prueba',
        'legend_object':     'Objeto',
        'median':            'Mediana',
        'mean':              'Promedio',
        'all_requests':      'Todas las Solicitudes',
        'failed':            'Fallidas',
    }
}


def load_data(csv_path):
    """Load and validate CSV data."""
    df = pd.read_csv(csv_path)
    # Ensure required columns
    required = ['Round', 'SeqNum', 'Object', 'ObjectName', 'Resource',
                'ResourceLabel', 'DelayMs', 'LatencyMs', 'Status']
    missing = [c for c in required if c not in df.columns]
    if missing:
        print(f"ERROR: Missing columns: {missing}")
        sys.exit(1)

    df['Failed'] = df['Status'].str.contains('FAIL', na=False)
    df['ResourceKey'] = df['ObjectName'] + '/' + df['ResourceLabel']
    df['ObjectLabel'] = df['Object'].astype(str) + ' ' + df['ObjectName']
    return df


def get_color(obj_name):
    """Get color for an object, with fallback."""
    return OBJECT_COLORS.get(obj_name, '#333333')


# ── Graph 1: Bar chart — Average latency per Object ──────────────────────────

def plot_bar_per_object(df, L, out_dir):
    ok = df[~df['Failed']]
    stats = ok.groupby('ObjectName')['LatencyMs'].agg(['mean', 'std', 'count'])
    stats = stats.sort_values('mean', ascending=True)

    fig, ax = plt.subplots(figsize=(10, 6))
    colors = [get_color(n) for n in stats.index]
    bars = ax.barh(range(len(stats)), stats['mean'], xerr=stats['std'],
                   color=colors, edgecolor='white', linewidth=0.5,
                   capsize=3, error_kw={'linewidth': 0.8})

    ax.set_yticks(range(len(stats)))
    ax.set_yticklabels(stats.index)
    ax.set_xlabel(L['xlabel_latency'])
    ax.set_title(L['title_bar_obj'])

    # Add value labels
    for i, (mean, std, cnt) in enumerate(zip(stats['mean'], stats['std'], stats['count'])):
        ax.text(mean + std + 20, i, f'{mean:.0f}ms (n={cnt:.0f})',
                va='center', fontsize=9, color='#333')

    plt.tight_layout()
    path = os.path.join(out_dir, '01_bar_latency_per_object.png')
    fig.savefig(path)
    plt.close(fig)
    print(f"  [1/7] {path}")


# ── Graph 2: Box plot — Distribution per Object ──────────────────────────────

def plot_box_per_object(df, L, out_dir):
    ok = df[~df['Failed']]
    order = ok.groupby('ObjectName')['LatencyMs'].median().sort_values().index.tolist()

    fig, ax = plt.subplots(figsize=(10, 6))
    data_groups = [ok[ok['ObjectName'] == name]['LatencyMs'].values for name in order]
    colors = [get_color(n) for n in order]

    bp = ax.boxplot(data_groups, vert=False, patch_artist=True,
                    labels=order, widths=0.6,
                    boxprops=dict(linewidth=0.8),
                    medianprops=dict(color='black', linewidth=1.5),
                    whiskerprops=dict(linewidth=0.8),
                    capprops=dict(linewidth=0.8),
                    flierprops=dict(marker='o', markersize=3, alpha=0.5))

    for patch, color in zip(bp['boxes'], colors):
        patch.set_facecolor(color)
        patch.set_alpha(0.7)

    ax.set_xlabel(L['xlabel_latency'])
    ax.set_title(L['title_box_obj'])
    plt.tight_layout()
    path = os.path.join(out_dir, '02_box_latency_per_object.png')
    fig.savefig(path)
    plt.close(fig)
    print(f"  [2/7] {path}")


# ── Graph 3: Line — Sequence vs Latency (congestion view) ────────────────────

def plot_sequence(df, L, out_dir):
    fig, ax = plt.subplots(figsize=(12, 5))

    ok = df[~df['Failed']]
    fail = df[df['Failed']]

    # Plot by object name with color coding
    for obj_name in ok['ObjectName'].unique():
        subset = ok[ok['ObjectName'] == obj_name]
        ax.scatter(subset['SeqNum'], subset['LatencyMs'],
                   c=get_color(obj_name), label=obj_name,
                   s=20, alpha=0.7, edgecolors='none')

    if len(fail) > 0:
        ax.scatter(fail['SeqNum'], fail['LatencyMs'],
                   c='red', marker='x', s=50, label=L['failed'], zorder=5)

    # Add round separators
    resources_per_round = df.groupby('Round')['SeqNum'].max()
    for seq in resources_per_round.values[:-1]:
        ax.axvline(x=seq + 0.5, color='gray', linestyle='--', linewidth=0.5, alpha=0.4)

    ax.set_xlabel(L['xlabel_seq'])
    ax.set_ylabel(L['ylabel_latency'])
    ax.set_title(L['title_seq'])
    ax.legend(loc='upper right', ncol=3, fontsize=8, framealpha=0.8)

    plt.tight_layout()
    path = os.path.join(out_dir, '03_sequence_vs_latency.png')
    fig.savefig(path)
    plt.close(fig)
    print(f"  [3/7] {path}")


# ── Graph 4: CDF ─────────────────────────────────────────────────────────────

def plot_cdf(df, L, out_dir):
    ok = df[~df['Failed']]
    fig, ax = plt.subplots(figsize=(8, 6))

    # Global CDF
    sorted_all = np.sort(ok['LatencyMs'].values)
    cdf_all = np.arange(1, len(sorted_all) + 1) / len(sorted_all)
    ax.plot(sorted_all, cdf_all, 'k-', linewidth=2, label=L['all_requests'])

    # Per-object CDF
    for obj_name in sorted(ok['ObjectName'].unique()):
        vals = np.sort(ok[ok['ObjectName'] == obj_name]['LatencyMs'].values)
        cdf = np.arange(1, len(vals) + 1) / len(vals)
        ax.plot(vals, cdf, color=get_color(obj_name), linewidth=1.2,
                alpha=0.7, label=obj_name)

    # Reference lines
    median_val = np.median(sorted_all)
    p90_val = np.percentile(sorted_all, 90)
    ax.axhline(y=0.5, color='gray', linestyle=':', linewidth=0.5)
    ax.axhline(y=0.9, color='gray', linestyle=':', linewidth=0.5)
    ax.axvline(x=median_val, color='blue', linestyle='--', linewidth=0.5, alpha=0.5)
    ax.axvline(x=p90_val, color='orange', linestyle='--', linewidth=0.5, alpha=0.5)

    ax.text(median_val + 10, 0.48, f'{L["median"]}: {median_val:.0f}ms', fontsize=8, color='blue')
    ax.text(p90_val + 10, 0.88, f'P90: {p90_val:.0f}ms', fontsize=8, color='orange')

    ax.set_xlabel(L['xlabel_latency'])
    ax.set_ylabel(L['ylabel_cdf'])
    ax.set_title(L['title_cdf'])
    ax.legend(loc='lower right', fontsize=8, framealpha=0.8)
    ax.set_ylim(0, 1.02)

    plt.tight_layout()
    path = os.path.join(out_dir, '04_cdf_latency.png')
    fig.savefig(path)
    plt.close(fig)
    print(f"  [4/7] {path}")


# ── Graph 5: Heatmap — Resource × Round ──────────────────────────────────────

def plot_heatmap(df, L, out_dir):
    ok = df[~df['Failed']].copy()

    pivot = ok.pivot_table(values='LatencyMs', index='ResourceKey',
                           columns='Round', aggfunc='mean')
    pivot = pivot.reindex(ok.groupby('ResourceKey')['LatencyMs'].mean()
                          .sort_values(ascending=False).index)

    fig, ax = plt.subplots(figsize=(max(8, len(pivot.columns) * 0.8 + 3),
                                    max(6, len(pivot) * 0.35 + 2)))

    cmap = LinearSegmentedColormap.from_list('latency',
           ['#2ecc71', '#f1c40f', '#e74c3c'], N=256)

    im = ax.imshow(pivot.values, aspect='auto', cmap=cmap, interpolation='nearest')
    cbar = fig.colorbar(im, ax=ax, label=L['xlabel_latency'], shrink=0.8)

    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels([f'R{c}' for c in pivot.columns], fontsize=8)
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels(pivot.index, fontsize=8)
    ax.set_xlabel(L['ylabel_round'])
    ax.set_title(L['title_heatmap'])

    # Annotate cells
    for i in range(len(pivot.index)):
        for j in range(len(pivot.columns)):
            val = pivot.values[i, j]
            if not np.isnan(val):
                color = 'white' if val > pivot.values[~np.isnan(pivot.values)].mean() else 'black'
                ax.text(j, i, f'{val:.0f}', ha='center', va='center',
                        fontsize=7, color=color)

    plt.tight_layout()
    path = os.path.join(out_dir, '05_heatmap_resource_round.png')
    fig.savefig(path)
    plt.close(fig)
    print(f"  [5/7] {path}")


# ── Graph 6: Bar — Per Resource ───────────────────────────────────────────────

def plot_bar_per_resource(df, L, out_dir):
    ok = df[~df['Failed']]
    stats = ok.groupby(['ObjectName', 'ResourceKey'])['LatencyMs'].agg(['mean', 'std']).reset_index()
    stats = stats.sort_values('mean', ascending=True)

    fig, ax = plt.subplots(figsize=(10, max(6, len(stats) * 0.35)))
    colors = [get_color(row['ObjectName']) for _, row in stats.iterrows()]

    bars = ax.barh(range(len(stats)), stats['mean'], xerr=stats['std'],
                   color=colors, edgecolor='white', linewidth=0.5,
                   capsize=2, error_kw={'linewidth': 0.6})

    ax.set_yticks(range(len(stats)))
    ax.set_yticklabels(stats['ResourceKey'], fontsize=9)
    ax.set_xlabel(L['xlabel_latency'])
    ax.set_title(L['title_bar_res'])

    for i, (mean, std) in enumerate(zip(stats['mean'], stats['std'])):
        ax.text(mean + std + 10, i, f'{mean:.0f}ms', va='center', fontsize=8, color='#555')

    plt.tight_layout()
    path = os.path.join(out_dir, '06_bar_latency_per_resource.png')
    fig.savefig(path)
    plt.close(fig)
    print(f"  [6/7] {path}")


# ── Graph 7: Summary statistics table ─────────────────────────────────────────

def plot_stats_table(df, L, out_dir):
    ok = df[~df['Failed']]

    # Per-object stats
    stats = ok.groupby('ObjectName')['LatencyMs'].agg([
        ('Count', 'count'),
        ('Mean', 'mean'),
        ('Std', 'std'),
        ('Min', 'min'),
        ('Median', 'median'),
        ('P90', lambda x: np.percentile(x, 90)),
        ('P99', lambda x: np.percentile(x, 99)),
        ('Max', 'max'),
    ]).round(1)
    stats = stats.sort_values('Mean')

    # Add totals row
    all_vals = ok['LatencyMs']
    totals = pd.DataFrame({
        'Count': [len(all_vals)],
        'Mean': [all_vals.mean()],
        'Std': [all_vals.std()],
        'Min': [all_vals.min()],
        'Median': [all_vals.median()],
        'P90': [np.percentile(all_vals, 90)],
        'P99': [np.percentile(all_vals, 99)],
        'Max': [all_vals.max()],
    }, index=['ALL']).round(1)
    stats = pd.concat([stats, totals])

    # Failure stats
    fail_count = df['Failed'].sum()
    fail_pct = (fail_count / len(df) * 100)

    fig, ax = plt.subplots(figsize=(12, max(3, len(stats) * 0.4 + 2)))
    ax.axis('off')

    # Table
    cell_text = []
    for idx, row in stats.iterrows():
        cell_text.append([
            idx,
            f"{row['Count']:.0f}",
            f"{row['Mean']:.1f}",
            f"{row['Std']:.1f}",
            f"{row['Min']:.0f}",
            f"{row['Median']:.0f}",
            f"{row['P90']:.0f}",
            f"{row['P99']:.0f}",
            f"{row['Max']:.0f}",
        ])

    col_labels = ['Object', 'N', 'Mean(ms)', 'Std', 'Min', 'Median', 'P90', 'P99', 'Max']
    table = ax.table(cellText=cell_text, colLabels=col_labels,
                     cellLoc='center', loc='center')
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1, 1.4)

    # Style header
    for j in range(len(col_labels)):
        table[0, j].set_facecolor('#2c3e50')
        table[0, j].set_text_props(color='white', fontweight='bold')

    # Style ALL row
    last_row = len(cell_text)
    for j in range(len(col_labels)):
        table[last_row, j].set_facecolor('#ecf0f1')
        table[last_row, j].set_text_props(fontweight='bold')

    # Color-code mean column
    means = [float(row[2]) for row in cell_text[:-1]]  # exclude ALL
    if means:
        max_mean = max(means)
        for i, mean_val in enumerate(means):
            ratio = mean_val / max_mean if max_mean > 0 else 0
            if ratio > 0.7:
                table[i + 1, 2].set_facecolor('#fadbd8')
            elif ratio < 0.3:
                table[i + 1, 2].set_facecolor('#d5f5e3')

    title = f"{L['title_stats']}"
    if fail_count > 0:
        title += f"  |  Failed: {fail_count}/{len(df)} ({fail_pct:.1f}%)"
    ax.set_title(title, fontsize=13, fontweight='bold', pad=20)

    plt.tight_layout()
    path = os.path.join(out_dir, '07_stats_table.png')
    fig.savefig(path)
    plt.close(fig)
    print(f"  [7/7] {path}")

    # Also save as CSV
    csv_path = os.path.join(out_dir, 'summary_statistics.csv')
    stats.to_csv(csv_path)
    print(f"        {csv_path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='LwM2M Latency Graph Generator')
    parser.add_argument('csv_file', help='Path to CSV from test_suite_latency.ps1')
    parser.add_argument('--lang', choices=['en', 'es'], default='es',
                        help='Language for labels (default: es)')
    parser.add_argument('--outdir', default=None,
                        help='Output directory for graphs (default: same as CSV)')
    args = parser.parse_args()

    if not os.path.exists(args.csv_file):
        print(f"ERROR: File not found: {args.csv_file}")
        sys.exit(1)

    L = LABELS[args.lang]
    plt.rcParams.update(THESIS_STYLE)

    # Output dir
    out_dir = args.outdir or os.path.join(os.path.dirname(args.csv_file), 'graphs')
    os.makedirs(out_dir, exist_ok=True)

    print(f"\nLoading: {args.csv_file}")
    df = load_data(args.csv_file)
    print(f"  Rows: {len(df)}, Rounds: {df['Round'].nunique()}, "
          f"Resources: {df['ResourceKey'].nunique()}, "
          f"Failed: {df['Failed'].sum()}")
    print(f"\nGenerating graphs ({args.lang}) → {out_dir}/")

    plot_bar_per_object(df, L, out_dir)
    plot_box_per_object(df, L, out_dir)
    plot_sequence(df, L, out_dir)
    plot_cdf(df, L, out_dir)
    plot_heatmap(df, L, out_dir)
    plot_bar_per_resource(df, L, out_dir)
    plot_stats_table(df, L, out_dir)

    print(f"\nDone! {7} graphs saved to {out_dir}/")
    print("All images are 300 DPI, suitable for thesis publication.\n")


if __name__ == '__main__':
    main()
