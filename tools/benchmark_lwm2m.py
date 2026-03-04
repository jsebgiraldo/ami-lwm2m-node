#!/usr/bin/env python3
"""
benchmark_lwm2m.py — LwM2M Observe Interval Benchmark Suite
============================================================
Thesis: Tesis_jsgiraldod_2026_rev_final

Measures network and telemetry performance at different LwM2M observe
intervals (pmin/pmax) over IEEE 802.15.4 Thread mesh.

Test Scenarios:
  - Baseline  : Grupo1 pmin=15/pmax=30, Grupo2 pmin=60/pmax=300
  - Aggressive: ALL resources pmin=1  / pmax=1
  - Medium    : ALL resources pmin=5  / pmax=5
  - Relaxed   : ALL resources pmin=10 / pmax=10

For each scenario the script:
  1. Reconfigures the TB Edge device profile via REST API
  2. Waits for warmup (node re-observe stabilization)
  3. Collects telemetry samples from TB Edge REST API
  4. Computes per-key and aggregate metrics
  5. Optionally runs OT CLI diagnostics via serial shell
  6. Restores original profile after all tests

Metrics captured per scenario:
  - Messages received per key
  - Inter-arrival time stats (min, max, avg, stddev, p50, p95, p99)
  - Data completeness (received vs expected)
  - Throughput (msgs/sec total and per key)
  - RSSI and LQI stability (from telemetry)
  - Estimated CoAP overhead (bytes)

Output:
  results/benchmark/ — CSV per scenario + summary JSON + thesis tables

Usage:
  python benchmark_lwm2m.py                   # Full run (all scenarios)
  python benchmark_lwm2m.py --scenario 1s     # Single scenario
  python benchmark_lwm2m.py --dry-run         # Validate without changing profile
  python benchmark_lwm2m.py --collect-only    # Collect current config (no profile change)
  python benchmark_lwm2m.py --duration 300    # Collection window per scenario (seconds)
"""

import argparse
import csv
import json
import math
import os
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ═══════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════

EDGE_URL = "http://192.168.1.111:8090"
USERNAME = "tenant@thingsboard.org"
PASSWORD = "tenant"

DEVICE_NAME = "ami-esp32c6-2434"
DEVICE_ID = "cc9da070-135b-11f1-80f9-cdb955f2c365"
PROFILE_ID = "b6d55c90-12db-11f1-b535-433a231637c4"

# DLMS polling interval (seconds) — the meter is read every 15s (v0.14.0)
DLMS_POLL_INTERVAL = 15

# ── LwM2M Observe Paths ────────────────────────────────────────────

GRUPO1_PATHS = [
    "/10242_1.0/0/4",   # voltage
    "/10242_1.0/0/5",   # current
    "/10242_1.0/0/6",   # activePower
    "/10242_1.0/0/41",  # activeEnergy
]

GRUPO2_PATHS = [
    "/10242_1.0/0/7",   # reactivePower
    "/10242_1.0/0/10",  # apparentPower
    "/10242_1.0/0/11",  # powerFactor
    "/10242_1.0/0/34",  # totalActivePower
    "/10242_1.0/0/35",  # totalReactivePower
    "/10242_1.0/0/38",  # totalApparentPower
    "/10242_1.0/0/39",  # totalPowerFactor
    "/10242_1.0/0/42",  # reactiveEnergy
    "/10242_1.0/0/45",  # apparentEnergy
    "/10242_1.0/0/49",  # frequency
]

RADIO_PATHS = [
    "/4_1.3/0/2",       # radioSignalStrength
    "/4_1.3/0/3",       # linkQuality
]

FW_PATHS = [
    "/5_1.1/0/3",       # state
    "/5_1.1/0/5",       # updateResult
]

ATTRIBUTE_PATHS = [
    "/3_1.2/0/0",       # manufacturer
    "/3_1.2/0/1",       # modelNumber
    "/3_1.2/0/2",       # serialNumber
]

KEY_NAMES = {
    "/3_1.2/0/0": "manufacturer",
    "/3_1.2/0/1": "modelNumber",
    "/3_1.2/0/2": "serialNumber",
    "/4_1.3/0/2": "radioSignalStrength",
    "/4_1.3/0/3": "linkQuality",
    "/5_1.1/0/3": "fwState",
    "/5_1.1/0/5": "fwUpdateResult",
    "/10242_1.0/0/4": "voltage",
    "/10242_1.0/0/5": "current",
    "/10242_1.0/0/6": "activePower",
    "/10242_1.0/0/7": "reactivePower",
    "/10242_1.0/0/10": "apparentPower",
    "/10242_1.0/0/11": "powerFactor",
    "/10242_1.0/0/34": "totalActivePower",
    "/10242_1.0/0/35": "totalReactivePower",
    "/10242_1.0/0/38": "totalApparentPower",
    "/10242_1.0/0/39": "totalPowerFactor",
    "/10242_1.0/0/41": "activeEnergy",
    "/10242_1.0/0/42": "reactiveEnergy",
    "/10242_1.0/0/45": "apparentEnergy",
    "/10242_1.0/0/49": "frequency",
}

