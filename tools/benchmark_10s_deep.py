#!/usr/bin/env python3
"""
benchmark_10s_deep.py — Deep analysis of 10s LwM2M observe interval
====================================================================
Thesis: Tesis_jsgiraldod_2026_rev_final

NOTA HISTÓRICA: Este script analiza datos del escenario Relajado (10s)
de firmware v0.13.0 (DLMS poll 30s). A partir de v0.15.1 (DLMS poll 15s
+ notificación por umbral), pmax=10 < DLMS_poll=15s produce 0 mensajes.
El escenario fue descontinuado; se conserva como referencia.

Performs a comprehensive single-scenario analysis at pmin=10/pmax=10:
  - 10 min collection (600s) for statistical significance
  - Per-second message rate timeline
  - Docker container resource usage (CPU, memory) from Edge RPi4
  - CoAP/6LoWPAN protocol overhead estimation
  - Per-key IAT distribution & jitter analysis
  - Cumulative data volume over time
  - Network utilization vs. capacity

Generates 10+ thesis-ready PNG graphs.

Usage:
  python benchmark_10s_deep.py                          # Full run
  python benchmark_10s_deep.py --duration 300            # 5 min
  python benchmark_10s_deep.py --analyze-only <dir>      # Re-graph existing data
"""

import argparse
import csv
import json
import math
import os
import ssl
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker
    import matplotlib.dates as mdates
    import numpy as np
    HAS_MPL = True
except ImportError:
    HAS_MPL = False
    print("WARNING: matplotlib not installed, graphs will be skipped")

# ═══════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════

EDGE_URL = "http://192.168.1.111:8090"
EDGE_SSH_HOST = "192.168.1.111"
EDGE_SSH_USER = "root"
EDGE_SSH_PASS = "root"
USERNAME = "tenant@thingsboard.org"
PASSWORD = "tenant"

DEVICE_NAME = "ami-esp32c6-2434"
DEVICE_ID = "cc9da070-135b-11f1-80f9-cdb955f2c365"
PROFILE_ID = "b6d55c90-12db-11f1-b535-433a231637c4"

DLMS_POLL_INTERVAL = 30  # seconds

TELEMETRY_KEYS = [
    "voltage", "current", "activePower", "reactivePower",
    "apparentPower", "powerFactor", "totalActivePower",
    "totalReactivePower", "totalApparentPower", "totalPowerFactor",
    "activeEnergy", "reactiveEnergy", "apparentEnergy", "frequency",
    "radioSignalStrength", "linkQuality",
]

# Group keys by type for analysis
GRUPO1_KEYS = ["voltage", "current", "activePower", "activeEnergy"]
GRUPO2_KEYS = [
    "apparentPower", "powerFactor", "totalActivePower",
    "totalReactivePower", "totalApparentPower", "totalPowerFactor",
    "reactiveEnergy", "apparentEnergy", "frequency",
]
RADIO_KEYS = ["radioSignalStrength", "linkQuality"]
METER_KEYS = GRUPO1_KEYS + ["reactivePower"] + GRUPO2_KEYS

# Protocol overhead estimates (bytes)
COAP_HEADER = 4
COAP_TOKEN = 8
COAP_OPTIONS = 20          # URI-path + observe option + content-format
CBOR_PAYLOAD_AVG = 30      # Average CBOR-encoded float value
COAP_MSG_BYTES = COAP_HEADER + COAP_TOKEN + COAP_OPTIONS + CBOR_PAYLOAD_AVG  # 62

# 6LoWPAN / IEEE 802.15.4 framing
IEEE_802154_HEADER = 23     # MHR (2 FCF + 1 seq + 4 PAN + 8 dst + 8 src)
IEEE_802154_FCS = 2
SIXLOWPAN_HEADER = 40      # IPHC compressed IPv6 + UDP
THREAD_MLE_OVERHEAD = 0    # MLE is separate, not per-data-packet
# Total radio frame per CoAP message (single fragment)
RADIO_FRAME_BYTES = IEEE_802154_HEADER + SIXLOWPAN_HEADER + COAP_MSG_BYTES + IEEE_802154_FCS  # ~127

# IEEE 802.15.4 capacity
IEEE_802154_DATA_RATE_BPS = 250_000   # 250 kbit/s raw
IEEE_802154_MAX_FRAME = 127           # bytes
# Effective throughput ~= 50% due to CSMA/CA, ACKs, IFS
IEEE_802154_EFFECTIVE_BPS = 125_000   # ~50% utilization

# LwM2M observe config for this test
PMIN = 10
PMAX = 10

# ═══════════════════════════════════════════════════════════════════
# LwM2M paths
# ═══════════════════════════════════════════════════════════════════

ALL_OBSERVE_PATHS = [
    "/10242_1.0/0/4", "/10242_1.0/0/5", "/10242_1.0/0/6",
    "/10242_1.0/0/7", "/10242_1.0/0/10", "/10242_1.0/0/11",
    "/10242_1.0/0/34", "/10242_1.0/0/35", "/10242_1.0/0/38",
    "/10242_1.0/0/39", "/10242_1.0/0/41", "/10242_1.0/0/42",
    "/10242_1.0/0/45", "/10242_1.0/0/49",
    "/4_1.3/0/2", "/4_1.3/0/3",
]

FW_PATHS = ["/5_1.1/0/3", "/5_1.1/0/5"]

# ═══════════════════════════════════════════════════════════════════
# TB Edge REST API helpers
# ═══════════════════════════════════════════════════════════════════

def api(path, token=None, method="GET", body=None):
    url = f"{EDGE_URL}{path}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        return json.loads(resp.read().decode())


def login():
    r = api("/api/auth/login", body={"username": USERNAME, "password": PASSWORD}, method="POST")
    return r["token"]


def get_telemetry(token, start_ts, end_ts, keys=None, limit=100000):
    if keys is None:
        keys = TELEMETRY_KEYS
    keys_param = ",".join(keys)
    path = (
        f"/api/plugins/telemetry/DEVICE/{DEVICE_ID}"
        f"/values/timeseries"
        f"?keys={keys_param}&startTs={start_ts}&endTs={end_ts}"
        f"&limit={limit}&orderBy=ASC"
    )
    return api(path, token=token)


def get_profile(token):
    return api(f"/api/deviceProfile/{PROFILE_ID}", token=token)


def save_profile(token, profile):
    api(f"/api/deviceProfile", token=token, method="POST", body=profile)


def set_profile_10s(token):
    """Set ALL observe paths to pmin=10, pmax=10."""
    profile = get_profile(token)
    tp = profile.get("profileData", {}).get("transportConfiguration", {})
    oa = tp.get("observeAttr", {})
    attr_lwm2m = oa.get("attributeLwm2m", {})
    
    all_paths = ALL_OBSERVE_PATHS + FW_PATHS
    for p in all_paths:
        attr_lwm2m[p] = {"pmin": PMIN, "pmax": PMAX}
    
    oa["attributeLwm2m"] = attr_lwm2m
    tp["observeAttr"] = oa
    profile["profileData"]["transportConfiguration"] = tp
    save_profile(token, profile)


def restore_baseline(token):
    """Restore production baseline profile."""
    profile = get_profile(token)
    tp = profile.get("profileData", {}).get("transportConfiguration", {})
    oa = tp.get("observeAttr", {})
    attr_lwm2m = oa.get("attributeLwm2m", {})
    
    grupo1_paths = ["/10242_1.0/0/4", "/10242_1.0/0/5", "/10242_1.0/0/6", "/10242_1.0/0/41"]
    grupo2_paths = [
        "/10242_1.0/0/7", "/10242_1.0/0/10", "/10242_1.0/0/11",
        "/10242_1.0/0/34", "/10242_1.0/0/35", "/10242_1.0/0/38",
        "/10242_1.0/0/39", "/10242_1.0/0/42", "/10242_1.0/0/45",
        "/10242_1.0/0/49",
    ]
    for p in grupo1_paths:
        attr_lwm2m[p] = {"pmin": 15, "pmax": 30}
    for p in grupo2_paths + ["/4_1.3/0/2", "/4_1.3/0/3"] + FW_PATHS:
        attr_lwm2m[p] = {"pmin": 60, "pmax": 300}
    
    oa["attributeLwm2m"] = attr_lwm2m
    tp["observeAttr"] = oa
    profile["profileData"]["transportConfiguration"] = tp
    save_profile(token, profile)


# ═══════════════════════════════════════════════════════════════════
# SSH + Docker/System Stats Collection (via paramiko to RPi4)
# ═══════════════════════════════════════════════════════════════════

try:
    import paramiko
    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False

_ssh_client = None


