#!/usr/bin/env python3
"""
AMI LwM2M Node — QA Checklist & Data-Quality Validator
=======================================================
Runs automated checks against:
 1. Unit test suite       - compiles & runs all 111 tests (GCC native)
 2. ThingsBoard Edge      - device status, LwM2M connectivity
 3. Telemetry quality     - zero spikes, gaps, ranges, energy monotonicity
 4. Edge server health    - Docker containers, OTBR/Thread status (SSH)

Architecture: ESP32-C6 → LwM2M/CoAP → TB Edge (built-in LwM2M transport)
ThingsBoard Edge IS the LwM2M server (built-in transport on ports 5683/5684 UDP).

Usage:
    python tools/qa_checklist.py [--skip-build] [--skip-tb] [--skip-ssh]
"""

import json
import time
import sys
import os
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timedelta

# ── Configuration ────────────────────────────────────────────────────
TB_URL         = "http://192.168.1.111:8090"
TB_USER        = "tenant@thingsboard.org"
TB_PASS        = "tenant"
TB_DEVICE_NAME = "ami-esp32c6-2434"

# Edge server SSH (OpenWrt RPi4)
EDGE_HOST      = "192.168.1.111"
EDGE_SSH_USER  = "root"
EDGE_SSH_PASS  = "root"

# Valid ranges for residential AMI meter (Microstar single-phase 120V/60Hz)
RANGES = {
    "voltage_r":           (100.0, 140.0),   # V RMS (120V ± ~15%)
    "current_r":           (0.0,   100.0),   # A (0 is valid — no load)
    "active_power_r":      (-15.0, 15.0),    # kW
    "reactive_power_r":    (-15.0, 15.0),    # kvar
    "apparent_power_r":    (0.0,   15.0),    # kVA (always ≥ 0)
    "power_factor_r":      (-1.0,  1.0),     # dimensionless
    "total_active_power":  (-50.0, 50.0),    # kW
    "total_reactive_power":(-50.0, 50.0),    # kvar
    "total_apparent_power": (0.0,  50.0),    # kVA
    "total_power_factor":  (-1.0,  1.0),
    "active_energy":       (0.0,   999999.0),# kWh (monotonic)
    "reactive_energy":     (0.0,   999999.0),# kvarh
    "apparent_energy":     (0.0,   999999.0),# kVAh
    "frequency":           (59.0,  61.0),    # Hz (60 Hz grid)
    "neutral_current":     (0.0,   100.0),   # A
}

# Fields that should NEVER be exactly 0.0 on a live meter
NEVER_ZERO = ["voltage_r", "frequency", "active_energy"]

# LwM2M Object 10242 resource map (RID → telemetry key)
PM_RESOURCES = {
    4:  "voltage_r",
    5:  "current_r",
    6:  "active_power_r",
    7:  "reactive_power_r",
    8:  "apparent_power_r",
    9:  "power_factor_r",
    22: "total_active_power",
    23: "total_reactive_power",
    24: "total_apparent_power",
    25: "total_power_factor",
    26: "active_energy",
    27: "reactive_energy",
    28: "apparent_energy",
    29: "frequency",
    30: "neutral_current",
}

# ── Helpers ──────────────────────────────────────────────────────────
class CheckResult:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.warnings = 0
        self.details = []

    def ok(self, msg):
        self.passed += 1
        self.details.append(("PASS", msg))

    def fail(self, msg):
        self.failed += 1
        self.details.append(("FAIL", msg))

    def warn(self, msg):
        self.warnings += 1
        self.details.append(("WARN", msg))

    def print_section(self, title):
        print(f"\n{'─'*60}")
        print(f"  {title}")
        print(f"{'─'*60}")
        for status, msg in self.details:
            icon = {"PASS": "✓", "FAIL": "✗", "WARN": "⚠"}[status]
            print(f"  {icon} [{status}] {msg}")
        self.details.clear()


def tb_login():
    """Authenticate with ThingsBoard Edge, return JWT token."""
    data = json.dumps({"username": TB_USER, "password": TB_PASS}).encode()
    req = urllib.request.Request(
        f"{TB_URL}/api/auth/login", data=data,
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())["token"]