# Telemetry keys we query from TB Edge (14 meter + 2 radio)
TELEMETRY_KEYS = [
    "voltage", "current", "activePower", "reactivePower",
    "apparentPower", "powerFactor", "totalActivePower",
    "totalReactivePower", "totalApparentPower", "totalPowerFactor",
    "activeEnergy", "reactiveEnergy", "apparentEnergy", "frequency",
    "radioSignalStrength", "linkQuality",
]

# CoAP/LwM2M overhead estimate per notification (bytes)
#   CoAP header (4) + token (8) + options (~20) + CBOR payload (~30)
COAP_NOTIFY_OVERHEAD_BYTES = 62

# ── Test Scenarios ──────────────────────────────────────────────────

SCENARIOS = {
    "baseline": {
        "label": "Baseline (Produccion)",
        "description": "Grupo1 pmin=15/pmax=30, Grupo2 pmin=60/pmax=300",
        "grupo1": {"pmin": 15, "pmax": 30},
        "grupo2": {"pmin": 60, "pmax": 300},
        "radio":  {"pmin": 60, "pmax": 300},
        "fw":     {"pmin": 60, "pmax": 300},
        "uniform": False,
        "notify_interval_ms": 0,      # Disabled — rely on DLMS poll (~30s)
    },
    "1s": {
        "label": "Agresivo (1s)",
        "description": "Todos los recursos pmin=1, pmax=1",
        "uniform_pmin": 1,
        "uniform_pmax": 1,
        "uniform": True,
        "notify_interval_ms": 1000,
    },
    "5s": {
        "label": "Medio (5s)",
        "description": "Todos los recursos pmin=5, pmax=5",
        "uniform_pmin": 5,
        "uniform_pmax": 5,
        "uniform": True,
        "notify_interval_ms": 5000,
    },
    "10s": {
        "label": "Relajado (10s)",
        "description": "Todos los recursos pmin=10, pmax=10",
        "uniform_pmin": 10,
        "uniform_pmax": 10,
        "uniform": True,
        "notify_interval_ms": 10000,
    },
}

# Default scenario execution order
SCENARIO_ORDER = ["baseline", "1s", "5s", "10s"]

# ═══════════════════════════════════════════════════════════════════
# TB Edge REST API Helpers
# ═══════════════════════════════════════════════════════════════════

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def api(path, method="GET", data=None, token=None):
    """Make an HTTP request to TB Edge REST API."""
    url = f"{EDGE_URL}{path}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Authorization"] = f"Bearer {token}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30, context=_ssl_ctx) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body_err = e.read().decode() if e.fp else ""
        raise RuntimeError(f"HTTP {e.code}: {body_err[:300]}") from e


def login():
    """Login and return JWT token."""
    resp = api("/api/auth/login", "POST",
               {"username": USERNAME, "password": PASSWORD})
    token = resp.get("token")
    if not token:
        raise RuntimeError("Login failed: no token")
    return token


def get_profile(token):
    """Fetch the full device profile."""
    return api(f"/api/deviceProfile/{PROFILE_ID}", token=token)


def save_profile(token, profile):
    """Save an updated device profile."""
    return api("/api/deviceProfile", "POST", profile, token=token)


def get_telemetry(token, start_ts, end_ts, keys=None, limit=100000):
    """Fetch device telemetry within a time window.

    Returns dict: { key: [ {ts, value}, ... ], ... }
    """
    if keys is None:
        keys = TELEMETRY_KEYS
    keys_param = ",".join(keys)
    path = (
        f"/api/plugins/telemetry/DEVICE/{DEVICE_ID}"
        f"/values/timeseries"
        f"?keys={keys_param}"
        f"&startTs={start_ts}"
        f"&endTs={end_ts}"
        f"&limit={limit}"
        f"&orderBy=ASC"
    )
    return api(path, token=token)


def get_latest_telemetry(token, keys=None):
    """Fetch latest telemetry values."""
    if keys is None:
        keys = TELEMETRY_KEYS
    keys_param = ",".join(keys)
    path = (
        f"/api/plugins/telemetry/DEVICE/{DEVICE_ID}"
        f"/values/timeseries"
        f"?keys={keys_param}"
    )
    return api(path, token=token)


# ═══════════════════════════════════════════════════════════════════
# Profile Reconfiguration
# ═══════════════════════════════════════════════════════════════════

def configure_profile_for_scenario(token, scenario):
    """Change profile observe pmin/pmax according to scenario config.

    Returns the modified profile for later restoration.
    """
    profile = get_profile(token)
    tc = profile.get("profileData", {}).get("transportConfiguration", {})
    observe_attr = tc.get("observeAttr", {})

    all_observe = GRUPO1_PATHS + GRUPO2_PATHS + RADIO_PATHS + FW_PATHS
    all_telemetry = GRUPO1_PATHS + GRUPO2_PATHS + RADIO_PATHS

    attr_lwm2m = {}

    if scenario["uniform"]:
        pmin = scenario["uniform_pmin"]
        pmax = scenario["uniform_pmax"]
        for path in all_observe:
            attr_lwm2m[path] = {"pmin": pmin, "pmax": pmax}
    else:
        for path in GRUPO1_PATHS:
            attr_lwm2m[path] = dict(scenario["grupo1"])
        for path in GRUPO2_PATHS:
            attr_lwm2m[path] = dict(scenario["grupo2"])
        for path in RADIO_PATHS:
            attr_lwm2m[path] = dict(scenario["radio"])
        for path in FW_PATHS:
            attr_lwm2m[path] = dict(scenario["fw"])

    observe_attr["keyName"] = KEY_NAMES
    observe_attr["observe"] = all_observe
    observe_attr["attribute"] = ATTRIBUTE_PATHS
    observe_attr["telemetry"] = all_telemetry
    observe_attr["attributeLwm2m"] = attr_lwm2m
    observe_attr["observeStrategy"] = "SINGLE"

    tc["observeAttr"] = observe_attr
    profile["profileData"]["transportConfiguration"] = tc

    save_profile(token, profile)
    return profile