def _get_ssh():
    """Get or create a persistent SSH connection to Edge RPi4."""
    global _ssh_client
    if _ssh_client is not None:
        tr = _ssh_client.get_transport()
        if tr and tr.is_active():
            return _ssh_client
    if not HAS_PARAMIKO:
        return None
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(EDGE_SSH_HOST, username=EDGE_SSH_USER,
                       password=EDGE_SSH_PASS, timeout=10)
        _ssh_client = client
        return client
    except Exception as e:
        print(f"    [ssh] Connection failed: {e}")
        return None


def _ssh_exec(cmd, timeout=10):
    """Execute command via SSH and return stdout."""
    ssh = _get_ssh()
    if not ssh:
        return None
    try:
        _, stdout, _ = ssh.exec_command(cmd, timeout=timeout)
        return stdout.read().decode().strip()
    except Exception:
        global _ssh_client
        _ssh_client = None
        return None


def collect_docker_stats_snapshot():
    """SSH to RPi4 and capture docker stats (one-shot)."""
    output = _ssh_exec(
        "docker stats --no-stream --format "
        "'{{.Name}},{{.CPUPerc}},{{.MemUsage}},{{.MemPerc}},{{.NetIO}},{{.BlockIO}}'"
    )
    if not output:
        return None
    stats = []
    for line in output.split("\n"):
        line = line.strip().strip("'")
        parts = line.split(",")
        if len(parts) >= 6:
            stats.append({
                "name": parts[0],
                "cpu_pct": parts[1].strip().rstrip("%"),
                "mem_usage": parts[2].strip(),
                "mem_pct": parts[3].strip().rstrip("%"),
                "net_io": parts[4].strip(),
                "block_io": parts[5].strip(),
            })
    return stats if stats else None


def collect_system_stats_snapshot():
    """Collect system-level metrics from Edge RPi4 via SSH."""
    result = {}

    # CPU load averages
    out = _ssh_exec("cat /proc/loadavg")
    if out:
        parts = out.split()
        result["load_1m"] = float(parts[0])
        result["load_5m"] = float(parts[1])
        result["load_15m"] = float(parts[2])

    # Memory
    out = _ssh_exec("free -b | grep Mem")
    if out:
        parts = out.split()
        total = int(parts[1])
        used = int(parts[2])
        avail_out = _ssh_exec("free -b | grep Mem | awk '{print $7}'")
        avail = int(avail_out) if avail_out else total - used
        result["mem_total_mb"] = round(total / 1024**2, 1)
        result["mem_used_mb"] = round(used / 1024**2, 1)
        result["mem_avail_mb"] = round(avail / 1024**2, 1)
        result["mem_used_pct"] = round(100 * used / total, 1) if total > 0 else 0

    # Disk usage /
    out = _ssh_exec("df -B1 / | tail -1")
    if out:
        parts = out.split()
        if len(parts) >= 5:
            result["disk_used_gb"] = round(int(parts[2]) / 1024**3, 2)
            result["disk_total_gb"] = round(int(parts[1]) / 1024**3, 2)
            result["disk_used_pct"] = parts[4].rstrip("%")

    # Network I/O (cumulative counters since boot) — eth0 + wpan0 (Thread)
    net_data = {}
    for iface_name in ["eth0", "wpan0"]:
        out = _ssh_exec(f"cat /proc/net/dev | grep '{iface_name}:'")
        if out:
            parts = out.split()
            iface_clean = parts[0].rstrip(":")
            net_data[iface_clean] = {
                "rx_bytes": int(parts[1]),
                "tx_bytes": int(parts[9]),
            }
    if net_data:
        # Primary: eth0 for overall, wpan0 for Thread radio
        if "eth0" in net_data:
            result["net_iface"] = "eth0"
            result["net_rx_bytes"] = net_data["eth0"]["rx_bytes"]
            result["net_tx_bytes"] = net_data["eth0"]["tx_bytes"]
        if "wpan0" in net_data:
            result["wpan_rx_bytes"] = net_data["wpan0"]["rx_bytes"]
            result["wpan_tx_bytes"] = net_data["wpan0"]["tx_bytes"]

    # CPU temperature
    out = _ssh_exec("cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null")
    if out:
        try:
            result["cpu_temp_c"] = round(int(out) / 1000, 1)
        except ValueError:
            pass

    # Uptime
    out = _ssh_exec("cat /proc/uptime")
    if out:
        result["uptime_s"] = round(float(out.split()[0]))

    return result if result else None


def parse_bytes(size_str):
    """Parse '12.3MB' or '45kB' or '1.2GB' to bytes."""
    size_str = size_str.strip()
    multipliers = {
        'B': 1, 'kB': 1024, 'KB': 1024, 'MB': 1024**2,
        'MiB': 1024**2, 'GB': 1024**3, 'GiB': 1024**3,
    }
    for suffix, mult in sorted(multipliers.items(), key=lambda x: -len(x[0])):
        if size_str.endswith(suffix):
            try:
                return float(size_str[:-len(suffix)].strip()) * mult
            except ValueError:
                return 0
    try:
        return float(size_str)
    except ValueError:
        return 0


# ═══════════════════════════════════════════════════════════════════
# Data Analysis
# ═══════════════════════════════════════════════════════════════════