def tb_get(token, path):
    """GET from ThingsBoard Edge API."""
    req = urllib.request.Request(
        f"{TB_URL}{path}",
        headers={"X-Authorization": f"Bearer {token}"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def tb_find_device(token):
    """Find AMI device by name."""
    devs = tb_get(token, "/api/tenant/devices?pageSize=50&page=0")
    for d in devs["data"]:
        if TB_DEVICE_NAME in d["name"]:
            return d
    return None


def ssh_cmd(cmd, timeout=10):
    """Run a command on the Edge server via SSH (sshpass required)."""
    try:
        r = subprocess.run(
            ["sshpass", "-p", EDGE_SSH_PASS, "ssh",
             "-o", "StrictHostKeyChecking=no",
             "-o", "ConnectTimeout=5",
             f"{EDGE_SSH_USER}@{EDGE_HOST}", cmd],
            capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


# ── Check 1: Unit Tests ─────────────────────────────────────────────
def check_unit_tests(results):
    """Compile and run the native unit test suite."""
    tests_dir = os.path.join(os.path.dirname(__file__), "..", "tests")
    tests_dir = os.path.abspath(tests_dir)

    if not os.path.isdir(tests_dir):
        results.fail(f"Tests directory not found: {tests_dir}")
        return

    # Compile
    compile_cmd = [
        "gcc", "-o", "run_tests.exe",
        "test_main.c", "test_hdlc.c", "test_cosem.c",
        "../src/dlms_hdlc.c", "../src/dlms_cosem.c",
        "-I../src", "-Istubs", "-DUNIT_TEST", "-lm", "-Wall",
    ]
    try:
        r = subprocess.run(compile_cmd, cwd=tests_dir, capture_output=True,
                           text=True, timeout=30)
        if r.returncode != 0:
            results.fail(f"Test compilation failed:\n{r.stderr[:500]}")
            return
        results.ok("Test suite compiled successfully")
    except FileNotFoundError:
        results.fail("GCC not found — cannot compile tests")
        return
    except subprocess.TimeoutExpired:
        results.fail("Test compilation timed out (30s)")
        return

    # Run
    try:
        exe = os.path.join(tests_dir, "run_tests.exe")
        r = subprocess.run([exe], cwd=tests_dir, capture_output=True,
                           text=True, timeout=30)
        output = r.stdout

        # Parse results
        if "ALL" in output and "PASSED" in output:
            # Extract count
            for line in output.splitlines():
                if "TESTS PASSED" in line:
                    results.ok(f"All tests passed: {line.strip()}")
                    break
            else:
                results.ok("All tests passed")
        else:
            # Count failures
            fail_count = output.count("FAIL")
            results.fail(f"Tests had {fail_count} failure(s)")

        # Check individual suites
        for suite in ["HDLC", "COSEM", "DLMS Logic"]:
            for line in output.splitlines():
                if f"Suite {suite}:" in line:
                    if "PASSED" in line:
                        results.ok(f"Suite {suite}: {line.strip().split(':')[-1].strip()}")
                    else:
                        results.fail(f"Suite {suite}: {line.strip()}")
    except Exception as e:
        results.fail(f"Test execution failed: {e}")


# ── Check 2: TB Edge Device & LwM2M Status ───────────────────────────
def check_tb_device(results):
    """Verify device exists, is active, and LwM2M transport is working."""

    try:
        token = tb_login()
        results.ok("ThingsBoard Edge login successful")
    except Exception as e:
        results.fail(f"Cannot login to ThingsBoard Edge: {e}")
        return None, None

    device = tb_find_device(token)
    if not device:
        results.fail(f"Device '{TB_DEVICE_NAME}' not found on TB Edge")
        return token, None
    dev_id = device["id"]["id"]
    results.ok(f"Device found: {device['name']} ({dev_id[:8]}...)")

    # Check device activity
    try:
        dev_info = tb_get(token, f"/api/device/{dev_id}")
        # Last activity time from device credentials or attributes
        additional = dev_info.get("additionalInfo", {})
        last_activity = additional.get("lastActivityTime")
        if last_activity:
            now_ms = int(time.time() * 1000)
            age_sec = (now_ms - last_activity) / 1000
            age_str = (f"{age_sec:.0f}s" if age_sec < 120
                       else f"{age_sec/60:.0f}min" if age_sec < 7200
                       else f"{age_sec/3600:.1f}h")
            if age_sec < 120:
                results.ok(f"Device active: last activity {age_str} ago")
            elif age_sec < 600:
                results.warn(f"Device idle: last activity {age_str} ago")
            else:
                results.fail(f"Device INACTIVE: last activity {age_str} ago")
    except Exception:
        results.warn("Could not check device activity time")

    # Check latest telemetry snapshot (no time range = latest values)
    try:
        keys_str = ",".join(PM_RESOURCES.values())
        latest = tb_get(token,
            f"/api/plugins/telemetry/DEVICE/{dev_id}/values/timeseries"
            f"?keys={keys_str}")

        readable = 0
        zero_found = []
        out_of_range = []
        now_ms = int(time.time() * 1000)

        for key in PM_RESOURCES.values():
            vals = latest.get(key, [])
            if not vals:
                continue
            fval = float(vals[0]["value"])
            readable += 1

            if key in NEVER_ZERO and fval == 0.0:
                zero_found.append(key)

            if key in RANGES:
                lo, hi = RANGES[key]
                if not (lo <= fval <= hi):
                    out_of_range.append(f"{key}={fval:.3f} ({lo}..{hi})")

        if readable > 0:
            results.ok(f"Telemetry keys with data: {readable}/{len(PM_RESOURCES)}")
        else:
            results.fail("No telemetry data available on device")

        if zero_found:
            results.fail(f"Zero values (should never be 0): {zero_found}")
        elif readable > 0:
            results.ok("No suspicious zeros in critical fields")

        if out_of_range:
            for oor in out_of_range:
                results.warn(f"Out of range: {oor}")
        elif readable > 0:
            results.ok("All latest values within expected ranges")

    except Exception as e:
        results.warn(f"Could not check latest telemetry: {e}")

    return token, dev_id


# ── Check 3: Telemetry Data Quality (historical) ────────────────────
def check_telemetry_quality(results, token, dev_id):
    """Analyze last 24h of telemetry for zero spikes, gaps, and anomalies."""

    if not token or not dev_id:
        results.fail("Skipped — no device connection")
        return

    now_ms = int(time.time() * 1000)
    day_ago_ms = now_ms - 86400_000
    hour_ago_ms = now_ms - 3600_000

    keys_str = ",".join(PM_RESOURCES.values())

    # Last 24h at up to 1000 points per key
    try:
        ts_data = tb_get(token,
            f"/api/plugins/telemetry/DEVICE/{dev_id}/values/timeseries"
            f"?keys={keys_str}&startTs={day_ago_ms}&endTs={now_ms}&limit=1000")
    except Exception as e:
        results.fail(f"Cannot fetch telemetry history: {e}")
        return

    if not ts_data or all(len(v) == 0 for v in ts_data.values()):
        results.fail("No telemetry data in the last 24 hours")
        return

    total_points = 0
    zero_spikes = {}
    stale_keys = []
    energy_decreases = []
    out_of_range_keys = []

    for key, values in ts_data.items():
        if not values:
            stale_keys.append(key)
            continue

        total_points += len(values)

        # Zero spikes in critical fields
        if key in NEVER_ZERO:
            zeros = [v for v in values if float(v.get("value", 1)) == 0.0]
            if zeros:
                zero_spikes[key] = len(zeros)

        # Staleness (most recent data point)
        newest_ts = max(int(v["ts"]) for v in values)
        age_min = (now_ms - newest_ts) / 60_000
        if age_min > 30:
            stale_keys.append(f"{key} ({age_min:.0f}min stale)")

        # Value ranges
        oor_count = 0
        if key in RANGES:
            lo, hi = RANGES[key]
            for v in values:
                fval = float(v.get("value", 0))
                if not (lo <= fval <= hi):
                    oor_count += 1
            if oor_count > 0:
                out_of_range_keys.append(f"{key}: {oor_count} out-of-range")

        # Energy monotonicity
        if key in ("active_energy", "reactive_energy", "apparent_energy"):
            sorted_vals = sorted(values, key=lambda v: int(v["ts"]))
            for i in range(1, len(sorted_vals)):
                prev = float(sorted_vals[i-1]["value"])
                curr = float(sorted_vals[i]["value"])
                if curr < prev - 0.001:
                    energy_decreases.append(f"{key}: {prev:.2f} -> {curr:.2f}")
                    break

    results.ok(f"Total data points (24h): {total_points}")

    # Estimate expected points: 15s poll → ~5760/day per key, but threshold filtering
    active_keys = sum(1 for v in ts_data.values() if len(v) > 0)
    results.ok(f"Active telemetry keys: {active_keys}/{len(PM_RESOURCES)}")

    if zero_spikes:
        for key, count in zero_spikes.items():
            results.fail(f"ZERO SPIKE: {key} had {count} zero value(s)")
    else:
        results.ok("No zero spikes in critical fields")

    if stale_keys:
        for sk in stale_keys[:5]:
            results.warn(f"Stale: {sk}")
    else:
        results.ok("All keys have recent data (<30 min)")

    if out_of_range_keys:
        for oor in out_of_range_keys:
            results.warn(f"Range violation: {oor}")
    else:
        results.ok("All values within expected ranges")

    if energy_decreases:
        for ed in energy_decreases:
            results.fail(f"Energy decreased: {ed}")
    else:
        results.ok("Energy values monotonically increasing")

    # Gap analysis on voltage_r
    voltage_pts = ts_data.get("voltage_r", [])
    if len(voltage_pts) > 1:
        sorted_pts = sorted(voltage_pts, key=lambda x: int(x["ts"]))
        gaps = []
        for i in range(1, len(sorted_pts)):
            gap_s = (int(sorted_pts[i]["ts"]) - int(sorted_pts[i-1]["ts"])) / 1000
            if gap_s > 120:  # gaps > 2 min
                gaps.append(gap_s)
        if gaps:
            results.warn(f"Data gaps >2min: {len(gaps)} (largest: {max(gaps)/60:.1f}min)")
        else:
            results.ok("No significant data gaps in voltage_r")

        vals = [float(v["value"]) for v in voltage_pts]
        avg_v = sum(vals) / len(vals)
        results.ok(f"Voltage stats: min={min(vals):.1f} avg={avg_v:.1f} max={max(vals):.1f} V")


# ── Check 4: Edge Server Health (SSH) ───────────────────────────────
def check_edge_server(results):
    """Check Docker containers and OTBR status on the Edge server via SSH."""

    # Check if sshpass is available
    try:
        subprocess.run(["sshpass", "-V"], capture_output=True, timeout=5)
    except FileNotFoundError:
        results.warn("sshpass not installed — SSH checks use manual method")
        # Fallback: check ports instead
        import socket
        for port, svc in [(8090, "TB Edge HTTP"), (5683, "LwM2M CoAP")]:
            try:
                s = socket.create_connection((EDGE_HOST, port), timeout=3)
                s.close()
                results.ok(f"{svc} (port {port}): reachable")
            except Exception:
                results.fail(f"{svc} (port {port}): NOT reachable")
        return

    # Docker containers
    docker_out = ssh_cmd("docker ps --format '{{.Names}}|{{.Status}}'")
    if docker_out:
        containers = docker_out.strip().split("\n")
        for c in containers:
            parts = c.split("|")
            name = parts[0] if parts else c
            status = parts[1] if len(parts) > 1 else "unknown"
            if "Up" in status:
                results.ok(f"Container {name}: {status}")
            else:
                results.fail(f"Container {name}: {status}")
    else:
        results.warn("Could not list Docker containers via SSH")

    # OTBR / Thread status
    thread_state = ssh_cmd("ot-ctl state 2>/dev/null || echo 'unavailable'")
    if thread_state and thread_state != "unavailable":
        results.ok(f"Thread OTBR state: {thread_state}")
    else:
        results.warn("Could not check OTBR Thread state")

    # System resources
    uptime = ssh_cmd("uptime")
    if uptime:
        results.ok(f"Server uptime: {uptime.strip()}")

    mem = ssh_cmd("free -m 2>/dev/null | grep Mem | awk '{print $3\"/\"$2\" MB (\"int($3/$2*100)\"%)\"}' || echo 'n/a'")
    if mem and mem != "n/a":
        results.ok(f"Memory usage: {mem}")


# ── Main ─────────────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]
    skip_build = "--skip-build" in args
    skip_tb = "--skip-tb" in args
    skip_ssh = "--skip-ssh" in args

    print("=" * 60)
    print("  AMI LwM2M Node — QA Checklist")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  TB Edge: {TB_URL}  |  Device: {TB_DEVICE_NAME}")
    print("=" * 60)

    all_results = []
    token = None
    dev_id = None

    # 1. Unit Tests
    if not skip_build:
        r = CheckResult()
        check_unit_tests(r)
        r.print_section("1. Unit Tests (GCC native)")
        all_results.append(r)
    else:
        print("\n  [SKIPPED] Unit tests (--skip-build)")

    # 2. TB Edge Device & LwM2M Status
    if not skip_tb:
        r = CheckResult()
        token, dev_id = check_tb_device(r)
        r.print_section("2. TB Edge — Device & LwM2M Status")
        all_results.append(r)
    else:
        print("\n  [SKIPPED] TB Edge checks (--skip-tb)")

    # 3. Telemetry Data Quality
    if not skip_tb and token and dev_id:
        r = CheckResult()
        check_telemetry_quality(r, token, dev_id)
        r.print_section("3. Telemetry Data Quality (24h)")
        all_results.append(r)

    # 4. Edge Server Health
    if not skip_ssh:
        r = CheckResult()
        check_edge_server(r)
        r.print_section("4. Edge Server Health")
        all_results.append(r)
    else:
        print("\n  [SKIPPED] SSH checks (--skip-ssh)")

    # Summary
    total_pass = sum(r.passed for r in all_results)
    total_fail = sum(r.failed for r in all_results)
    total_warn = sum(r.warnings for r in all_results)

    print(f"\n{'='*60}")
    print(f"  QA SUMMARY")
    print(f"{'='*60}")
    print(f"  ✓ Passed:   {total_pass}")
    print(f"  ✗ Failed:   {total_fail}")
    print(f"  ⚠ Warnings: {total_warn}")

    if total_fail == 0:
        print(f"\n  ✓ ALL CHECKS PASSED — System operational")
    else:
        print(f"\n  ✗ {total_fail} CHECK(S) FAILED — Review above")

    print(f"{'='*60}\n")
    return 1 if total_fail > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
