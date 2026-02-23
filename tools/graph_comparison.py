#!/usr/bin/env python3
"""
LwM2M Before/After Optimization Comparison -- Thesis Graphs
=============================================================
Generates side-by-side comparison graphs between the baseline test
(default firmware, 3s delay) and the optimized test (tuned firmware, 5s delay).

Usage:
    python tools/graph_comparison.py results/latency_BEFORE.csv results/latency_AFTER.csv --lang es
"""

import sys
import os
import argparse
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

# -- Style --
STYLE = {
    'figure.figsize': (12, 6),
    'font.size': 11,
    'font.family': 'serif',
    'axes.titlesize': 14,
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

LABELS = {
    'es': {
        'before': 'Antes (FW default, 3s)',
        'after': 'Despues (FW optimizado, 5s)',
        'success_rate': 'Tasa de Exito (%)',
        'round': 'Ronda',
        'object': 'Objeto LwM2M',
        'latency_ms': 'Latencia (ms)',
        'requests': 'Solicitudes',
        'success': 'Exitosas',
        'failed': 'Fallidas',
        'title_success_comparison': 'Comparacion: Tasa de Exito por Ronda',
        'title_fail_by_object': 'Comparacion: Fallos por Objeto LwM2M',
        'title_latency_comparison': 'Comparacion: Latencia Promedio por Objeto',
        'title_overall': 'Resumen Comparativo de Optimizacion',
        'title_cdf': 'CDF Comparativa de Latencia',
        'title_resource_success': 'Tasa de Exito por Recurso',
        'probability': 'Probabilidad Acumulada',
        'improvement': 'Mejora',
        'resource': 'Recurso',
    },
    'en': {
        'before': 'Before (default FW, 3s)',
        'after': 'After (optimized FW, 5s)',
        'success_rate': 'Success Rate (%)',
        'round': 'Round',
        'object': 'LwM2M Object',
        'latency_ms': 'Latency (ms)',
        'requests': 'Requests',
        'success': 'Successful',
        'failed': 'Failed',
        'title_success_comparison': 'Comparison: Success Rate per Round',
        'title_fail_by_object': 'Comparison: Failures per LwM2M Object',
        'title_latency_comparison': 'Comparison: Average Latency per Object',
        'title_overall': 'Optimization Summary Comparison',
        'title_cdf': 'Comparative Latency CDF',
        'title_resource_success': 'Success Rate per Resource',
        'probability': 'Cumulative Probability',
        'improvement': 'Improvement',
        'resource': 'Resource',
    }
}

COLOR_BEFORE = '#e74c3c'  # Red
COLOR_AFTER  = '#2ecc71'  # Green


def load_csv(path):
    df = pd.read_csv(path)
    df['is_success'] = df['Status'].str.contains('CONTENT', case=False, na=False)
    df['LatencyMs'] = pd.to_numeric(df['LatencyMs'], errors='coerce')
    return df


def fig1_success_per_round(df_before, df_after, L, outdir):
    """Bar chart: success rate per round, before vs after."""
    fig, ax = plt.subplots(figsize=(12, 5))

    rounds_b = df_before.groupby('Round')['is_success'].mean() * 100
    rounds_a = df_after.groupby('Round')['is_success'].mean() * 100

    x = np.arange(1, max(len(rounds_b), len(rounds_a)) + 1)
    w = 0.35

    ax.bar(x - w/2, rounds_b.values, w, label=L['before'], color=COLOR_BEFORE, alpha=0.85)
    ax.bar(x + w/2, rounds_a.values, w, label=L['after'], color=COLOR_AFTER, alpha=0.85)

    ax.set_xlabel(L['round'])
    ax.set_ylabel(L['success_rate'])
    ax.set_title(L['title_success_comparison'])
    ax.set_xticks(x)
    ax.set_ylim(0, 105)
    ax.axhline(y=90, color='orange', linestyle='--', alpha=0.5, label='90% objetivo')
    ax.legend()

    # Add value labels
    for i, v in enumerate(rounds_b.values):
        ax.text(x[i] - w/2, v + 1, f'{v:.0f}%', ha='center', va='bottom', fontsize=8, color=COLOR_BEFORE)
    for i, v in enumerate(rounds_a.values):
        ax.text(x[i] + w/2, v + 1, f'{v:.0f}%', ha='center', va='bottom', fontsize=8, color=COLOR_AFTER)

    path = os.path.join(outdir, 'cmp_01_success_per_round.png')
    fig.savefig(path)
    plt.close(fig)
    print(f'  [1/6] {path}')


def fig2_failures_by_object(df_before, df_after, L, outdir):
    """Bar chart: number of failures per Object, before vs after."""
    fig, ax = plt.subplots(figsize=(12, 6))

    fail_b = df_before[~df_before['is_success']].groupby('ObjectName').size()
    fail_a = df_after[~df_after['is_success']].groupby('ObjectName').size()

    all_objects = sorted(set(fail_b.index) | set(fail_a.index) |
                         set(df_before['ObjectName'].unique()) | set(df_after['ObjectName'].unique()))
    fail_b = fail_b.reindex(all_objects, fill_value=0)
    fail_a = fail_a.reindex(all_objects, fill_value=0)

    x = np.arange(len(all_objects))
    w = 0.35

    ax.barh(x - w/2, fail_b.values, w, label=L['before'], color=COLOR_BEFORE, alpha=0.85)
    ax.barh(x + w/2, fail_a.values, w, label=L['after'], color=COLOR_AFTER, alpha=0.85)

    ax.set_yticks(x)
    ax.set_yticklabels(all_objects)
    ax.set_xlabel(L['failed'])
    ax.set_ylabel(L['object'])
    ax.set_title(L['title_fail_by_object'])
    ax.legend()
    ax.invert_yaxis()

    path = os.path.join(outdir, 'cmp_02_failures_by_object.png')
    fig.savefig(path)
    plt.close(fig)
    print(f'  [2/6] {path}')


def fig3_latency_by_object(df_before, df_after, L, outdir):
    """Bar chart: average latency per Object (success only), before vs after."""
    fig, ax = plt.subplots(figsize=(12, 6))

    succ_b = df_before[df_before['is_success']].groupby('ObjectName')['LatencyMs'].mean()
    succ_a = df_after[df_after['is_success']].groupby('ObjectName')['LatencyMs'].mean()

    all_objects = sorted(set(succ_b.index) | set(succ_a.index))
    succ_b = succ_b.reindex(all_objects, fill_value=0)
    succ_a = succ_a.reindex(all_objects, fill_value=0)

    x = np.arange(len(all_objects))
    w = 0.35

    ax.barh(x - w/2, succ_b.values, w, label=L['before'], color=COLOR_BEFORE, alpha=0.85)
    ax.barh(x + w/2, succ_a.values, w, label=L['after'], color=COLOR_AFTER, alpha=0.85)

    ax.set_yticks(x)
    ax.set_yticklabels(all_objects)
    ax.set_xlabel(L['latency_ms'])
    ax.set_ylabel(L['object'])
    ax.set_title(L['title_latency_comparison'])
    ax.legend()
    ax.invert_yaxis()

    path = os.path.join(outdir, 'cmp_03_latency_by_object.png')
    fig.savefig(path)
    plt.close(fig)
    print(f'  [3/6] {path}')


def fig4_overall_summary(df_before, df_after, L, outdir):
    """Summary comparison table as image."""
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.axis('off')

    total_b = len(df_before)
    succ_b = df_before['is_success'].sum()
    fail_b = total_b - succ_b
    rate_b = succ_b / total_b * 100
    lat_b = df_before[df_before['is_success']]['LatencyMs'].mean()
    med_b = df_before[df_before['is_success']]['LatencyMs'].median()

    total_a = len(df_after)
    succ_a = df_after['is_success'].sum()
    fail_a = total_a - succ_a
    rate_a = succ_a / total_a * 100
    lat_a = df_after[df_after['is_success']]['LatencyMs'].mean()
    med_a = df_after[df_after['is_success']]['LatencyMs'].median()

    headers = ['Metrica', L['before'], L['after'], L['improvement']]
    rows = [
        ['Total solicitudes', str(total_b), str(total_a), '-'],
        ['Exitosas', f'{succ_b} ({rate_b:.1f}%)', f'{succ_a} ({rate_a:.1f}%)',
         f'+{rate_a - rate_b:.1f}pp'],
        ['Fallidas', f'{fail_b} ({100-rate_b:.1f}%)', f'{fail_a} ({100-rate_a:.1f}%)',
         f'-{(100-rate_b) - (100-rate_a):.1f}pp'],
        ['Latencia promedio (ms)', f'{lat_b:.0f}', f'{lat_a:.0f}',
         f'{((lat_a - lat_b)/lat_b*100):+.1f}%'],
        ['Latencia mediana (ms)', f'{med_b:.0f}', f'{med_a:.0f}',
         f'{((med_a - med_b)/med_b*100):+.1f}%'],
    ]

    table = ax.table(cellText=rows, colLabels=headers,
                     cellLoc='center', loc='center',
                     colColours=['#3498db', COLOR_BEFORE, COLOR_AFTER, '#f39c12'])

    table.auto_set_font_size(False)
    table.set_fontsize(11)
    table.scale(1, 1.8)

    # Style header
    for j in range(len(headers)):
        table[0, j].set_text_props(color='white', fontweight='bold')

    # Style data cells
    for i in range(1, len(rows) + 1):
        for j in range(len(headers)):
            table[i, j].set_facecolor('#f8f9fa')

    ax.set_title(L['title_overall'], fontsize=14, fontweight='bold', pad=20)

    path = os.path.join(outdir, 'cmp_04_summary_table.png')
    fig.savefig(path)
    plt.close(fig)
    print(f'  [4/6] {path}')


def fig5_cdf_comparison(df_before, df_after, L, outdir):
    """CDF of latency, before vs after (success only)."""
    fig, ax = plt.subplots(figsize=(10, 6))

    lat_b = np.sort(df_before[df_before['is_success']]['LatencyMs'].values)
    lat_a = np.sort(df_after[df_after['is_success']]['LatencyMs'].values)

    cdf_b = np.arange(1, len(lat_b) + 1) / len(lat_b)
    cdf_a = np.arange(1, len(lat_a) + 1) / len(lat_a)

    ax.plot(lat_b, cdf_b, color=COLOR_BEFORE, linewidth=2, label=L['before'])
    ax.plot(lat_a, cdf_a, color=COLOR_AFTER, linewidth=2, label=L['after'])

    ax.axhline(y=0.5, color='gray', linestyle=':', alpha=0.5, label='P50')
    ax.axhline(y=0.95, color='gray', linestyle='--', alpha=0.5, label='P95')

    ax.set_xlabel(L['latency_ms'])
    ax.set_ylabel(L['probability'])
    ax.set_title(L['title_cdf'])
    ax.legend()
    ax.set_xlim(0, max(lat_b.max(), lat_a.max()) * 1.05)

    path = os.path.join(outdir, 'cmp_05_cdf_comparison.png')
    fig.savefig(path)
    plt.close(fig)
    print(f'  [5/6] {path}')


def fig6_resource_success_rate(df_before, df_after, L, outdir):
    """Horizontal bar: success rate per resource, before vs after."""
    fig, ax = plt.subplots(figsize=(14, 8))

    rate_b = df_before.groupby('ResourceLabel')['is_success'].mean() * 100
    rate_a = df_after.groupby('ResourceLabel')['is_success'].mean() * 100

    all_res = sorted(set(rate_b.index) | set(rate_a.index))
    rate_b = rate_b.reindex(all_res, fill_value=0)
    rate_a = rate_a.reindex(all_res, fill_value=0)

    x = np.arange(len(all_res))
    w = 0.35

    ax.barh(x - w/2, rate_b.values, w, label=L['before'], color=COLOR_BEFORE, alpha=0.85)
    ax.barh(x + w/2, rate_a.values, w, label=L['after'], color=COLOR_AFTER, alpha=0.85)

    ax.set_yticks(x)
    ax.set_yticklabels(all_res, fontsize=8)
    ax.set_xlabel(L['success_rate'])
    ax.set_ylabel(L['resource'])
    ax.set_title(L['title_resource_success'])
    ax.set_xlim(0, 110)
    ax.axvline(x=90, color='orange', linestyle='--', alpha=0.5, label='90% objetivo')
    ax.legend(loc='lower right')
    ax.invert_yaxis()

    path = os.path.join(outdir, 'cmp_06_resource_success_rate.png')
    fig.savefig(path)
    plt.close(fig)
    print(f'  [6/6] {path}')


def main():
    parser = argparse.ArgumentParser(description='LwM2M Before/After Comparison Graphs')
    parser.add_argument('before_csv', help='CSV from baseline test (before optimization)')
    parser.add_argument('after_csv', help='CSV from optimized test (after optimization)')
    parser.add_argument('--lang', choices=['es', 'en'], default='en', help='Language')
    parser.add_argument('--outdir', default=None, help='Output directory')
    args = parser.parse_args()

    L = LABELS[args.lang]

    plt.rcParams.update(STYLE)

    df_before = load_csv(args.before_csv)
    df_after = load_csv(args.after_csv)

    outdir = args.outdir or os.path.join(os.path.dirname(args.after_csv), 'comparison')
    os.makedirs(outdir, exist_ok=True)

    print(f'\nBefore: {args.before_csv}')
    print(f'  Rows: {len(df_before)}, Success: {df_before["is_success"].sum()}, '
          f'Fail: {(~df_before["is_success"]).sum()}')
    print(f'After:  {args.after_csv}')
    print(f'  Rows: {len(df_after)}, Success: {df_after["is_success"].sum()}, '
          f'Fail: {(~df_after["is_success"]).sum()}')
    print(f'\nGenerating comparison graphs ({args.lang}) -> {outdir}/')

    fig1_success_per_round(df_before, df_after, L, outdir)
    fig2_failures_by_object(df_before, df_after, L, outdir)
    fig3_latency_by_object(df_before, df_after, L, outdir)
    fig4_overall_summary(df_before, df_after, L, outdir)
    fig5_cdf_comparison(df_before, df_after, L, outdir)
    fig6_resource_success_rate(df_before, df_after, L, outdir)

    print(f'\nDone! 6 comparison graphs saved to {outdir}/')
    print('All images are 300 DPI, suitable for thesis publication.')


if __name__ == '__main__':
    main()