def analyze_telemetry(telemetry_data, start_ts, end_ts, duration_sec):
    """Comprehensive analysis of telemetry data for 10s scenario."""
    
    results = {
        "config": {
            "pmin": PMIN, "pmax": PMAX,
            "dlms_poll_s": DLMS_POLL_INTERVAL,
            "duration_s": duration_sec,
            "num_keys": len(TELEMETRY_KEYS),
            "coap_msg_bytes": COAP_MSG_BYTES,
            "radio_frame_bytes": RADIO_FRAME_BYTES,
        },
        "aggregate": {},
        "per_key": {},
        "timeline": {},
        "protocol_overhead": {},
        "network_utilization": {},
    }
    
    # ── Flatten all samples with timestamps ──
    all_samples = []
    per_key_ts = defaultdict(list)
    per_key_values = defaultdict(list)
    
    for key in TELEMETRY_KEYS:
        entries = telemetry_data.get(key, [])
        for e in entries:
            ts = int(e["ts"])
            val = float(e["value"])
            all_samples.append({"ts": ts, "key": key, "value": val})
            per_key_ts[key].append(ts)
            per_key_values[key].append(val)
    
    all_samples.sort(key=lambda x: x["ts"])
    total_msgs = len(all_samples)
    
    # ── Per-key analysis ──
    for key in TELEMETRY_KEYS:
        timestamps = sorted(per_key_ts[key])
        values = per_key_values[key]
        n = len(timestamps)
        
        iats = []
        for i in range(1, len(timestamps)):
            iats.append((timestamps[i] - timestamps[i-1]) / 1000.0)
        
        # Expected messages: duration / pmax
        if key in RADIO_KEYS:
            expected = 0  # radio keys may not report
        elif key in GRUPO1_KEYS:
            expected = int(duration_sec / PMAX)
        else:
            expected = int(duration_sec / PMAX)
        
        entry = {
            "samples": n,
            "expected": expected,
            "completeness_pct": round(100 * n / expected, 1) if expected > 0 else 0,
            "throughput_mps": round(n / duration_sec, 4) if duration_sec > 0 else 0,
            "coap_bytes": n * COAP_MSG_BYTES,
            "radio_bytes": n * RADIO_FRAME_BYTES,
        }
        
        if iats:
            entry.update({
                "iat_min": round(min(iats), 3),
                "iat_max": round(max(iats), 3),
                "iat_avg": round(statistics.mean(iats), 3),
                "iat_median": round(statistics.median(iats), 3),
                "iat_stddev": round(statistics.stdev(iats), 3) if len(iats) > 1 else 0,
                "iat_p95": round(sorted(iats)[int(0.95 * len(iats))], 3) if iats else None,
                "jitter_avg": round(statistics.mean([abs(iat - PMAX) for iat in iats]), 3),
            })
        
        if values:
            entry.update({
                "val_min": round(min(values), 4),
                "val_max": round(max(values), 4),
                "val_avg": round(statistics.mean(values), 4),
                "val_stddev": round(statistics.stdev(values), 4) if len(values) > 1 else 0,
            })
        
        results["per_key"][key] = entry
    
    # ── Aggregate metrics ──
    reporting_keys = [k for k in TELEMETRY_KEYS if len(per_key_ts[k]) > 0]
    all_iats = []
    for key in TELEMETRY_KEYS:
        ts_list = sorted(per_key_ts[key])
        for i in range(1, len(ts_list)):
            all_iats.append((ts_list[i] - ts_list[i-1]) / 1000.0)
    
    results["aggregate"] = {
        "total_messages": total_msgs,
        "keys_reporting": len(reporting_keys),
        "keys_expected": len(TELEMETRY_KEYS),
        "throughput_mps": round(total_msgs / duration_sec, 4),
        "iat_global_avg": round(statistics.mean(all_iats), 3) if all_iats else None,
        "iat_global_median": round(statistics.median(all_iats), 3) if all_iats else None,
        "iat_global_stddev": round(statistics.stdev(all_iats), 3) if len(all_iats) > 1 else None,
        "iat_global_p95": round(sorted(all_iats)[int(0.95 * len(all_iats))], 3) if all_iats else None,
    }
    
    # ── Timeline: messages per 10-second bin ──
    bin_size_ms = 10_000
    min_ts = start_ts
    bins = defaultdict(int)
    bin_keys = defaultdict(set)
    for s in all_samples:
        b = ((s["ts"] - min_ts) // bin_size_ms) * bin_size_ms + min_ts
        bins[b] += 1
        bin_keys[b].add(s["key"])
    
    timeline = []
    t = min_ts
    while t <= end_ts:
        timeline.append({
            "ts": t,
            "elapsed_s": (t - min_ts) / 1000.0,
            "msgs": bins.get(t, 0),
            "keys": len(bin_keys.get(t, set())),
        })
        t += bin_size_ms
    results["timeline"] = timeline
    
    # ── Protocol overhead ──
    coap_total_bytes = total_msgs * COAP_MSG_BYTES
    radio_total_bytes = total_msgs * RADIO_FRAME_BYTES
    coap_bps = (coap_total_bytes * 8) / duration_sec if duration_sec > 0 else 0
    radio_bps = (radio_total_bytes * 8) / duration_sec if duration_sec > 0 else 0
    
    results["protocol_overhead"] = {
        "coap_msg_bytes": COAP_MSG_BYTES,
        "radio_frame_bytes": RADIO_FRAME_BYTES,
        "total_coap_bytes": coap_total_bytes,
        "total_radio_bytes": radio_total_bytes,
        "total_coap_kb": round(coap_total_bytes / 1024, 2),
        "total_radio_kb": round(radio_total_bytes / 1024, 2),
        "coap_bps": round(coap_bps, 1),
        "radio_bps": round(radio_bps, 1),
        "coap_kbps": round(coap_bps / 1000, 3),
        "radio_kbps": round(radio_bps / 1000, 3),
        "payload_efficiency_pct": round(100 * CBOR_PAYLOAD_AVG / RADIO_FRAME_BYTES, 1),
        # Per hour projection
        "coap_bytes_per_hour": round(coap_total_bytes * 3600 / duration_sec),
        "radio_bytes_per_hour": round(radio_total_bytes * 3600 / duration_sec),
        "msgs_per_hour": round(total_msgs * 3600 / duration_sec),
    }
    
    # ── Network utilization ──
    utilization_pct = (radio_bps / IEEE_802154_EFFECTIVE_BPS) * 100
    results["network_utilization"] = {
        "ieee802154_raw_bps": IEEE_802154_DATA_RATE_BPS,
        "ieee802154_effective_bps": IEEE_802154_EFFECTIVE_BPS,
        "ami_radio_bps": round(radio_bps, 1),
        "utilization_pct": round(utilization_pct, 4),
        "headroom_pct": round(100 - utilization_pct, 4),
        "max_concurrent_nodes_est": int(IEEE_802154_EFFECTIVE_BPS / radio_bps) if radio_bps > 0 else 999,
    }
    
    return results


# ═══════════════════════════════════════════════════════════════════
# Graph Generation
# ═══════════════════════════════════════════════════════════════════

def setup_style():
    plt.rcParams.update({
        "figure.figsize": (10, 6),
        "figure.dpi": 150,
        "font.size": 11,
        "axes.titlesize": 13,
        "axes.labelsize": 12,
        "xtick.labelsize": 10,
        "ytick.labelsize": 10,
        "legend.fontsize": 10,
        "figure.facecolor": "white",
        "axes.grid": True,
        "grid.alpha": 0.3,
    })


def fig01_message_rate_timeline(results, raw_samples, outdir, fmt):
    """Timeline of messages per 10s bin over entire collection."""
    timeline = results["timeline"]
    if not timeline:
        return
    
    elapsed = [t["elapsed_s"] / 60.0 for t in timeline]
    msgs = [t["msgs"] for t in timeline]
    keys = [t["keys"] for t in timeline]
    
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
    
    ax1.bar(elapsed, msgs, width=10/60, color="#2196F3", alpha=0.8, edgecolor="none")
    ax1.set_ylabel("Mensajes por ventana de 10s")
    ax1.set_title("Tasa de mensajes LwM2M — Escenario 10s (pmin=10, pmax=10)")
    avg_rate = statistics.mean(msgs) if msgs else 0
    ax1.axhline(y=avg_rate, color="red", linestyle="--", linewidth=1.5, label=f"Promedio: {avg_rate:.1f} msgs/10s")
    ax1.legend()
    
    ax2.bar(elapsed, keys, width=10/60, color="#4CAF50", alpha=0.8, edgecolor="none")
    ax2.set_ylabel("Llaves distintas por ventana")
    ax2.set_xlabel("Tiempo (minutos)")
    ax2.set_ylim(0, 16)
    
    plt.tight_layout()
    path = os.path.join(outdir, f"01_message_rate_timeline.{fmt}")
    plt.savefig(path, bbox_inches="tight")
    plt.close()
    print(f"  -> {path}")


def fig02_iat_distribution(results, raw_samples, outdir, fmt):
    """IAT histogram + CDF for all keys combined."""
    all_iats = []
    for key in METER_KEYS:
        pk = results["per_key"].get(key, {})
        if pk.get("samples", 0) < 2:
            continue
        # Reconstruct IATs from raw
        key_samples = sorted([s for s in raw_samples if s["key"] == key], key=lambda x: x["ts"])
        for i in range(1, len(key_samples)):
            iat = (key_samples[i]["ts"] - key_samples[i-1]["ts"]) / 1000.0
            all_iats.append(iat)
    
    if not all_iats:
        return
    
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))
    
    # Histogram
    ax1.hist(all_iats, bins=50, color="#FF9800", edgecolor="white", alpha=0.85, density=True)
    ax1.axvline(x=PMAX, color="red", linestyle="--", linewidth=2, label=f"pmax = {PMAX}s")
    ax1.axvline(x=statistics.mean(all_iats), color="blue", linestyle="-.", linewidth=1.5,
                label=f"Media = {statistics.mean(all_iats):.2f}s")
    ax1.set_xlabel("Inter-Arrival Time (s)")
    ax1.set_ylabel("Densidad")
    ax1.set_title("Distribucion de IAT — Todos los recursos")
    ax1.legend()
    
    # CDF
    sorted_iats = sorted(all_iats)
    cdf = np.arange(1, len(sorted_iats) + 1) / len(sorted_iats)
    ax2.plot(sorted_iats, cdf, color="#2196F3", linewidth=2)
    ax2.axvline(x=PMAX, color="red", linestyle="--", linewidth=1.5, label=f"pmax = {PMAX}s")
    ax2.axhline(y=0.5, color="gray", linestyle=":", alpha=0.5)
    ax2.axhline(y=0.95, color="gray", linestyle=":", alpha=0.5)
    # Mark p50 and p95
    p50 = sorted_iats[int(0.5 * len(sorted_iats))]
    p95 = sorted_iats[int(0.95 * len(sorted_iats))]
    ax2.annotate(f"p50 = {p50:.1f}s", xy=(p50, 0.5), fontsize=9,
                 xytext=(p50 + 2, 0.4), arrowprops=dict(arrowstyle="->"))
    ax2.annotate(f"p95 = {p95:.1f}s", xy=(p95, 0.95), fontsize=9,
                 xytext=(p95 + 2, 0.85), arrowprops=dict(arrowstyle="->"))
    ax2.set_xlabel("Inter-Arrival Time (s)")
    ax2.set_ylabel("CDF")
    ax2.set_title("CDF acumulativa de IAT")
    ax2.legend()
    
    plt.tight_layout()
    path = os.path.join(outdir, f"02_iat_distribution.{fmt}")
    plt.savefig(path, bbox_inches="tight")
    plt.close()
    print(f"  -> {path}")


