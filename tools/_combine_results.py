#!/usr/bin/env python3
"""
_combine_results.py — Combine individually-run scenario CSVs into
a unified benchmark_summary.json and thesis_table.txt.

Usage:
  python tools/_combine_results.py results/benchmark/20260303_184204
"""

import csv
import json
import os
import sys
from datetime import datetime, timezone

SCENARIOS = {
    "baseline": {
        "label": "Baseline (Produccion)",
        "description": "Grupo1 pmin=15/pmax=30, Grupo2 pmin=60/pmax=300",
        "grupo1": {"pmin": 15, "pmax": 30},
        "grupo2": {"pmin": 60, "pmax": 300},
        "radio":  {"pmin": 60, "pmax": 300},
        "fw":     {"pmin": 60, "pmax": 300},
        "uniform": False,
        "notify_interval_ms": 0,
    },
    "1s": {
        "label": "Agresivo (1s)",
        "description": "Todos los recursos pmin=1, pmax=1",
        "uniform_pmin": 1, "uniform_pmax": 1,
        "uniform": True, "notify_interval_ms": 1000,
    },
    "5s": {
        "label": "Medio (5s)",
        "description": "Todos los recursos pmin=5, pmax=5",
        "uniform_pmin": 5, "uniform_pmax": 5,
        "uniform": True, "notify_interval_ms": 5000,
    },
    "10s": {
        "label": "Relajado (10s)",
        "description": "Todos los recursos pmin=10, pmax=10",
        "uniform_pmin": 10, "uniform_pmax": 10,
        "uniform": True, "notify_interval_ms": 10000,
    },
}

SCENARIO_ORDER = ["baseline", "1s", "5s", "10s"]

TELEMETRY_KEYS = [
    "voltage", "current", "activePower", "reactivePower",
    "apparentPower", "powerFactor", "totalActivePower",
    "totalReactivePower", "totalApparentPower", "totalPowerFactor",
    "activeEnergy", "reactiveEnergy", "apparentEnergy",
    "frequency", "radioSignalStrength", "linkQuality",
]

COAP_MSG_BYTES = 62
DURATION_SEC = 300


def _safe_float(v):
    try:
        return float(v) if v and v.strip() and v.strip() != '' else None
    except (ValueError, TypeError):
        return None


def _safe_int(v):
    try:
        return int(v) if v and v.strip() else 0
    except (ValueError, TypeError):
        return 0


def load_per_key_csv(result_dir, scenario_name):
    """Load per_key CSV and reconstruct metrics dict."""
    path = os.path.join(result_dir, f"per_key_{scenario_name}.csv")
    if not os.path.exists(path):
        return None
    per_key = {}
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = row["key"]
            per_key[key] = {
                "key": key,
                "samples": _safe_int(row.get("samples")),
                "expected": _safe_int(row.get("expected")),
                "completeness_pct": _safe_float(row.get("completeness_pct")) or 0,
                "throughput_msgs_per_sec": _safe_float(row.get("throughput_msgs_per_sec")) or 0,
                "iat_min_s": _safe_float(row.get("iat_min_s")),
                "iat_max_s": _safe_float(row.get("iat_max_s")),
                "iat_avg_s": _safe_float(row.get("iat_avg_s")),
                "iat_stddev_s": _safe_float(row.get("iat_stddev_s")),
                "iat_median_s": _safe_float(row.get("iat_median_s")),
                "iat_p95_s": _safe_float(row.get("iat_p95_s")),
                "iat_p99_s": _safe_float(row.get("iat_p99_s")),
                "value_min": _safe_float(row.get("value_min")),
                "value_max": _safe_float(row.get("value_max")),
                "value_avg": _safe_float(row.get("value_avg")),
                "value_stddev": _safe_float(row.get("value_stddev")),
            }
    return per_key


def load_raw_csv(result_dir, scenario_name):
    """Load raw timeseries CSV."""
    path = os.path.join(result_dir, f"raw_ts_{scenario_name}.csv")
    if not os.path.exists(path):
        return None
    rows = []
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)
    return rows