def restore_baseline(token):
    """Restore the production baseline profile."""
    print("\n  Restaurando perfil baseline...")
    configure_profile_for_scenario(token, SCENARIOS["baseline"])
    print("  Perfil restaurado a produccion.")


# ═══════════════════════════════════════════════════════════════════
# Metrics Computation
# ═══════════════════════════════════════════════════════════════════

def compute_metrics(telemetry_data, duration_sec, scenario_cfg):
    """Compute comprehensive metrics from telemetry time series.

    Args:
        telemetry_data: dict from get_telemetry()
        duration_sec: actual collection window in seconds
        scenario_cfg: scenario configuration dict

    Returns:
        dict with per-key and aggregate metrics
    """
    per_key = {}
    all_inter_arrivals = []
    total_messages = 0
    total_expected = 0

    for key in TELEMETRY_KEYS:
        samples = telemetry_data.get(key, [])
        n = len(samples)
        total_messages += n

        # Determine expected pmax for this key
        if scenario_cfg["uniform"]:
            pmax = scenario_cfg["uniform_pmax"]
        else:
            # Map key back to path to find its group
            path = None
            for p, k in KEY_NAMES.items():
                if k == key:
                    path = p
                    break
            if path in GRUPO1_PATHS:
                pmax = scenario_cfg["grupo1"]["pmax"]
            elif path in GRUPO2_PATHS:
                pmax = scenario_cfg["grupo2"]["pmax"]
            elif path in RADIO_PATHS:
                pmax = scenario_cfg["radio"]["pmax"]
            else:
                pmax = scenario_cfg.get("fw", {}).get("pmax", 300)

        expected = max(1, int(duration_sec / pmax))
        total_expected += expected

        # Compute inter-arrival times
        inter_arrivals = []
        if n >= 2:
            timestamps = sorted([s["ts"] for s in samples])
            for i in range(1, len(timestamps)):
                delta_ms = timestamps[i] - timestamps[i - 1]
                inter_arrivals.append(delta_ms / 1000.0)  # to seconds
            all_inter_arrivals.extend(inter_arrivals)

        # Per-key stats
        km = {
            "key": key,
            "samples": n,
            "expected": expected,
            "completeness_pct": round(100 * n / expected, 1) if expected > 0 else 0,
            "throughput_msgs_per_sec": round(n / duration_sec, 4) if duration_sec > 0 else 0,
        }

        if inter_arrivals:
            km["iat_min_s"] = round(min(inter_arrivals), 3)
            km["iat_max_s"] = round(max(inter_arrivals), 3)
            km["iat_avg_s"] = round(statistics.mean(inter_arrivals), 3)
            km["iat_stddev_s"] = round(statistics.stdev(inter_arrivals), 3) if len(inter_arrivals) > 1 else 0
            km["iat_median_s"] = round(statistics.median(inter_arrivals), 3)
            sorted_iat = sorted(inter_arrivals)
            km["iat_p95_s"] = round(sorted_iat[int(0.95 * len(sorted_iat))], 3)
            km["iat_p99_s"] = round(sorted_iat[int(0.99 * len(sorted_iat))], 3)
        else:
            for stat in ["iat_min_s", "iat_max_s", "iat_avg_s", "iat_stddev_s",
                         "iat_median_s", "iat_p95_s", "iat_p99_s"]:
                km[stat] = None

        # Value range (for numeric keys)
        values = []
        for s in samples:
            try:
                values.append(float(s["value"]))
            except (ValueError, TypeError, KeyError):
                pass
        if values:
            km["value_min"] = round(min(values), 4)
            km["value_max"] = round(max(values), 4)
            km["value_avg"] = round(statistics.mean(values), 4)
            km["value_stddev"] = round(statistics.stdev(values), 4) if len(values) > 1 else 0
        else:
            km["value_min"] = km["value_max"] = km["value_avg"] = km["value_stddev"] = None

        per_key[key] = km

    # Aggregate metrics
    agg = {
        "total_messages": total_messages,
        "total_expected": total_expected,
        "overall_completeness_pct": round(100 * total_messages / total_expected, 1) if total_expected > 0 else 0,
        "overall_throughput_msgs_per_sec": round(total_messages / duration_sec, 4) if duration_sec > 0 else 0,
        "total_keys_reporting": sum(1 for km in per_key.values() if km["samples"] > 0),
        "total_keys_expected": len(TELEMETRY_KEYS),
        "duration_sec": round(duration_sec, 1),
        "estimated_coap_bytes": total_messages * COAP_NOTIFY_OVERHEAD_BYTES,
        "estimated_coap_bps": round(
            (total_messages * COAP_NOTIFY_OVERHEAD_BYTES * 8) / duration_sec, 1
        ) if duration_sec > 0 else 0,
    }

    if all_inter_arrivals:
        agg["iat_global_avg_s"] = round(statistics.mean(all_inter_arrivals), 3)
        agg["iat_global_stddev_s"] = round(statistics.stdev(all_inter_arrivals), 3) if len(all_inter_arrivals) > 1 else 0
        agg["iat_global_min_s"] = round(min(all_inter_arrivals), 3)
        agg["iat_global_max_s"] = round(max(all_inter_arrivals), 3)
    else:
        agg["iat_global_avg_s"] = agg["iat_global_stddev_s"] = None
        agg["iat_global_min_s"] = agg["iat_global_max_s"] = None

    # RSSI/LQI stability
    rssi_data = per_key.get("radioSignalStrength", {})
    lqi_data = per_key.get("linkQuality", {})
    agg["rssi_avg_dBm"] = rssi_data.get("value_avg")
    agg["rssi_stddev_dBm"] = rssi_data.get("value_stddev")
    agg["lqi_avg_pct"] = lqi_data.get("value_avg")
    agg["lqi_stddev_pct"] = lqi_data.get("value_stddev")

    return {"per_key": per_key, "aggregate": agg}