def fig03_per_key_iat_boxplot(results, raw_samples, outdir, fmt):
    """Box plot of IAT per telemetry key."""
    keys_order = [k for k in METER_KEYS if results["per_key"].get(k, {}).get("samples", 0) >= 2]
    
    iat_data = []
    labels = []
    for key in keys_order:
        key_samples = sorted([s for s in raw_samples if s["key"] == key], key=lambda x: x["ts"])
        iats = [(key_samples[i]["ts"] - key_samples[i-1]["ts"]) / 1000.0
                for i in range(1, len(key_samples))]
        if iats:
            iat_data.append(iats)
            labels.append(key.replace("total", "t.").replace("Energy", "Ener").replace("Power", "Pwr"))
    
    if not iat_data:
        return
    
    fig, ax = plt.subplots(figsize=(14, 6))
    bp = ax.boxplot(iat_data, tick_labels=labels, patch_artist=True, showfliers=True,
                    flierprops=dict(marker="o", markersize=4, alpha=0.5))
    
    colors = plt.cm.Set3(np.linspace(0, 1, len(iat_data)))
    for patch, color in zip(bp["boxes"], colors):
        patch.set_facecolor(color)
    
    ax.axhline(y=PMAX, color="red", linestyle="--", linewidth=1.5, label=f"pmax = {PMAX}s")
    ax.set_ylabel("Inter-Arrival Time (s)")
    ax.set_title("IAT por recurso — Escenario 10s")
    ax.legend()
    plt.xticks(rotation=45, ha="right")
    plt.tight_layout()
    path = os.path.join(outdir, f"03_iat_per_key_boxplot.{fmt}")
    plt.savefig(path, bbox_inches="tight")
    plt.close()
    print(f"  -> {path}")


def fig04_cumulative_data_volume(results, raw_samples, outdir, fmt):
    """Cumulative bytes over time at different protocol layers."""
    if not raw_samples:
        return
    
    samples_sorted = sorted(raw_samples, key=lambda x: x["ts"])
    min_ts = samples_sorted[0]["ts"]
    
    times = [(s["ts"] - min_ts) / 1000.0 / 60.0 for s in samples_sorted]
    cum_coap = [COAP_MSG_BYTES * (i + 1) / 1024 for i in range(len(samples_sorted))]
    cum_radio = [RADIO_FRAME_BYTES * (i + 1) / 1024 for i in range(len(samples_sorted))]
    cum_payload = [CBOR_PAYLOAD_AVG * (i + 1) / 1024 for i in range(len(samples_sorted))]
    
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.fill_between(times, 0, cum_radio, alpha=0.3, color="#F44336", label="802.15.4 (radio)")
    ax.fill_between(times, 0, cum_coap, alpha=0.4, color="#2196F3", label="CoAP + 6LoWPAN")
    ax.fill_between(times, 0, cum_payload, alpha=0.5, color="#4CAF50", label="Payload CBOR util")
    
    ax.set_xlabel("Tiempo (minutos)")
    ax.set_ylabel("Volumen acumulado (KB)")
    ax.set_title("Volumen de datos acumulado por capa de protocolo — Escenario 10s")
    ax.legend(loc="upper left")
    
    # Add right y-axis for message count
    ax2 = ax.twinx()
    cum_msgs = list(range(1, len(samples_sorted) + 1))
    ax2.plot(times, cum_msgs, color="black", linewidth=1, linestyle=":", alpha=0.6, label="# Mensajes")
    ax2.set_ylabel("Mensajes acumulados")
    ax2.legend(loc="lower right")
    
    plt.tight_layout()
    path = os.path.join(outdir, f"04_cumulative_data_volume.{fmt}")
    plt.savefig(path, bbox_inches="tight")
    plt.close()
    print(f"  -> {path}")


def fig05_protocol_overhead_breakdown(results, outdir, fmt):
    """Stacked bar showing protocol overhead per layer."""
    overhead = results["protocol_overhead"]
    total_msgs = results["aggregate"]["total_messages"]
    
    layers = ["Payload\n(CBOR)", "CoAP\nHeader+Opts", "6LoWPAN\n(IPHC+UDP)", "802.15.4\n(MHR+FCS)"]
    per_msg = [CBOR_PAYLOAD_AVG, COAP_HEADER + COAP_TOKEN + COAP_OPTIONS, SIXLOWPAN_HEADER, IEEE_802154_HEADER + IEEE_802154_FCS]
    total_kb = [x * total_msgs / 1024 for x in per_msg]
    colors = ["#4CAF50", "#2196F3", "#FF9800", "#F44336"]
    
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))
    
    # Per message breakdown
    bars = ax1.barh(layers, per_msg, color=colors, edgecolor="white", height=0.6)
    for bar, val in zip(bars, per_msg):
        ax1.text(bar.get_width() + 1, bar.get_y() + bar.get_height()/2,
                 f"{val} B", va="center", fontsize=10, fontweight="bold")
    ax1.set_xlabel("Bytes por mensaje")
    ax1.set_title(f"Overhead por capa — por mensaje ({RADIO_FRAME_BYTES} B total)")
    ax1.set_xlim(0, max(per_msg) * 1.3)
    
    # Total volume breakdown (stacked)
    bottom = 0
    for layer, kb, color in zip(layers, total_kb, colors):
        ax2.bar("10s\nEscenario", kb, bottom=bottom, color=color, edgecolor="white", label=layer)
        if kb > 0.5:
            ax2.text(0, bottom + kb/2, f"{kb:.1f} KB", ha="center", va="center", fontsize=9, fontweight="bold")
        bottom += kb
    ax2.set_ylabel("Volumen total (KB)")
    ax2.set_title(f"Overhead total — {total_msgs} mensajes en {results['config']['duration_s']}s")
    ax2.legend(loc="upper right")
    
    plt.tight_layout()
    path = os.path.join(outdir, f"05_protocol_overhead.{fmt}")
    plt.savefig(path, bbox_inches="tight")
    plt.close()
    print(f"  -> {path}")


def fig06_network_utilization(results, outdir, fmt):
    """Gauge-style chart showing 802.15.4 bandwidth utilization."""
    net = results["network_utilization"]
    
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))
    
    # Pie gauge
    utilization = net["utilization_pct"]
    headroom = net["headroom_pct"]
    ax1.pie([utilization, headroom],
            labels=[f"AMI LwM2M\n{utilization:.3f}%", f"Disponible\n{headroom:.3f}%"],
            colors=["#F44336", "#E0E0E0"],
            startangle=90, autopct=None,
            wedgeprops=dict(width=0.4, edgecolor="white"))
    ax1.set_title("Utilizacion del canal 802.15.4")
    
    # Scalability bar
    max_nodes = net["max_concurrent_nodes_est"]
    node_counts = [1, 5, 10, 25, 50, 100, min(max_nodes, 200)]
    utilizations = [n * utilization for n in node_counts]
    colors_bar = ["#4CAF50" if u < 50 else "#FF9800" if u < 80 else "#F44336" for u in utilizations]
    
    bars = ax2.barh([str(n) for n in node_counts], utilizations, color=colors_bar, edgecolor="white")
    ax2.axvline(x=100, color="red", linewidth=2, linestyle="-", label="Capacidad maxima")
    ax2.axvline(x=70, color="#FF9800", linewidth=1.5, linestyle="--", label="Umbral 70%")
    for bar, u in zip(bars, utilizations):
        ax2.text(bar.get_width() + 1, bar.get_y() + bar.get_height()/2,
                 f"{u:.2f}%", va="center", fontsize=9)
    ax2.set_xlabel("Utilizacion del canal (%)")
    ax2.set_ylabel("Numero de nodos")
    ax2.set_title("Escalabilidad — Nodos concurrentes en red Thread")
    ax2.legend()
    ax2.set_xlim(0, max(120, max(utilizations) * 1.2))
    
    plt.tight_layout()
    path = os.path.join(outdir, f"06_network_utilization.{fmt}")
    plt.savefig(path, bbox_inches="tight")
    plt.close()
    print(f"  -> {path}")


def fig07_completeness_heatmap(results, outdir, fmt):
    """Heatmap-style bar chart of completeness per key."""
    keys = [k for k in METER_KEYS if k in results["per_key"]]
    completeness = [results["per_key"][k].get("completeness_pct", 0) for k in keys]
    samples = [results["per_key"][k].get("samples", 0) for k in keys]
    expected = [results["per_key"][k].get("expected", 0) for k in keys]
    
    fig, ax = plt.subplots(figsize=(14, 6))
    
    short_labels = [k.replace("total", "t.").replace("Energy", "Ener").replace("Power", "Pwr") for k in keys]
    colors = ["#4CAF50" if c >= 30 else "#FF9800" if c >= 10 else "#F44336" for c in completeness]
    
    bars = ax.bar(short_labels, completeness, color=colors, edgecolor="white")
    ax.axhline(y=100, color="blue", linestyle="--", linewidth=1, label="100% esperado")
    
    for bar, s, e, c in zip(bars, samples, expected, completeness):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1,
                f"{s}/{e}", ha="center", va="bottom", fontsize=8)
    
    ax.set_ylabel("Completitud (%)")
    ax.set_title("Completitud de datos por recurso — Escenario 10s")
    ax.legend()
    plt.xticks(rotation=45, ha="right")
    plt.tight_layout()
    path = os.path.join(outdir, f"07_completeness_per_key.{fmt}")
    plt.savefig(path, bbox_inches="tight")
    plt.close()
    print(f"  -> {path}")