def compute_aggregate(per_key, raw_rows, scenario_cfg):
    """Compute aggregate metrics from per_key data."""
    total_messages = sum(pk.get("samples", 0) for pk in per_key.values())
    keys_reporting = sum(1 for pk in per_key.values() if pk.get("samples", 0) > 0)
    duration = DURATION_SEC

    # Expected messages
    if scenario_cfg.get("uniform"):
        interval = scenario_cfg.get("uniform_pmax", 30)
        expected = len(TELEMETRY_KEYS) * (duration / interval)
    else:
        expected = max(total_messages, 1)  # baseline doesn't have uniform

    completeness = (total_messages / expected * 100) if expected > 0 else 0
    throughput = total_messages / duration if duration > 0 else 0

    # IAT global stats from per-key averages
    iat_vals = [pk["iat_avg_s"] for pk in per_key.values()
                if pk.get("iat_avg_s") is not None and pk.get("samples", 0) > 1]

    iat_global_avg = sum(iat_vals) / len(iat_vals) if iat_vals else None
    if iat_vals:
        mean = iat_global_avg
        iat_stddev = (sum((x - mean) ** 2 for x in iat_vals) / len(iat_vals)) ** 0.5
    else:
        iat_stddev = None

    iat_mins = [pk["iat_min_s"] for pk in per_key.values()
                if pk.get("iat_min_s") is not None]
    iat_maxs = [pk["iat_max_s"] for pk in per_key.values()
                if pk.get("iat_max_s") is not None]

    coap_bytes = total_messages * COAP_MSG_BYTES
    coap_bps = (coap_bytes * 8) / duration if duration > 0 else 0

    # RSSI/LQI
    rssi_key = per_key.get("radioSignalStrength", {})
    lqi_key = per_key.get("linkQuality", {})
    rssi_avg = rssi_key.get("value_avg") if rssi_key.get("samples", 0) > 0 else None
    lqi_avg = lqi_key.get("value_avg") if lqi_key.get("samples", 0) > 0 else None

    return {
        "total_messages": total_messages,
        "total_expected": int(expected),
        "overall_completeness_pct": round(completeness, 1),
        "overall_throughput_msgs_per_sec": round(throughput, 4),
        "total_keys_reporting": keys_reporting,
        "total_keys_expected": len(TELEMETRY_KEYS),
        "duration_sec": duration,
        "estimated_coap_bytes": coap_bytes,
        "estimated_coap_bps": round(coap_bps, 1),
        "iat_global_avg_s": round(iat_global_avg, 3) if iat_global_avg else None,
        "iat_global_stddev_s": round(iat_stddev, 3) if iat_stddev else None,
        "iat_global_min_s": min(iat_mins) if iat_mins else None,
        "iat_global_max_s": max(iat_maxs) if iat_maxs else None,
        "rssi_avg_dBm": rssi_avg,
        "rssi_stddev_dBm": None,
        "lqi_avg_pct": lqi_avg,
        "lqi_stddev_pct": None,
    }


def _fmt(val):
    if val is None:
        return "-"
    return f"{val:.3f}" if isinstance(val, float) else str(val)


def _fmt_latex(val):
    if val is None:
        return "--"
    return f"{val:.2f}" if isinstance(val, float) else str(val)