# ═══════════════════════════════════════════════════════════════════
# Serial Diagnostics (optional)
# ═══════════════════════════════════════════════════════════════════

def force_lwm2m_update(port="COM12", baud=115200, timeout=5):
    """Send 'lwm2m update' via serial shell to force re-registration.

    After a profile change on the server, the device must re-register
    so the server re-emits observe requests with the new pmin/pmax.
    Without this, observe parameters only refresh at next lifetime update
    (~270s with lifetime=300).

    Returns True if successful, False otherwise.
    """
    try:
        import serial
    except ImportError:
        print("    [serial] pyserial not available, cannot force LwM2M update")
        return False

    try:
        ser = serial.Serial(port, baud, timeout=timeout, write_timeout=timeout)
        time.sleep(0.5)
        ser.reset_input_buffer()

        # Wake up the shell (send empty line first)
        ser.write(b"\r\n")
        time.sleep(0.3)
        ser.reset_input_buffer()

        # Send lwm2m update command
        ser.write(b"lwm2m update\r\n")
        time.sleep(3)  # Wait for registration update to complete
        response = ser.read(ser.in_waiting or 1).decode("utf-8", errors="replace")
        ser.close()

        if "update" in response.lower() or "complete" in response.lower() or len(response.strip()) > 0:
            print(f"        LwM2M update enviado OK")
            return True
        else:
            print(f"        LwM2M update: sin confirmacion ({response[:100]})")
            return True  # Command was sent regardless

    except Exception as e:
        print(f"    [serial] Error forcing LwM2M update: {e}")
        return False


def set_firmware_notify_interval(interval_ms, port="COM12", baud=115200, timeout=5):
    """Set the firmware's observer re-notify interval via shell command.

    The firmware uses this interval to re-trigger lwm2m_notify_observer()
    for all power-meter resources.  When interval_ms == 0 the firmware
    only notifies after DLMS polls (~30 s).

    Returns True if the command was sent successfully.
    """
    try:
        import serial
    except ImportError:
        print("    [serial] pyserial not available, cannot set notify interval")
        return False

    try:
        ser = serial.Serial(port, baud, timeout=timeout, write_timeout=timeout)
        time.sleep(0.5)
        ser.reset_input_buffer()

        # Wake up the shell
        ser.write(b"\r\n")
        time.sleep(0.3)
        ser.reset_input_buffer()

        cmd = f"notify_interval {interval_ms}\r\n"
        ser.write(cmd.encode())
        time.sleep(1)
        response = ser.read(ser.in_waiting or 1).decode("utf-8", errors="replace")
        ser.close()

        print(f"        notify_interval -> {interval_ms} ms"
              f"{' (disabled)' if interval_ms == 0 else ''}")
        return True

    except Exception as e:
        print(f"    [serial] Error setting notify interval: {e}")
        return False