def fig08_data_rate_over_time(results, raw_samples, outdir, fmt):
    """Data rate (bytes/s) over time at different layers."""
    if not raw_samples:
        return
    
    samples_sorted = sorted(raw_samples, key=lambda x: x["ts"])
    min_ts = samples_sorted[0]["ts"]
    duration_s = results["config"]["duration_s"]
    
    # 30-second sliding window
    window_ms = 30_000
    step_ms = 5_000
    
    times = []
    coap_bps_list = []
    radio_bps_list = []
    msg_rate_list = []
    
    t = min_ts
    while t < min_ts + duration_s * 1000:
        window_msgs = sum(1 for s in samples_sorted if t <= s["ts"] < t + window_ms)
        window_secs = window_ms / 1000
        coap_bps = (window_msgs * COAP_MSG_BYTES * 8) / window_secs
        radio_bps = (window_msgs * RADIO_FRAME_BYTES * 8) / window_secs
        msg_rate = window_msgs / window_secs
        
        times.append((t - min_ts) / 1000 / 60)
        coap_bps_list.append(coap_bps)
        radio_bps_list.append(radio_bps)
        msg_rate_list.append(msg_rate)
        t += step_ms
    
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    
    ax1.plot(times, [r / 1000 for r in radio_bps_list], color="#F44336", linewidth=1.5, label="802.15.4 (radio)")
    ax1.plot(times, [c / 1000 for c in coap_bps_list], color="#2196F3", linewidth=1.5, label="CoAP")
    ax1.set_ylabel("Data rate (kbit/s)")
    ax1.set_title("Tasa de datos instantanea (ventana deslizante 30s) — Escenario 10s")
    ax1.legend()
    
    ax2.plot(times, msg_rate_list, color="#4CAF50", linewidth=1.5)
    avg_rate = statistics.mean(msg_rate_list) if msg_rate_list else 0
    ax2.axhline(y=avg_rate, color="red", linestyle="--", linewidth=1, label=f"Promedio: {avg_rate:.2f} msgs/s")
    ax2.set_xlabel("Tiempo (minutos)")
    ax2.set_ylabel("Mensajes/segundo")
    ax2.set_title("Tasa de mensajes instantanea")
    ax2.legend()
    
    plt.tight_layout()
    path = os.path.join(outdir, f"08_data_rate_timeline.{fmt}")
    plt.savefig(path, bbox_inches="tight")
    plt.close()
    print(f"  -> {path}")


def fig09_jitter_analysis(results, raw_samples, outdir, fmt):
    """Jitter (deviation from expected pmax) per key."""
    keys_data = {}
    for key in METER_KEYS:
        key_samples = sorted([s for s in raw_samples if s["key"] == key], key=lambda x: x["ts"])
        if len(key_samples) < 3:
            continue
        iats = [(key_samples[i]["ts"] - key_samples[i-1]["ts"]) / 1000.0 for i in range(1, len(key_samples))]
        jitter = [iat - PMAX for iat in iats]
        keys_data[key] = jitter
    
    if not keys_data:
        return
    
    fig, ax = plt.subplots(figsize=(14, 6))
    
    positions = []
    labels = []
    for i, (key, jitter) in enumerate(keys_data.items()):
        x = [i] * len(jitter)
        ax.scatter(x, jitter, alpha=0.5, s=20, zorder=3)
        positions.append(i)
        labels.append(key.replace("total", "t.").replace("Energy", "Ener").replace("Power", "Pwr"))
    
    ax.axhline(y=0, color="red", linestyle="--", linewidth=1.5, label="Ideal (0 jitter)")
    ax.set_xticks(positions)
    ax.set_xticklabels(labels, rotation=45, ha="right")
    ax.set_ylabel("Jitter (s) — desviacion de pmax=10s")
    ax.set_title("Analisis de Jitter por recurso — Escenario 10s")
    ax.legend()
    
    plt.tight_layout()
    path = os.path.join(outdir, f"09_jitter_per_key.{fmt}")
    plt.savefig(path, bbox_inches="tight")
    plt.close()
    print(f"  -> {path}")


def fig10_docker_resources(docker_timeline, outdir, fmt):
    """CPU and memory usage of Edge containers over time."""
    if not docker_timeline:
        print("  -> (skipped, no docker stats)")
        return
    
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
    
    # Extract per-container data
    containers = defaultdict(lambda: {"times": [], "cpu": [], "mem_pct": []})
    for snapshot in docker_timeline:
        t = snapshot["elapsed_min"]
        for cstat in snapshot.get("stats", []):
            name = cstat["name"]
            try:
                cpu = float(cstat["cpu_pct"])
                mem = float(cstat["mem_pct"])
            except (ValueError, KeyError):
                continue
            containers[name]["times"].append(t)
            containers[name]["cpu"].append(cpu)
            containers[name]["mem_pct"].append(mem)
    
    colors_map = {"tb-edge": "#2196F3", "tb-edge-postgres": "#FF9800"}
    for name, data in containers.items():
        color = colors_map.get(name, "#999999")
        ax1.plot(data["times"], data["cpu"], label=name, color=color, linewidth=1.5)
        ax2.plot(data["times"], data["mem_pct"], label=name, color=color, linewidth=1.5)
    
    ax1.set_ylabel("CPU (%)")
    ax1.set_title("Carga del sistema Edge (RPi4) — Durante escenario 10s")
    ax1.legend()
    
    ax2.set_xlabel("Tiempo (minutos)")
    ax2.set_ylabel("Memoria (%)")
    ax2.legend()
    
    plt.tight_layout()
    path = os.path.join(outdir, f"10_docker_resources.{fmt}")
    plt.savefig(path, bbox_inches="tight")
    plt.close()
    print(f"  -> {path}")


def fig11_summary_table(results, docker_timeline, outdir, fmt):
    """Summary metrics table as image for thesis."""
    fig, ax = plt.subplots(figsize=(12, 8))
    ax.axis("off")
    
    agg = results["aggregate"]
    proto = results["protocol_overhead"]
    net = results["network_utilization"]
    cfg = results["config"]
    
    # Build table data
    table_data = [
        ["Parametro", "Valor", "Unidad"],
        ["Duracion de prueba", f"{cfg['duration_s']}", "segundos"],
        ["Intervalo observe (pmin/pmax)", f"{cfg['pmin']}/{cfg['pmax']}", "segundos"],
        ["Intervalo DLMS polling", f"{cfg['dlms_poll_s']}", "segundos"],
        ["", "", ""],
        ["Total mensajes recibidos", f"{agg['total_messages']}", "mensajes"],
        ["Llaves activas", f"{agg['keys_reporting']}/{agg['keys_expected']}", ""],
        ["Throughput promedio", f"{agg['throughput_mps']:.4f}", "msgs/s"],
        ["IAT promedio global", f"{agg['iat_global_avg']:.2f}" if agg['iat_global_avg'] else "-", "segundos"],
        ["IAT mediana global", f"{agg['iat_global_median']:.2f}" if agg['iat_global_median'] else "-", "segundos"],
        ["IAT p95 global", f"{agg['iat_global_p95']:.2f}" if agg['iat_global_p95'] else "-", "segundos"],
        ["", "", ""],
        ["Tamano mensaje CoAP", f"{proto['coap_msg_bytes']}", "bytes"],
        ["Tamano trama 802.15.4", f"{proto['radio_frame_bytes']}", "bytes"],
        ["Volumen total CoAP", f"{proto['total_coap_kb']:.2f}", "KB"],
        ["Volumen total radio", f"{proto['total_radio_kb']:.2f}", "KB"],
        ["Data rate CoAP", f"{proto['coap_kbps']:.3f}", "kbit/s"],
        ["Data rate radio", f"{proto['radio_kbps']:.3f}", "kbit/s"],
        ["Eficiencia payload", f"{proto['payload_efficiency_pct']:.1f}", "%"],
        ["", "", ""],
        ["Proyeccion/hora — msgs", f"{proto['msgs_per_hour']}", "msgs/h"],
        ["Proyeccion/hora — radio", f"{proto['radio_bytes_per_hour'] / 1024:.1f}", "KB/h"],
        ["", "", ""],
        ["Utilizacion canal 802.15.4", f"{net['utilization_pct']:.4f}", "%"],
        ["Nodos max concurrentes est.", f"{net['max_concurrent_nodes_est']}", "nodos"],
    ]
    
    # Add docker stats if available
    if docker_timeline:
        tb_cpus = []
        tb_mems = []
        for snap in docker_timeline:
            for cs in snap.get("stats", []):
                if cs["name"] == "tb-edge":
                    try:
                        tb_cpus.append(float(cs["cpu_pct"]))
                        tb_mems.append(float(cs["mem_pct"]))
                    except ValueError:
                        pass
        if tb_cpus:
            table_data.append(["", "", ""])
            table_data.append(["CPU Edge avg (tb-edge)", f"{statistics.mean(tb_cpus):.1f}", "%"])
            table_data.append(["CPU Edge max (tb-edge)", f"{max(tb_cpus):.1f}", "%"])
            table_data.append(["Memoria Edge avg", f"{statistics.mean(tb_mems):.1f}", "%"])
            table_data.append(["Memoria Edge max", f"{max(tb_mems):.1f}", "%"])
    
    table = ax.table(
        cellText=table_data,
        cellLoc="center",
        loc="center",
        colWidths=[0.45, 0.25, 0.20],
    )
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1, 1.4)
    
    # Style header row
    for j in range(3):
        table[0, j].set_facecolor("#2196F3")
        table[0, j].set_text_props(color="white", fontweight="bold")
    
    # Style separator rows (empty)
    for i, row in enumerate(table_data):
        if row[0] == "" and row[1] == "":
            for j in range(3):
                table[i, j].set_facecolor("#F5F5F5")
                table[i, j].set_edgecolor("#F5F5F5")
    
    ax.set_title("Resumen de metricas — Escenario LwM2M Observe 10s", fontsize=14, fontweight="bold", pad=20)
    
    plt.tight_layout()
    path = os.path.join(outdir, f"11_summary_table.{fmt}")
    plt.savefig(path, bbox_inches="tight")
    plt.close()
    print(f"  -> {path}")