def main():
    if len(sys.argv) < 2:
        print("Usage: python _combine_results.py <result_dir>")
        sys.exit(1)

    result_dir = sys.argv[1]
    if not os.path.isdir(result_dir):
        print(f"ERROR: {result_dir} is not a directory")
        sys.exit(1)

    print(f"Combining results from: {result_dir}")

    # Build combined summary
    summary = {
        "benchmark": "LwM2M Observe Interval Performance",
        "thesis": "Tesis_jsgiraldod_2026_rev_final",
        "device": "ami-esp32c6-2434",
        "device_id": "cc9da070-135b-11f1-80f9-cdb955f2c365",
        "profile": "b6d55c90-12db-11f1-b535-433a231637c4",
        "dlms_poll_interval_s": 15,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "scenarios": {},
    }

    found_scenarios = []
    for name in SCENARIO_ORDER:
        per_key = load_per_key_csv(result_dir, name)
        if per_key is None:
            print(f"  {name}: SKIPPED (no CSV)")
            continue

        raw_rows = load_raw_csv(result_dir, name)
        scenario_cfg = SCENARIOS[name]
        aggregate = compute_aggregate(per_key, raw_rows, scenario_cfg)

        # Determine timing from raw CSV
        start_iso = None
        end_iso = None
        if raw_rows:
            timestamps = [int(r.get("timestamp_ms", 0)) for r in raw_rows]
            if timestamps:
                start_ts = min(timestamps)
                end_ts = max(timestamps)
                start_iso = datetime.fromtimestamp(start_ts / 1000, tz=timezone.utc).isoformat()
                end_iso = datetime.fromtimestamp(end_ts / 1000, tz=timezone.utc).isoformat()

        scenario_summary = {
            "config": {k: v for k, v in scenario_cfg.items() if k != "label"},
            "timing": {
                "start": start_iso,
                "end": end_iso,
                "warmup_sec": 90,
                "collection_sec": DURATION_SEC,
            },
            "aggregate": aggregate,
            "per_key": per_key,
        }
        summary["scenarios"][name] = scenario_summary
        found_scenarios.append(name)

        print(f"  {name}: {aggregate['total_messages']} msgs, "
              f"{aggregate['overall_throughput_msgs_per_sec']:.3f} msgs/s, "
              f"IAT avg={_fmt(aggregate.get('iat_global_avg_s'))}s")

    # Save combined JSON
    json_path = os.path.join(result_dir, "benchmark_summary.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"\n  Saved: {json_path}")

    # Generate thesis table
    lines = []
    lines.append("=" * 90)
    lines.append("TABLA COMPARATIVA DE ESCENARIOS — Metricas Agregadas")
    lines.append("(Para Tesis_jsgiraldod_2026_rev_final)")
    lines.append("=" * 90)
    lines.append("")

    header = (
        f"{'Escenario':<22} {'Msgs':>6} {'Compl%':>7} "
        f"{'Msgs/s':>8} {'IAT avg':>8} {'IAT p95':>8} "
        f"{'CoAP KB':>8} {'CoAP bps':>9} "
        f"{'RSSI':>6} {'LQI':>5}"
    )
    lines.append(header)
    lines.append("-" * 90)

    for name in SCENARIO_ORDER:
        if name not in found_scenarios:
            continue
        agg = summary["scenarios"][name]["aggregate"]
        label = SCENARIOS[name]["label"]
        lines.append(
            f"{label:<22} "
            f"{agg.get('total_messages', 0):>6} "
            f"{agg.get('overall_completeness_pct', 0):>6.1f}% "
            f"{agg.get('overall_throughput_msgs_per_sec', 0):>8.3f} "
            f"{_fmt(agg.get('iat_global_avg_s')):>8} "
            f"{_fmt(None):>8} "
            f"{agg.get('estimated_coap_bytes', 0) / 1024:>7.1f}K "
            f"{agg.get('estimated_coap_bps', 0):>9.1f} "
            f"{_fmt(agg.get('rssi_avg_dBm')):>6} "
            f"{_fmt(agg.get('lqi_avg_pct')):>5}"
        )

    lines.append("")
    lines.append("")

    lines.append("=" * 110)
    lines.append("INTER-ARRIVAL TIME (IAT) POR RECURSO — Comparacion entre escenarios")
    lines.append("=" * 110)
    lines.append("")

    header2 = f"{'Key':<24}"
    for name in SCENARIO_ORDER:
        if name in found_scenarios:
            header2 += f" | {'avg':>6} {'p95':>6} {'N':>5}"
    lines.append(header2)
    lines.append("-" * 110)

    for key in TELEMETRY_KEYS:
        row = f"{key:<24}"
        for name in SCENARIO_ORDER:
            if name not in found_scenarios:
                continue
            km = summary["scenarios"][name]["per_key"].get(key, {})
            avg = _fmt(km.get("iat_avg_s"))
            p95 = _fmt(km.get("iat_p95_s"))
            n = km.get("samples", 0)
            row += f" | {avg:>6} {p95:>6} {n:>5}"
        lines.append(row)

    lines.append("")
    lines.append("")

    lines.append("=" * 90)
    lines.append("LATEX TABLE (copy-paste into thesis)")
    lines.append("=" * 90)
    lines.append("")
    lines.append(r"\begin{table}[htbp]")
    lines.append(r"\centering")
    lines.append(r"\caption{Rendimiento de transporte LwM2M bajo diferentes intervalos de observacion}")
    lines.append(r"\label{tab:lwm2m-benchmark}")
    lines.append(r"\begin{tabular}{lrrrrrrr}")
    lines.append(r"\toprule")
    lines.append(
        r"\textbf{Escenario} & \textbf{Msgs} & \textbf{Compl.\%} & "
        r"\textbf{Msgs/s} & \textbf{IAT avg (s)} & "
        r"\textbf{CoAP (KB)} & \textbf{RSSI (dBm)} & \textbf{LQI (\%)} \\"
    )
    lines.append(r"\midrule")

    for name in SCENARIO_ORDER:
        if name not in found_scenarios:
            continue
        agg = summary["scenarios"][name]["aggregate"]
        label = SCENARIOS[name]["label"]
        lines.append(
            f"{label} & "
            f"{agg.get('total_messages', 0)} & "
            f"{agg.get('overall_completeness_pct', 0):.1f} & "
            f"{agg.get('overall_throughput_msgs_per_sec', 0):.3f} & "
            f"{_fmt_latex(agg.get('iat_global_avg_s'))} & "
            f"{agg.get('estimated_coap_bytes', 0) / 1024:.1f} & "
            f"{_fmt_latex(agg.get('rssi_avg_dBm'))} & "
            f"{_fmt_latex(agg.get('lqi_avg_pct'))} \\\\"
        )

    lines.append(r"\bottomrule")
    lines.append(r"\end{tabular}")
    lines.append(r"\end{table}")
    lines.append("")

    table_path = os.path.join(result_dir, "thesis_table.txt")
    with open(table_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"  Saved: {table_path}")

    print(f"\n  Combined {len(found_scenarios)} scenarios: {', '.join(found_scenarios)}")


if __name__ == "__main__":
    main()