def collect_ot_diagnostics(port="COM12", baud=115200, timeout=5):
    """Collect OpenThread CLI diagnostics via serial shell.

    Returns dict with parsed OT counters, or None if serial unavailable.
    """
    try:
        import serial
    except ImportError:
        print("    [serial] pyserial not available, skipping OT diagnostics")
        return None

    diag = {}
    try:
        ser = serial.Serial(port, baud, timeout=timeout, write_timeout=timeout)
        time.sleep(0.5)
        ser.reset_input_buffer()

        def send_cmd(cmd, wait=2):
            ser.reset_input_buffer()
            ser.write((cmd + "\r\n").encode())
            time.sleep(wait)
            data = ser.read(ser.in_waiting or 1)
            return data.decode("utf-8", errors="replace")

        # OT state
        out = send_cmd("ot state")
        for line in out.split("\n"):
            line = line.strip()
            if line in ("leader", "router", "child", "detached", "disabled"):
                diag["ot_state"] = line
                break

        # OT counters mac
        out = send_cmd("ot counters mac")
        for line in out.split("\n"):
            line = line.strip()
            if ":" in line:
                parts = line.split(":")
                if len(parts) == 2:
                    k, v = parts[0].strip(), parts[1].strip()
                    try:
                        diag[f"mac_{k}"] = int(v)
                    except ValueError:
                        pass

        # OT counters mle
        out = send_cmd("ot counters mle")
        for line in out.split("\n"):
            line = line.strip()
            if ":" in line:
                parts = line.split(":")
                if len(parts) == 2:
                    k, v = parts[0].strip(), parts[1].strip()
                    try:
                        diag[f"mle_{k}"] = int(v)
                    except ValueError:
                        pass

        # OT router table
        out = send_cmd("ot router table")
        diag["router_table_raw"] = out.strip()

        ser.close()
        return diag

    except Exception as e:
        print(f"    [serial] Error collecting OT diagnostics: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════
# Report Generation
# ═══════════════════════════════════════════════════════════════════

def save_per_key_csv(metrics, scenario_name, output_dir):
    """Save per-key metrics as CSV."""
    fname = os.path.join(output_dir, f"per_key_{scenario_name}.csv")
    fieldnames = [
        "key", "samples", "expected", "completeness_pct",
        "throughput_msgs_per_sec",
        "iat_min_s", "iat_max_s", "iat_avg_s", "iat_stddev_s",
        "iat_median_s", "iat_p95_s", "iat_p99_s",
        "value_min", "value_max", "value_avg", "value_stddev",
    ]
    with open(fname, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for key in TELEMETRY_KEYS:
            km = metrics["per_key"].get(key, {})
            row = {fn: km.get(fn, "") for fn in fieldnames}
            row["key"] = key
            writer.writerow(row)
    return fname


def save_raw_timeseries_csv(telemetry_data, scenario_name, output_dir):
    """Save raw time series data as CSV for post-analysis."""
    fname = os.path.join(output_dir, f"raw_ts_{scenario_name}.csv")
    with open(fname, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp_ms", "datetime_utc", "key", "value"])
        for key in TELEMETRY_KEYS:
            samples = telemetry_data.get(key, [])
            for s in sorted(samples, key=lambda x: x["ts"]):
                ts_ms = s["ts"]
                dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                writer.writerow([ts_ms, dt.isoformat(), key, s.get("value", "")])
    return fname


def save_summary_json(all_results, output_dir):
    """Save the complete benchmark summary as JSON."""
    fname = os.path.join(output_dir, "benchmark_summary.json")
    summary = {
        "benchmark": "LwM2M Observe Interval Performance",
        "thesis": "Tesis_jsgiraldod_2026_rev_final",
        "device": DEVICE_NAME,
        "device_id": DEVICE_ID,
        "profile": PROFILE_ID,
        "dlms_poll_interval_s": DLMS_POLL_INTERVAL,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "scenarios": {},
    }
    for name, result in all_results.items():
        scenario_summary = {
            "config": {
                k: v for k, v in SCENARIOS[name].items()
                if k not in ("label",)
            },
            "timing": {
                "start": result.get("start_iso"),
                "end": result.get("end_iso"),
                "warmup_sec": result.get("warmup_sec"),
                "collection_sec": result.get("collection_sec"),
            },
            "aggregate": result.get("metrics", {}).get("aggregate", {}),
            "per_key": result.get("metrics", {}).get("per_key", {}),
        }
        if result.get("ot_diag_before"):
            scenario_summary["ot_diag_before"] = result["ot_diag_before"]
        if result.get("ot_diag_after"):
            scenario_summary["ot_diag_after"] = result["ot_diag_after"]
        summary["scenarios"][name] = scenario_summary

    with open(fname, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)
    return fname


def generate_thesis_table(all_results, output_dir):
    """Generate a LaTeX-ready comparison table for the thesis."""
    fname = os.path.join(output_dir, "thesis_table.txt")
    lines = []

    # ── Table 1: Aggregate comparison ──
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
        if name not in all_results:
            continue
        agg = all_results[name].get("metrics", {}).get("aggregate", {})
        label = SCENARIOS[name]["label"]
        lines.append(
            f"{label:<22} "
            f"{agg.get('total_messages', 0):>6} "
            f"{agg.get('overall_completeness_pct', 0):>6.1f}% "
            f"{agg.get('overall_throughput_msgs_per_sec', 0):>8.3f} "
            f"{_fmt(agg.get('iat_global_avg_s')):>8} "
            f"{_fmt(None):>8} "  # p95 is per-key, not global
            f"{agg.get('estimated_coap_bytes', 0) / 1024:>7.1f}K "
            f"{agg.get('estimated_coap_bps', 0):>9.1f} "
            f"{_fmt(agg.get('rssi_avg_dBm')):>6} "
            f"{_fmt(agg.get('lqi_avg_pct')):>5}"
        )

    lines.append("")
    lines.append("")

    # ── Table 2: Per-key IAT comparison ──
    lines.append("=" * 110)
    lines.append("INTER-ARRIVAL TIME (IAT) POR RECURSO — Comparacion entre escenarios")
    lines.append("=" * 110)
    lines.append("")

    header2 = f"{'Key':<24}"
    for name in SCENARIO_ORDER:
        if name in all_results:
            header2 += f" | {'avg':>6} {'p95':>6} {'N':>5}"
    lines.append(header2)
    lines.append("-" * 110)

    for key in TELEMETRY_KEYS:
        row = f"{key:<24}"
        for name in SCENARIO_ORDER:
            if name not in all_results:
                continue
            km = all_results[name].get("metrics", {}).get("per_key", {}).get(key, {})
            avg = _fmt(km.get("iat_avg_s"))
            p95 = _fmt(km.get("iat_p95_s"))
            n = km.get("samples", 0)
            row += f" | {avg:>6} {p95:>6} {n:>5}"
        lines.append(row)

    lines.append("")
    lines.append("")

    # ── Table 3: LaTeX-formatted ──
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
        if name not in all_results:
            continue
        agg = all_results[name].get("metrics", {}).get("aggregate", {})
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

    with open(fname, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return fname


def _fmt(val):
    """Format a numeric value or return '-' if None."""
    if val is None:
        return "-"
    return f"{val:.3f}" if isinstance(val, float) else str(val)


def _fmt_latex(val):
    """Format for LaTeX: no None."""
    if val is None:
        return "--"
    return f"{val:.2f}" if isinstance(val, float) else str(val)


# ═══════════════════════════════════════════════════════════════════
# Test Orchestrator
# ═══════════════════════════════════════════════════════════════════

def run_scenario(token, scenario_name, duration_sec, warmup_sec,
                 serial_port=None, dry_run=False, collect_only=False):
    """Execute a single benchmark scenario.

    Returns dict with timing info, metrics, and optional OT diagnostics.
    """
    scenario = SCENARIOS[scenario_name]
    print(f"\n{'=' * 60}")
    print(f"  ESCENARIO: {scenario['label']}")
    print(f"  {scenario['description']}")
    print(f"  Duracion: {duration_sec}s, Warmup: {warmup_sec}s")
    print(f"{'=' * 60}")

    result = {
        "scenario": scenario_name,
        "config": scenario,
        "warmup_sec": warmup_sec,
        "collection_sec": duration_sec,
    }

    # Step 1: Reconfigure profile (unless collect_only or dry_run)
    if not collect_only and not dry_run:
        print(f"\n  [1/7] Reconfigurando perfil -> {scenario['label']}...")
        try:
            configure_profile_for_scenario(token, scenario)
            print(f"        Perfil actualizado.")
        except Exception as e:
            print(f"        ERROR al actualizar perfil: {e}")
            result["error"] = str(e)
            return result

        # Set firmware-side notify interval via serial
        notify_ms = scenario.get("notify_interval_ms", 0)
        if serial_port:
            print(f"\n  [1b/7] Configurando firmware notify_interval={notify_ms}ms...")
            set_firmware_notify_interval(notify_ms, serial_port)

            print(f"\n  [1c/7] Forzando LwM2M re-registration...")
            force_lwm2m_update(serial_port)
            print(f"        Esperando 15s para que observes se re-establezcan...")
            time.sleep(15)
        else:
            print(f"        AVISO: Sin puerto serial — no se puede configurar firmware")
    elif dry_run:
        print(f"\n  [1/7] DRY-RUN: Se configuraria {scenario['label']} (sin cambios)")
    else:
        print(f"\n  [1/7] COLLECT-ONLY: Usando configuracion actual")

    # Step 2: OT diagnostics BEFORE
    if serial_port and not dry_run:
        print(f"\n  [2/7] Recopilando diagnosticos OT (antes)...")
        result["ot_diag_before"] = collect_ot_diagnostics(serial_port)
        if result["ot_diag_before"]:
            state = result["ot_diag_before"].get("ot_state", "?")
            print(f"        OT state: {state}")
    else:
        print(f"\n  [2/7] Diagnosticos OT: {'omitido (dry-run)' if dry_run else 'sin puerto serial'}")

    # Step 3: Warmup
    if not dry_run and not collect_only:
        print(f"\n  [3/7] Warmup: esperando {warmup_sec}s para estabilizacion...")
        _countdown(warmup_sec)
    elif collect_only:
        print(f"\n  [3/7] COLLECT-ONLY: warmup reducido (10s)...")
        if not dry_run:
            _countdown(10)
    else:
        print(f"\n  [3/7] DRY-RUN: Se esperarian {warmup_sec}s de warmup")

    # Step 4: Collect telemetry
    if not dry_run:
        start_ts = int(time.time() * 1000)
        result["start_iso"] = datetime.now(tz=timezone.utc).isoformat()
        print(f"\n  [4/7] Recopilando telemetria durante {duration_sec}s...")
        print(f"        Inicio: {result['start_iso']}")
        _countdown(duration_sec)
        end_ts = int(time.time() * 1000)
        result["end_iso"] = datetime.now(tz=timezone.utc).isoformat()
        print(f"        Fin:    {result['end_iso']}")

        # Fetch telemetry from TB Edge
        print(f"        Consultando TB Edge API...")
        telemetry = get_telemetry(token, start_ts, end_ts)
        total_samples = sum(len(v) for v in telemetry.values())
        keys_with_data = sum(1 for v in telemetry.values() if v)
        print(f"        Recibidos: {total_samples} muestras en {keys_with_data}/{len(TELEMETRY_KEYS)} llaves")

        # Compute metrics
        actual_duration = (end_ts - start_ts) / 1000.0
        result["metrics"] = compute_metrics(telemetry, actual_duration, scenario)
        result["telemetry_raw"] = telemetry

        agg = result["metrics"]["aggregate"]
        print(f"\n        --- Resumen Rapido ---")
        print(f"        Total mensajes:  {agg['total_messages']}")
        print(f"        Completitud:     {agg['overall_completeness_pct']:.1f}%")
        print(f"        Throughput:      {agg['overall_throughput_msgs_per_sec']:.3f} msgs/s")
        print(f"        IAT promedio:    {_fmt(agg.get('iat_global_avg_s'))} s")
        print(f"        CoAP estimado:   {agg['estimated_coap_bytes'] / 1024:.1f} KB")
        print(f"        RSSI promedio:   {_fmt(agg.get('rssi_avg_dBm'))} dBm")
        print(f"        LQI promedio:    {_fmt(agg.get('lqi_avg_pct'))}%")
    else:
        print(f"\n  [4/7] DRY-RUN: Se recopilarian {duration_sec}s de telemetria")
        # Simulate with current data
        print(f"        Consultando telemetria actual (snapshot)...")
        latest = get_latest_telemetry(token)
        keys_with_data = sum(1 for v in latest.values() if v)
        print(f"        Telemetria activa: {keys_with_data}/{len(TELEMETRY_KEYS)} llaves")
        result["metrics"] = {"per_key": {}, "aggregate": {"note": "dry-run snapshot"}}

    # Step 5: Quick rate verification (for non-baseline scenarios)
    if not dry_run and scenario_name != "baseline":
        print(f"\n  [5/7] Verificacion de tasa de recepcion...")
        # Check last 30s to confirm observe interval matches
        verify_ts_end = int(time.time() * 1000)
        verify_ts_start = verify_ts_end - 30000
        try:
            verify_data = get_telemetry(token, verify_ts_start, verify_ts_end, keys=["voltage"])
            v_samples = verify_data.get("voltage", [])
            if len(v_samples) >= 2:
                ts_list = sorted([s["ts"] for s in v_samples])
                deltas = [(ts_list[i+1] - ts_list[i])/1000 for i in range(len(ts_list)-1)]
                avg_iat = sum(deltas)/len(deltas)
                print(f"        voltage: {len(v_samples)} muestras en 30s, IAT avg={avg_iat:.1f}s")
            else:
                print(f"        voltage: {len(v_samples)} muestras en 30s")
        except Exception as e:
            print(f"        Error verificando tasa: {e}")
    else:
        print(f"\n  [5/7] Verificacion de tasa: {'omitido (baseline/dry-run)'}")

    # Step 6: OT diagnostics AFTER
    if serial_port and not dry_run:
        print(f"\n  [6/7] Recopilando diagnosticos OT (despues)...")
        result["ot_diag_after"] = collect_ot_diagnostics(serial_port)
    else:
        print(f"\n  [6/7] Diagnosticos OT: {'omitido (dry-run)' if dry_run else 'sin puerto serial'}")

    # Step 7: Restore baseline notify interval for next scenario
    if serial_port and not dry_run and scenario_name != "baseline":
        print(f"\n  [7/7] Restaurando notify_interval=0 (pre-next scenario)...")
        set_firmware_notify_interval(0, serial_port)
    else:
        print(f"\n  [7/7] Restore notify: {'n/a' if scenario_name == 'baseline' else 'omitido'}")
    return result


def _countdown(seconds):
    """Print countdown progress."""
    interval = max(1, seconds // 20)  # ~20 ticks
    remaining = seconds
    while remaining > 0:
        wait = min(interval, remaining)
        time.sleep(wait)
        remaining -= wait
        pct = 100 * (seconds - remaining) / seconds
        bar = "#" * int(pct / 5) + "." * (20 - int(pct / 5))
        print(f"\r        [{bar}] {seconds - remaining}/{seconds}s ({pct:.0f}%)", end="", flush=True)
    print()


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════

def _override_edge_url(url):
    """Override the EDGE_URL global from CLI argument."""
    global EDGE_URL
    EDGE_URL = url


def main():
    parser = argparse.ArgumentParser(
        description="LwM2M Observe Interval Benchmark Suite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--scenario", "-s",
        choices=list(SCENARIOS.keys()) + ["all"],
        default="all",
        help="Scenario to run (default: all)",
    )
    parser.add_argument(
        "--duration", "-d",
        type=int, default=300,
        help="Collection window per scenario in seconds (default: 300)",
    )
    parser.add_argument(
        "--warmup", "-w",
        type=int, default=90,
        help="Warmup time after profile change in seconds (default: 90)",
    )
    parser.add_argument(
        "--serial-port", "-p",
        default=None,
        help="Serial port for OT diagnostics (e.g., COM12). Optional.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate configuration without changing profile or waiting",
    )
    parser.add_argument(
        "--collect-only",
        action="store_true",
        help="Collect metrics with current profile (no reconfiguration)",
    )
    parser.add_argument(
        "--output-dir", "-o",
        default=None,
        help="Output directory (default: results/benchmark/YYYYMMDD_HHMMSS)",
    )
    parser.add_argument(
        "--edge-url",
        default=None,
        help=f"TB Edge URL (default: {EDGE_URL})",
    )

    args = parser.parse_args()

    if args.edge_url:
        _override_edge_url(args.edge_url)

    # Determine output directory
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.output_dir:
        output_dir = args.output_dir
    else:
        base_results = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "results", "benchmark"
        )
        output_dir = os.path.join(base_results, timestamp)
    os.makedirs(output_dir, exist_ok=True)

    # Determine scenarios to run
    if args.scenario == "all":
        scenarios_to_run = list(SCENARIO_ORDER)
    else:
        scenarios_to_run = [args.scenario]

    # Print banner
    n_resources = len(TELEMETRY_KEYS)
    total_time_est = len(scenarios_to_run) * (args.duration + args.warmup)

    print()
    print("=" * 60)
    print("  LwM2M OBSERVE BENCHMARK SUITE")
    print("  Tesis_jsgiraldod_2026_rev_final")
    print("=" * 60)
    print(f"  Device     : {DEVICE_NAME}")
    print(f"  Edge URL   : {EDGE_URL}")
    print(f"  Profile    : {PROFILE_ID}")
    print(f"  Resources  : {n_resources} telemetry keys")
    print(f"  DLMS poll  : {DLMS_POLL_INTERVAL}s")
    print(f"  Scenarios  : {', '.join(scenarios_to_run)}")
    print(f"  Duration   : {args.duration}s per scenario")
    print(f"  Warmup     : {args.warmup}s per scenario")
    print(f"  Est. total : {total_time_est / 60:.0f} min")
    print(f"  Serial     : {args.serial_port or 'not configured'}")
    print(f"  Dry-run    : {args.dry_run}")
    print(f"  Output     : {output_dir}")
    print(f"  Started    : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Login
    print("\n  Autenticando...")
    try:
        token = login()
        print(f"  OK")
    except Exception as e:
        print(f"  ERROR: {e}")
        sys.exit(1)

    # Run scenarios
    all_results = {}
    for i, name in enumerate(scenarios_to_run, 1):
        print(f"\n{'#' * 60}")
        print(f"  ESCENARIO {i}/{len(scenarios_to_run)}: {name}")
        print(f"{'#' * 60}")

        result = run_scenario(
            token=token,
            scenario_name=name,
            duration_sec=args.duration,
            warmup_sec=args.warmup,
            serial_port=args.serial_port,
            dry_run=args.dry_run,
            collect_only=args.collect_only,
        )
        all_results[name] = result

        # Save per-scenario CSV (if we have data)
        if result.get("metrics", {}).get("per_key"):
            csv_path = save_per_key_csv(result["metrics"], name, output_dir)
            print(f"\n  CSV por llave guardado: {csv_path}")

        if result.get("telemetry_raw"):
            raw_path = save_raw_timeseries_csv(result["telemetry_raw"], name, output_dir)
            print(f"  CSV raw timeseries:    {raw_path}")

        # Pausa entre escenarios
        if i < len(scenarios_to_run) and not args.dry_run:
            pause = 15
            print(f"\n  Pausa de {pause}s antes del siguiente escenario...")
            time.sleep(pause)

    # Restore baseline after non-baseline tests
    if not args.dry_run and not args.collect_only:
        if scenarios_to_run != ["baseline"]:
            restore_baseline(token)
            # Also restore firmware notify interval to disabled
            if args.serial_port:
                print("  Restaurando firmware notify_interval=0...")
                set_firmware_notify_interval(0, args.serial_port)

    # Generate reports
    print(f"\n{'=' * 60}")
    print("  GENERANDO REPORTES")
    print(f"{'=' * 60}")

    summary_path = save_summary_json(all_results, output_dir)
    print(f"  JSON resumen:    {summary_path}")

    if len(all_results) > 0 and not args.dry_run:
        thesis_path = generate_thesis_table(all_results, output_dir)
        print(f"  Tabla tesis:     {thesis_path}")

    # Final summary
    print(f"\n{'=' * 60}")
    print("  BENCHMARK COMPLETO")
    print(f"{'=' * 60}")
    print(f"  Escenarios ejecutados: {len(all_results)}")
    print(f"  Directorio salida:     {output_dir}")
    print(f"  Finalizado:            {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if not args.dry_run:
        print(f"\n  --- Resumen de resultados ---")
        for name in scenarios_to_run:
            if name not in all_results:
                continue
            agg = all_results[name].get("metrics", {}).get("aggregate", {})
            label = SCENARIOS[name]["label"]
            print(f"  {label}:")
            print(f"    Mensajes: {agg.get('total_messages', '?')}, "
                  f"Completitud: {agg.get('overall_completeness_pct', '?')}%, "
                  f"Throughput: {agg.get('overall_throughput_msgs_per_sec', '?')} msgs/s")

    print(f"\n{'=' * 60}")
    print(f"  Archivos generados:")
    for f in sorted(os.listdir(output_dir)):
        fpath = os.path.join(output_dir, f)
        size_kb = os.path.getsize(fpath) / 1024
        print(f"    {f} ({size_kb:.1f} KB)")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