def fig12_system_resources(system_timeline, outdir, fmt="png"):
    """Fig 12: Edge RPi4 system resources (CPU load, memory, temp, network I/O)."""
    if not system_timeline or len(system_timeline) < 2:
        print("  [12] Sin datos de sistema — saltando")
        return
    
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    
    elapsed = [s.get("elapsed_s", 0) / 60 for s in system_timeline]  # minutes
    
    # --- (a) CPU Load Averages ---
    ax = axes[0, 0]
    load_1 = [s.get("load_1m", 0) for s in system_timeline]
    load_5 = [s.get("load_5m", 0) for s in system_timeline]
    load_15 = [s.get("load_15m", 0) for s in system_timeline]
    ax.plot(elapsed, load_1, "o-", color="#E53935", markersize=4, label="1 min")
    ax.plot(elapsed, load_5, "s-", color="#FB8C00", markersize=4, label="5 min")
    ax.plot(elapsed, load_15, "^-", color="#43A047", markersize=4, label="15 min")
    ax.axhline(y=4, color="gray", linestyle="--", alpha=0.5, label="RPi4 cores=4")
    ax.set_xlabel("Tiempo (min)")
    ax.set_ylabel("CPU Load Average")
    ax.set_title("(a) CPU Load Average", fontweight="bold")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)
    
    # --- (b) Memory Usage ---
    ax = axes[0, 1]
    mem_used = [s.get("mem_used_pct", 0) for s in system_timeline]
    ax.fill_between(elapsed, mem_used, alpha=0.3, color="#1976D2")
    ax.plot(elapsed, mem_used, "o-", color="#1976D2", markersize=4)
    ax.set_xlabel("Tiempo (min)")
    ax.set_ylabel("Memoria usada (%)")
    ax.set_title("(b) Uso de Memoria RAM", fontweight="bold")
    ax.set_ylim(0, 100)
    ax.grid(True, alpha=0.3)
    # Annotate average
    if mem_used:
        avg_mem = sum(mem_used) / len(mem_used)
        ax.axhline(y=avg_mem, color="#0D47A1", linestyle="--", alpha=0.6)
        ax.text(elapsed[-1] * 0.7, avg_mem + 2, f"Promedio: {avg_mem:.1f}%",
                fontsize=9, color="#0D47A1")
    
    # --- (c) CPU Temperature ---
    ax = axes[1, 0]
    cpu_temp = [s.get("cpu_temp_c", None) for s in system_timeline]
    has_temp = any(t is not None for t in cpu_temp)
    if has_temp:
        temps = [t if t is not None else 0 for t in cpu_temp]
        ax.plot(elapsed, temps, "o-", color="#E53935", markersize=4)
        ax.fill_between(elapsed, temps, alpha=0.2, color="#E53935")
        ax.set_ylabel("Temperatura (°C)")
        ax.set_title("(c) Temperatura CPU", fontweight="bold")
        if max(temps) > 70:
            ax.axhline(y=80, color="red", linestyle="--", alpha=0.5, label="Throttle (80°C)")
            ax.legend(fontsize=8)
    else:
        ax.text(0.5, 0.5, "Sensor no disponible", ha="center", va="center",
                fontsize=12, transform=ax.transAxes, color="gray")
        ax.set_title("(c) Temperatura CPU", fontweight="bold")
    ax.set_xlabel("Tiempo (min)")
    ax.grid(True, alpha=0.3)
    
    # --- (d) Network I/O (delta from first sample) ---
    ax = axes[1, 1]
    rx0 = system_timeline[0].get("net_rx_bytes", 0)
    tx0 = system_timeline[0].get("net_tx_bytes", 0)
    net_rx_kb = [(s.get("net_rx_bytes", rx0) - rx0) / 1024 for s in system_timeline]
    net_tx_kb = [(s.get("net_tx_bytes", tx0) - tx0) / 1024 for s in system_timeline]
    ax.plot(elapsed, net_rx_kb, "o-", color="#1976D2", markersize=4, label="eth0 RX")
    ax.plot(elapsed, net_tx_kb, "s-", color="#43A047", markersize=4, label="eth0 TX")
    
    # wpan0 (Thread 802.15.4) if available
    wpan_rx0 = system_timeline[0].get("wpan_rx_bytes", 0)
    wpan_tx0 = system_timeline[0].get("wpan_tx_bytes", 0)
    if wpan_rx0 or any(s.get("wpan_rx_bytes") for s in system_timeline):
        wpan_rx_kb = [(s.get("wpan_rx_bytes", wpan_rx0) - wpan_rx0) / 1024 for s in system_timeline]
        wpan_tx_kb = [(s.get("wpan_tx_bytes", wpan_tx0) - wpan_tx0) / 1024 for s in system_timeline]
        ax.plot(elapsed, wpan_rx_kb, "^--", color="#7B1FA2", markersize=4, label="wpan0 RX (Thread)")
        ax.plot(elapsed, wpan_tx_kb, "v--", color="#F57C00", markersize=4, label="wpan0 TX (Thread)")
    
    ax.set_xlabel("Tiempo (min)")
    ax.set_ylabel("Datos acumulados (KB)")
    ax.set_title("(d) Trafico de Red del Edge", fontweight="bold")
    ax.legend(fontsize=7, loc="upper left")
    ax.grid(True, alpha=0.3)
    # Annotate total
    if net_rx_kb and net_tx_kb:
        total_rx = net_rx_kb[-1]
        total_tx = net_tx_kb[-1]
        iface = system_timeline[0].get("net_iface", "eth0")
        ax.text(0.05, 0.95, f"{iface}: RX={total_rx:.1f}KB TX={total_tx:.1f}KB",
                transform=ax.transAxes, fontsize=8, va="top",
                bbox=dict(boxstyle="round", facecolor="white", alpha=0.8))
    
    fig.suptitle("Carga del Sistema Edge (RPi4) — Escenario LwM2M Observe 10s",
                 fontsize=14, fontweight="bold", y=1.02)
    plt.tight_layout()
    path = os.path.join(outdir, f"12_system_resources.{fmt}")
    plt.savefig(path, bbox_inches="tight")
    plt.close()
    print(f"  -> {path}")


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════

def progress_bar(current, total, label="", width=20):
    pct = current / total if total > 0 else 0
    filled = int(width * pct)
    bar = "#" * filled + "." * (width - filled)
    print(f"\r        [{bar}] {current}/{total}s ({pct*100:.0f}%) {label}", end="", flush=True)


def run_collection(token, duration_sec, warmup_sec, docker_interval=30):
    """Run the 10s scenario collection with docker stats sampling."""
    
    print(f"\n  [1/5] Configurando perfil -> pmin={PMIN}, pmax={PMAX}...")
    set_profile_10s(token)
    print(f"        Perfil actualizado.")
    
    print(f"\n  [2/5] Warmup: esperando {warmup_sec}s para estabilizacion...")
    for i in range(warmup_sec):
        progress_bar(i + 1, warmup_sec)
        time.sleep(1)
    print()
    
    print(f"\n  [3/5] Recopilando telemetria durante {duration_sec}s...")
    start_dt = datetime.now(timezone.utc)
    start_ts = int(start_dt.timestamp() * 1000)
    print(f"        Inicio: {start_dt.isoformat()}")
    
    # Collect docker + system stats periodically during data collection
    docker_timeline = []
    system_timeline = []
    collection_start = time.time()
    last_stats_check = 0
    
    # Capture initial network counters for delta computation
    initial_sys = collect_system_stats_snapshot()
    if initial_sys:
        initial_sys["elapsed_s"] = 0.0
        system_timeline.append(initial_sys)
    
    for i in range(duration_sec):
        elapsed = time.time() - collection_start
        progress_bar(i + 1, duration_sec)
        
        # Stats snapshot every docker_interval seconds
        if elapsed - last_stats_check >= docker_interval:
            stats = collect_docker_stats_snapshot()
            if stats:
                docker_timeline.append({
                    "elapsed_s": round(elapsed, 1),
                    "elapsed_min": round(elapsed / 60, 2),
                    "stats": stats,
                })
            sys_stats = collect_system_stats_snapshot()
            if sys_stats:
                sys_stats["elapsed_s"] = round(elapsed, 1)
                system_timeline.append(sys_stats)
            last_stats_check = elapsed
        
        time.sleep(1)
    print()
    
    end_dt = datetime.now(timezone.utc)
    end_ts = int(end_dt.timestamp() * 1000)
    print(f"        Fin:    {end_dt.isoformat()}")
    
    print(f"\n  [4/5] Consultando TB Edge API...")
    telemetry = get_telemetry(token, start_ts, end_ts)
    
    total_msgs = sum(len(v) for v in telemetry.values())
    keys_reporting = sum(1 for v in telemetry.values() if len(v) > 0)
    print(f"        Recibidos: {total_msgs} muestras en {keys_reporting}/{len(TELEMETRY_KEYS)} llaves")
    
    print(f"\n  [5/5] Analizando datos...")
    results = analyze_telemetry(telemetry, start_ts, end_ts, duration_sec)
    
    # Flatten raw samples for graphs
    raw_samples = []
    for key in TELEMETRY_KEYS:
        for e in telemetry.get(key, []):
            raw_samples.append({"ts": int(e["ts"]), "key": key, "value": float(e["value"])})
    raw_samples.sort(key=lambda x: x["ts"])
    
    return results, raw_samples, docker_timeline, system_timeline, start_ts, end_ts


def save_results(results, raw_samples, docker_timeline, system_timeline, outdir):
    """Save all data to files."""
    os.makedirs(outdir, exist_ok=True)
    
    # Summary JSON
    with open(os.path.join(outdir, "analysis_10s.json"), "w") as f:
        json.dump(results, f, indent=2, default=str)
    
    # Raw timeseries CSV
    with open(os.path.join(outdir, "raw_ts_10s.csv"), "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp_ms", "datetime_utc", "key", "value"])
        for s in raw_samples:
            dt = datetime.fromtimestamp(s["ts"] / 1000, tz=timezone.utc).isoformat()
            writer.writerow([s["ts"], dt, s["key"], s["value"]])
    
    # Per-key CSV
    with open(os.path.join(outdir, "per_key_10s.csv"), "w", newline="") as f:
        writer = csv.writer(f)
        fields = ["key", "samples", "expected", "completeness_pct", "throughput_mps",
                  "coap_bytes", "radio_bytes", "iat_avg", "iat_median", "iat_stddev",
                  "iat_p95", "jitter_avg", "val_avg", "val_stddev"]
        writer.writerow(fields)
        for key in METER_KEYS + RADIO_KEYS:
            pk = results["per_key"].get(key, {})
            writer.writerow([key] + [pk.get(f, "") for f in fields[1:]])
    
    # Docker timeline CSV
    if docker_timeline:
        with open(os.path.join(outdir, "docker_stats.csv"), "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["elapsed_s", "container", "cpu_pct", "mem_pct", "mem_usage", "net_io"])
            for snap in docker_timeline:
                for cs in snap.get("stats", []):
                    writer.writerow([
                        snap["elapsed_s"], cs["name"],
                        cs.get("cpu_pct", ""), cs.get("mem_pct", ""),
                        cs.get("mem_usage", ""), cs.get("net_io", ""),
                    ])
    
    # System stats timeline CSV
    if system_timeline:
        with open(os.path.join(outdir, "system_stats.csv"), "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["elapsed_s", "load_1m", "load_5m", "load_15m",
                             "mem_used_mb", "mem_avail_mb", "mem_used_pct",
                             "cpu_temp_c", "net_rx_bytes", "net_tx_bytes",
                             "wpan_rx_bytes", "wpan_tx_bytes"])
            for snap in system_timeline:
                writer.writerow([
                    snap.get("elapsed_s", ""),
                    snap.get("load_1m", ""), snap.get("load_5m", ""), snap.get("load_15m", ""),
                    snap.get("mem_used_mb", ""), snap.get("mem_avail_mb", ""), snap.get("mem_used_pct", ""),
                    snap.get("cpu_temp_c", ""),
                    snap.get("net_rx_bytes", ""), snap.get("net_tx_bytes", ""),
                    snap.get("wpan_rx_bytes", ""), snap.get("wpan_tx_bytes", ""),
                ])
    
    # Thesis-ready text summary
    agg = results["aggregate"]
    proto = results["protocol_overhead"]
    net = results["network_utilization"]
    cfg = results["config"]
    
    with open(os.path.join(outdir, "thesis_summary_10s.txt"), "w") as f:
        f.write("=" * 70 + "\n")
        f.write("ANALISIS PROFUNDO — ESCENARIO LwM2M OBSERVE 10s\n")
        f.write(f"Tesis: Tesis_jsgiraldod_2026_rev_final\n")
        f.write(f"Generado: {datetime.now(timezone.utc).isoformat()}\n")
        f.write("=" * 70 + "\n\n")
        
        f.write(f"Configuracion:\n")
        f.write(f"  pmin/pmax: {cfg['pmin']}/{cfg['pmax']}s\n")
        f.write(f"  DLMS poll: {cfg['dlms_poll_s']}s\n")
        f.write(f"  Duracion:  {cfg['duration_s']}s\n")
        f.write(f"  Keys:      {cfg['num_keys']}\n\n")
        
        f.write(f"Metricas Agregadas:\n")
        f.write(f"  Total mensajes:      {agg['total_messages']}\n")
        f.write(f"  Llaves activas:      {agg['keys_reporting']}/{agg['keys_expected']}\n")
        f.write(f"  Throughput:          {agg['throughput_mps']:.4f} msgs/s\n")
        f.write(f"  IAT promedio:        {agg['iat_global_avg']:.2f}s\n" if agg['iat_global_avg'] else "")
        f.write(f"  IAT mediana:         {agg['iat_global_median']:.2f}s\n" if agg['iat_global_median'] else "")
        f.write(f"  IAT p95:             {agg['iat_global_p95']:.2f}s\n" if agg['iat_global_p95'] else "")
        f.write(f"\n")
        
        f.write(f"Volumen de Datos:\n")
        f.write(f"  CoAP total:          {proto['total_coap_kb']:.2f} KB\n")
        f.write(f"  Radio (802.15.4):    {proto['total_radio_kb']:.2f} KB\n")
        f.write(f"  Data rate CoAP:      {proto['coap_kbps']:.3f} kbit/s\n")
        f.write(f"  Data rate radio:     {proto['radio_kbps']:.3f} kbit/s\n")
        f.write(f"  Eficiencia payload:  {proto['payload_efficiency_pct']:.1f}%\n\n")
        
        f.write(f"Proyeccion Horaria:\n")
        f.write(f"  Mensajes/hora:       {proto['msgs_per_hour']}\n")
        f.write(f"  Radio KB/hora:       {proto['radio_bytes_per_hour'] / 1024:.1f}\n\n")
        
        f.write(f"Utilizacion de Red:\n")
        f.write(f"  Canal 802.15.4:      {net['utilization_pct']:.4f}%\n")
        f.write(f"  Nodos max estimados: {net['max_concurrent_nodes_est']}\n\n")
        
        f.write(f"Per-Key IAT:\n")
        f.write(f"  {'Key':<22} {'N':>5} {'IAT avg':>8} {'IAT p95':>8} {'Jitter':>8} {'Compl%':>8}\n")
        f.write(f"  {'-'*60}\n")
        for key in METER_KEYS:
            pk = results["per_key"].get(key, {})
            n = pk.get("samples", 0)
            iat = pk.get("iat_avg", "")
            p95 = pk.get("iat_p95", "")
            jit = pk.get("jitter_avg", "")
            comp = pk.get("completeness_pct", 0)
            f.write(f"  {key:<22} {n:>5} {iat if iat else '-':>8} {p95 if p95 else '-':>8} {jit if jit else '-':>8} {comp:>7.1f}%\n")
        
        # LaTeX table
        f.write(f"\n{'='*70}\n")
        f.write(f"TABLA LATEX\n")
        f.write(f"{'='*70}\n\n")
        f.write(r"\begin{table}[htbp]" + "\n")
        f.write(r"\centering" + "\n")
        f.write(r"\caption{Metricas de rendimiento — Escenario LwM2M Observe 10s}" + "\n")
        f.write(r"\label{tab:lwm2m-10s-deep}" + "\n")
        f.write(r"\begin{tabular}{lrr}" + "\n")
        f.write(r"\toprule" + "\n")
        f.write(r"\textbf{Metrica} & \textbf{Valor} & \textbf{Unidad} \\" + "\n")
        f.write(r"\midrule" + "\n")
        f.write(f"Total mensajes & {agg['total_messages']} & msgs \\\\\n")
        f.write(f"Throughput & {agg['throughput_mps']:.4f} & msgs/s \\\\\n")
        if agg['iat_global_avg']:
            f.write(f"IAT promedio & {agg['iat_global_avg']:.2f} & s \\\\\n")
        if agg['iat_global_p95']:
            f.write(f"IAT p95 & {agg['iat_global_p95']:.2f} & s \\\\\n")
        f.write(f"Vol. CoAP & {proto['total_coap_kb']:.2f} & KB \\\\\n")
        f.write(f"Vol. radio & {proto['total_radio_kb']:.2f} & KB \\\\\n")
        f.write(f"Data rate radio & {proto['radio_kbps']:.3f} & kbit/s \\\\\n")
        f.write(f"Eficiencia payload & {proto['payload_efficiency_pct']:.1f} & \\% \\\\\n")
        f.write(f"Utilizacion canal & {net['utilization_pct']:.4f} & \\% \\\\\n")
        f.write(f"Nodos max est. & {net['max_concurrent_nodes_est']} & nodos \\\\\n")
        f.write(r"\bottomrule" + "\n")
        f.write(r"\end{tabular}" + "\n")
        f.write(r"\end{table}" + "\n")


def generate_all_graphs(results, raw_samples, docker_timeline, system_timeline, outdir, fmt="png"):
    """Generate all thesis graphs."""
    if not HAS_MPL:
        print("  ERROR: matplotlib required for graphs")
        return
    
    setup_style()
    
    print(f"\n  Generando graficos ({fmt})...")
    fig01_message_rate_timeline(results, raw_samples, outdir, fmt)
    fig02_iat_distribution(results, raw_samples, outdir, fmt)
    fig03_per_key_iat_boxplot(results, raw_samples, outdir, fmt)
    fig04_cumulative_data_volume(results, raw_samples, outdir, fmt)
    fig05_protocol_overhead_breakdown(results, outdir, fmt)
    fig06_network_utilization(results, outdir, fmt)
    fig07_completeness_heatmap(results, outdir, fmt)
    fig08_data_rate_over_time(results, raw_samples, outdir, fmt)
    fig09_jitter_analysis(results, raw_samples, outdir, fmt)
    fig10_docker_resources(docker_timeline, outdir, fmt)
    fig11_summary_table(results, docker_timeline, outdir, fmt)
    fig12_system_resources(system_timeline, outdir, fmt)


def load_existing(analyze_dir):
    """Load results from a previous run for re-graphing."""
    analysis_path = os.path.join(analyze_dir, "analysis_10s.json")
    raw_path = os.path.join(analyze_dir, "raw_ts_10s.csv")
    docker_path = os.path.join(analyze_dir, "docker_stats.csv")
    system_path = os.path.join(analyze_dir, "system_stats.csv")
    
    with open(analysis_path) as f:
        results = json.load(f)
    
    raw_samples = []
    with open(raw_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw_samples.append({
                "ts": int(row["timestamp_ms"]),
                "key": row["key"],
                "value": float(row["value"]),
            })
    
    docker_timeline = []
    if os.path.exists(docker_path):
        # Reconstruct timeline from CSV
        snap_map = defaultdict(list)
        with open(docker_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                t = float(row["elapsed_s"])
                snap_map[t].append(row)
        for t in sorted(snap_map.keys()):
            docker_timeline.append({
                "elapsed_s": t,
                "elapsed_min": round(t / 60, 2),
                "stats": [{"name": r["container"], "cpu_pct": r["cpu_pct"],
                           "mem_pct": r["mem_pct"], "mem_usage": r.get("mem_usage", ""),
                           "net_io": r.get("net_io", "")} for r in snap_map[t]],
            })
    
    system_timeline = []
    if os.path.exists(system_path):
        with open(system_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                snap = {"elapsed_s": float(row["elapsed_s"])}
                for k in ["load_1m", "load_5m", "load_15m", "mem_used_mb",
                           "mem_avail_mb", "mem_used_pct", "cpu_temp_c"]:
                    if row.get(k):
                        snap[k] = float(row[k])
                for k in ["net_rx_bytes", "net_tx_bytes"]:
                    if row.get(k):
                        snap[k] = int(row[k])
                system_timeline.append(snap)
    
    return results, raw_samples, docker_timeline, system_timeline


def main():
    parser = argparse.ArgumentParser(description="Deep 10s LwM2M observe benchmark")
    parser.add_argument("--duration", type=int, default=600, help="Collection duration in seconds (default: 600)")
    parser.add_argument("--warmup", type=int, default=90, help="Warmup period in seconds (default: 90)")
    parser.add_argument("--format", default="png", choices=["png", "pdf", "svg"], help="Graph format")
    parser.add_argument("--analyze-only", metavar="DIR", help="Re-generate graphs from existing data directory")
    parser.add_argument("--no-restore", action="store_true", help="Don't restore baseline profile after test")
    args = parser.parse_args()
    
    print()
    print("=" * 60)
    print("  DEEP ANALYSIS — LwM2M OBSERVE 10s")
    print("  Tesis_jsgiraldod_2026_rev_final")
    print("=" * 60)
    
    if args.analyze_only:
        print(f"\n  Mode: Re-analyze existing data")
        print(f"  Dir:  {args.analyze_only}")
        results, raw_samples, docker_timeline, system_timeline = load_existing(args.analyze_only)
        generate_all_graphs(results, raw_samples, docker_timeline, system_timeline, args.analyze_only, args.format)
        print(f"\n  {'='*50}")
        print(f"  Graficos regenerados en {args.analyze_only}")
        print(f"  {'='*50}\n")
        return
    
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = os.path.join(os.path.dirname(__file__), "..", "results", "benchmark_10s", ts)
    
    print(f"\n  Config:")
    print(f"    pmin/pmax:  {PMIN}/{PMAX}s")
    print(f"    Duration:   {args.duration}s ({args.duration//60} min)")
    print(f"    Warmup:     {args.warmup}s")
    print(f"    DLMS poll:  {DLMS_POLL_INTERVAL}s")
    print(f"    Keys:       {len(TELEMETRY_KEYS)}")
    print(f"    Edge SSH:   {EDGE_SSH_USER}@{EDGE_SSH_HOST}")
    print(f"    Output:     {outdir}")
    print(f"    Format:     {args.format}")
    est_min = (args.warmup + args.duration) / 60
    print(f"    Est. time:  {est_min:.0f} min")
    
    print(f"\n  Autenticando...")
    token = login()
    print(f"  OK")
    
    try:
        results, raw_samples, docker_timeline, system_timeline, start_ts, end_ts = \
            run_collection(token, args.duration, args.warmup)
        
        # Save data
        save_results(results, raw_samples, docker_timeline, system_timeline, outdir)
        
        # Quick summary
        agg = results["aggregate"]
        proto = results["protocol_overhead"]
        net = results["network_utilization"]
        
        print(f"\n  {'='*50}")
        print(f"  RESUMEN RAPIDO")
        print(f"  {'='*50}")
        print(f"    Total mensajes:    {agg['total_messages']}")
        print(f"    Throughput:        {agg['throughput_mps']:.4f} msgs/s")
        print(f"    IAT promedio:      {agg['iat_global_avg']}s")
        print(f"    CoAP total:        {proto['total_coap_kb']:.2f} KB")
        print(f"    Radio total:       {proto['total_radio_kb']:.2f} KB")
        print(f"    Data rate radio:   {proto['radio_kbps']:.3f} kbit/s")
        print(f"    Canal utiliz:      {net['utilization_pct']:.4f}%")
        print(f"    Max nodos est.:    {net['max_concurrent_nodes_est']}")
        
        # Generate graphs
        generate_all_graphs(results, raw_samples, docker_timeline, system_timeline, outdir, args.format)
        
        print(f"\n  {'='*50}")
        print(f"  Resultados guardados en:")
        print(f"    {outdir}")
        print(f"  {'='*50}\n")
        
    finally:
        if not args.no_restore:
            print(f"\n  Restaurando perfil baseline...")
            try:
                restore_baseline(token)
                print(f"  Perfil restaurado.")
            except Exception as e:
                print(f"  ERROR restaurando: {e}")
        # Close SSH connection
        global _ssh_client
        if _ssh_client:
            try:
                _ssh_client.close()
            except Exception:
                pass
            _ssh_client = None


if __name__ == "__main__":
    main()
