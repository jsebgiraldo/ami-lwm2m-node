#!/usr/bin/env python3
"""
AMI Watchdog — Automated Monitoring & Recovery System
=====================================================

Periodically checks the health of the entire AMI pipeline:
  ESP32-C6 → Thread → TB Edge (LwM2M) → Telemetry

Detects failures and attempts automatic recovery.

Usage:
    python ami_watchdog.py                  # Single check
    python ami_watchdog.py --daemon         # Run continuously (every 5 min)
    python ami_watchdog.py --interval 120   # Custom interval (seconds)
    python ami_watchdog.py --dry-run        # Check only, no recovery actions

Author: AMI Tesis Project
Date:   2026-03-02
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import subprocess

# ──────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────
CONFIG = {
    # ThingsBoard Edge
    "tb_edge_url": "http://192.168.1.111:8090",
    "tb_username": "tenant@thingsboard.org",
    "tb_password": "tenant",

    # Device
    "device_id": "cc9da070-135b-11f1-80f9-cdb955f2c365",
    "device_name": "ami-esp32c6-2434",
    "device_profile_id": "b6d55c90-12db-11f1-b535-433a231637c4",

    # Edge SSH (for Docker checks)
    "edge_ssh_host": "root@192.168.1.111",

    # Serial (for node recovery via DTR toggle / reboot)
    "serial_port": "COM12",
    "serial_baud": 115200,

    # Thresholds
    "stale_telemetry_minutes": 10,       # Alert if no data for this long
    "critical_telemetry_minutes": 20,    # Attempt recovery after this long
    "max_recovery_attempts": 3,          # Max recoveries per cycle
    "recovery_cooldown_minutes": 15,     # Min time between recovery attempts

    # Telemetry keys to monitor
    "telemetry_keys": [
        "voltage", "current", "activePower", "frequency",
        "activeEnergy", "powerFactor"
    ],

    # Expected value ranges (for anomaly detection)
    "expected_ranges": {
        "voltage":     (100.0, 140.0),
        "current":     (0.0, 100.0),
        "activePower": (0.0, 50000.0),
        "frequency":   (55.0, 65.0),
        "powerFactor": (0.0, 1.0),
    },

    # Logging
    "log_dir": None,  # Set below
}

# Resolve log directory relative to this script
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
CONFIG["log_dir"] = str(PROJECT_DIR / "results" / "watchdog")

# ──────────────────────────────────────────────────────────────
# Logging Setup
# ──────────────────────────────────────────────────────────────

def setup_logging(log_dir: str) -> logging.Logger:
    """Configure logging to both console and rotating file."""
    os.makedirs(log_dir, exist_ok=True)

    log_file = os.path.join(log_dir, f"watchdog_{datetime.now():%Y%m%d}.log")

    logger = logging.getLogger("ami_watchdog")
    logger.setLevel(logging.DEBUG)

    # Console handler — INFO level
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(message)s", datefmt="%H:%M:%S"
    ))
    logger.addHandler(ch)

    # File handler — DEBUG level
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(fh)

    return logger


# ──────────────────────────────────────────────────────────────
# ThingsBoard Edge API Client
# ──────────────────────────────────────────────────────────────

class TBEdgeClient:
    """Minimal ThingsBoard Edge REST API client."""

    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.token: Optional[str] = None
        self.token_expires: float = 0

    def _ensure_auth(self):
        """Authenticate or refresh token."""
        if self.token and time.time() < self.token_expires:
            return
        resp = requests.post(
            f"{self.base_url}/api/auth/login",
            json={"username": self.username, "password": self.password},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        self.token = data["token"]
        # Tokens typically valid ~15min, refresh at 12min
        self.token_expires = time.time() + 720

    def _headers(self) -> dict:
        self._ensure_auth()
        return {"X-Authorization": f"Bearer {self.token}"}

    def _get(self, path: str, **kwargs) -> dict:
        resp = requests.get(
            f"{self.base_url}{path}",
            headers=self._headers(),
            timeout=15,
            **kwargs,
        )
        resp.raise_for_status()
        return resp.json()

    def get_device_attributes(self, device_id: str, scope: str = "SERVER_SCOPE") -> dict:
        """Get device attributes, returns dict keyed by attribute name."""
        attrs = self._get(
            f"/api/plugins/telemetry/DEVICE/{device_id}/values/attributes/{scope}"
        )
        return {a["key"]: a for a in attrs}

    def get_latest_telemetry(self, device_id: str, keys: list) -> dict:
        """Get latest telemetry values."""
        keys_str = ",".join(keys)
        return self._get(
            f"/api/plugins/telemetry/DEVICE/{device_id}/values/timeseries",
            params={"keys": keys_str},
        )

    def get_device_info(self, device_id: str) -> dict:
        """Get device details."""
        return self._get(f"/api/device/{device_id}")


# ──────────────────────────────────────────────────────────────
# Health Checks
# ──────────────────────────────────────────────────────────────

class HealthStatus:
    """Aggregated health check results."""

    def __init__(self):
        self.checks: list[dict] = []
        self.overall = "OK"  # OK, WARNING, CRITICAL
        self.timestamp = datetime.now(timezone.utc)
        self.recovery_needed = False
        self.recovery_actions: list[str] = []

    def add_check(self, name: str, status: str, message: str, details: dict = None):
        check = {
            "name": name,
            "status": status,
            "message": message,
            "details": details or {},
        }
        self.checks.append(check)

        # Escalate overall status
        if status == "CRITICAL" and self.overall != "CRITICAL":
            self.overall = "CRITICAL"
            self.recovery_needed = True
        elif status == "WARNING" and self.overall == "OK":
            self.overall = "WARNING"

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "overall": self.overall,
            "recovery_needed": self.recovery_needed,
            "recovery_actions": self.recovery_actions,
            "checks": self.checks,
        }


def check_edge_reachable(config: dict, logger: logging.Logger) -> dict:
    """Check 1: Is TB Edge API reachable?"""
    try:
        # Try login endpoint since TB Edge may not expose /health
        resp = requests.post(
            f"{config['tb_edge_url']}/api/auth/login",
            json={"username": config["tb_username"], "password": config["tb_password"]},
            timeout=10,
        )
        if resp.status_code == 200 and "token" in resp.json():
            return {"status": "OK", "message": "TB Edge API reachable and auth working"}
        return {"status": "WARNING", "message": f"TB Edge auth returned HTTP {resp.status_code}"}
    except requests.ConnectionError:
        return {"status": "CRITICAL", "message": "TB Edge API unreachable — connection refused"}
    except requests.Timeout:
        return {"status": "CRITICAL", "message": "TB Edge API unreachable — timeout"}
    except Exception as e:
        return {"status": "CRITICAL", "message": f"TB Edge API error: {e}"}


def check_docker_containers(config: dict, logger: logging.Logger) -> dict:
    """Check 2: Are Docker containers running on the Edge?"""
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
             config["edge_ssh_host"],
             "docker ps --format '{{.Names}}|{{.Status}}' 2>/dev/null"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return {"status": "WARNING", "message": f"SSH/Docker check failed: {result.stderr.strip()}"}

        containers = {}
        for line in result.stdout.strip().split("\n"):
            if "|" in line:
                name, status = line.split("|", 1)
                containers[name.strip()] = status.strip()

        required = ["tb-edge", "tb-edge-postgres"]
        issues = []
        for c in required:
            if c not in containers:
                issues.append(f"{c}: NOT RUNNING")
            elif "Up" not in containers[c]:
                issues.append(f"{c}: {containers[c]}")

        if issues:
            return {
                "status": "CRITICAL",
                "message": f"Docker issues: {'; '.join(issues)}",
                "details": containers,
            }

        return {
            "status": "OK",
            "message": f"All containers healthy: {', '.join(f'{k}={v}' for k,v in containers.items())}",
            "details": containers,
        }
    except subprocess.TimeoutExpired:
        return {"status": "WARNING", "message": "SSH timeout checking Docker"}
    except FileNotFoundError:
        return {"status": "WARNING", "message": "SSH not available — cannot check Docker"}
    except Exception as e:
        return {"status": "WARNING", "message": f"Docker check error: {e}"}


def check_device_connectivity(tb: TBEdgeClient, config: dict, logger: logging.Logger) -> dict:
    """Check 3: Is the device active in ThingsBoard?"""
    try:
        attrs = tb.get_device_attributes(config["device_id"])

        active = attrs.get("active", {}).get("value", False)
        last_connect_ts = attrs.get("lastConnectTime", {}).get("value", 0)
        last_disconnect_ts = attrs.get("lastDisconnectTime", {}).get("value", 0)
        last_activity_ts = attrs.get("lastActivityTime", {}).get("value", 0)

        now_ms = int(time.time() * 1000)
        inactive_minutes = (now_ms - last_activity_ts) / 60000 if last_activity_ts else float("inf")

        details = {
            "active": active,
            "last_connect": datetime.fromtimestamp(last_connect_ts / 1000).strftime("%H:%M:%S") if last_connect_ts else "never",
            "last_activity": datetime.fromtimestamp(last_activity_ts / 1000).strftime("%H:%M:%S") if last_activity_ts else "never",
            "last_disconnect": datetime.fromtimestamp(last_disconnect_ts / 1000).strftime("%H:%M:%S") if last_disconnect_ts else "never",
            "inactive_minutes": round(inactive_minutes, 1),
        }

        if not active:
            return {
                "status": "CRITICAL",
                "message": f"Device OFFLINE — last active {details['last_activity']} ({inactive_minutes:.0f}min ago)",
                "details": details,
            }

        if inactive_minutes > config["stale_telemetry_minutes"]:
            return {
                "status": "WARNING",
                "message": f"Device active but stale — {inactive_minutes:.0f}min since last activity",
                "details": details,
            }

        return {
            "status": "OK",
            "message": f"Device online — active {inactive_minutes:.0f}min ago",
            "details": details,
        }
    except Exception as e:
        return {"status": "CRITICAL", "message": f"Cannot check device: {e}"}


def check_telemetry_freshness(tb: TBEdgeClient, config: dict, logger: logging.Logger) -> dict:
    """Check 4: Is telemetry data fresh and within expected ranges?"""
    try:
        ts_data = tb.get_latest_telemetry(config["device_id"], config["telemetry_keys"])

        if not ts_data:
            return {"status": "CRITICAL", "message": "No telemetry data found"}

        now_ms = int(time.time() * 1000)
        stale_keys = []
        anomalies = []
        values = {}

        for key, entries in ts_data.items():
            if not entries:
                stale_keys.append(key)
                continue

            entry = entries[0]
            age_min = (now_ms - entry["ts"]) / 60000
            val = float(entry["value"])
            values[key] = {"value": val, "age_min": round(age_min, 1)}

            if age_min > config["stale_telemetry_minutes"]:
                stale_keys.append(f"{key}({age_min:.0f}min)")

            # Range check
            if key in config["expected_ranges"]:
                lo, hi = config["expected_ranges"][key]
                if not (lo <= val <= hi):
                    anomalies.append(f"{key}={val} (expected {lo}-{hi})")

        details = {"values": values, "stale_keys": stale_keys, "anomalies": anomalies}

        if stale_keys:
            stale_min = max(v["age_min"] for v in values.values()) if values else float("inf")
            severity = "CRITICAL" if stale_min > config["critical_telemetry_minutes"] else "WARNING"
            return {
                "status": severity,
                "message": f"Stale telemetry: {', '.join(stale_keys)}",
                "details": details,
            }

        if anomalies:
            return {
                "status": "WARNING",
                "message": f"Telemetry anomalies: {', '.join(anomalies)}",
                "details": details,
            }

        # All good
        sample = values.get("voltage", {})
        return {
            "status": "OK",
            "message": f"Telemetry fresh — voltage={sample.get('value', '?')}V ({sample.get('age_min', '?')}min ago)",
            "details": details,
        }
    except Exception as e:
        return {"status": "CRITICAL", "message": f"Telemetry check failed: {e}"}


def check_lwm2m_registration(tb: TBEdgeClient, config: dict, logger: logging.Logger) -> dict:
    """Check 5: Verify LwM2M registration state."""
    try:
        attrs = tb.get_device_attributes(config["device_id"], "SHARED_SCOPE")
        client_attrs = tb.get_device_attributes(config["device_id"], "CLIENT_SCOPE")

        details = {}
        for a_dict in [attrs, client_attrs]:
            for k, v in a_dict.items():
                details[k] = v.get("value")

        # If we have manufacturer info, registration was successful at some point
        has_client_data = "Manufacturer" in details or "ModelNumber" in details

        if has_client_data:
            return {
                "status": "OK",
                "message": f"LwM2M registered — {details.get('Manufacturer', '?')} / {details.get('ModelNumber', '?')}",
                "details": details,
            }

        return {
            "status": "WARNING",
            "message": "No LwM2M client attributes found — registration may have failed",
            "details": details,
        }
    except Exception as e:
        return {"status": "WARNING", "message": f"LwM2M check error: {e}"}


# ──────────────────────────────────────────────────────────────
# Recovery Actions
# ──────────────────────────────────────────────────────────────

class RecoveryManager:
    """Manages progressive recovery attempts."""

    RECOVERY_STATE_FILE = "watchdog_recovery_state.json"

    def __init__(self, config: dict, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.state_file = os.path.join(config["log_dir"], self.RECOVERY_STATE_FILE)
        self.state = self._load_state()

    def _load_state(self) -> dict:
        try:
            with open(self.state_file, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {
                "last_recovery_time": 0,
                "recovery_count": 0,
                "last_recovery_action": None,
                "escalation_level": 0,
                "history": [],
            }

    def _save_state(self):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w") as f:
            json.dump(self.state, f, indent=2)

    def can_attempt_recovery(self) -> bool:
        """Check if recovery cooldown has elapsed."""
        cooldown_sec = self.config["recovery_cooldown_minutes"] * 60
        elapsed = time.time() - self.state["last_recovery_time"]

        if elapsed < cooldown_sec:
            self.logger.info(
                f"Recovery cooldown: {cooldown_sec - elapsed:.0f}s remaining"
            )
            return False

        if self.state["recovery_count"] >= self.config["max_recovery_attempts"]:
            self.logger.warning(
                f"Max recovery attempts ({self.config['max_recovery_attempts']}) reached. "
                "Manual intervention required."
            )
            return False

        return True

    def attempt_recovery(self, health: HealthStatus, dry_run: bool = False) -> list[str]:
        """
        Progressive recovery strategy:
          Level 0: Wait & re-check (node may auto-recover)
          Level 1: Restart TB Edge Docker containers
          Level 2: Toggle DTR on serial (hardware reset ESP32)
          Level 3: Full restart: Docker + DTR reset
        """
        level = self.state["escalation_level"]
        actions_taken = []

        self.logger.info(f"=== Recovery Attempt (Level {level}) ===")

        if dry_run:
            self.logger.info("[DRY RUN] Would attempt recovery but --dry-run is set")
            actions_taken.append(f"[DRY RUN] Level {level} recovery skipped")
            return actions_taken

        if level == 0:
            # Level 0: Just wait — the node may reboot and auto-register
            msg = "Level 0: Passive wait — node may auto-recover on next LwM2M registration cycle (5min)"
            self.logger.info(msg)
            actions_taken.append(msg)

        elif level == 1:
            # Level 1: Restart TB Edge LwM2M transport (soft restart Docker)
            msg = "Level 1: Restarting TB Edge Docker container"
            self.logger.info(msg)
            actions_taken.append(msg)
            success = self._restart_docker_edge()
            if success:
                actions_taken.append("Docker restart completed")
            else:
                actions_taken.append("Docker restart FAILED")

        elif level == 2:
            # Level 2: Hardware reset the ESP32 via DTR toggle
            msg = "Level 2: Hardware reset ESP32 via serial DTR toggle"
            self.logger.info(msg)
            actions_taken.append(msg)
            success = self._reset_esp32_serial()
            if success:
                actions_taken.append("ESP32 hardware reset sent")
            else:
                actions_taken.append("ESP32 reset FAILED — serial not available")

        elif level >= 3:
            # Level 3: Full restart
            msg = "Level 3: Full restart — Docker + ESP32 reset"
            self.logger.info(msg)
            actions_taken.append(msg)
            self._restart_docker_edge()
            time.sleep(10)
            self._reset_esp32_serial()
            actions_taken.append("Full restart executed")

        # Update state
        self.state["last_recovery_time"] = time.time()
        self.state["recovery_count"] += 1
        self.state["escalation_level"] = min(level + 1, 3)
        self.state["last_recovery_action"] = {
            "level": level,
            "time": datetime.now().isoformat(),
            "actions": actions_taken,
        }
        self.state["history"].append(self.state["last_recovery_action"])

        # Keep only last 50 entries
        self.state["history"] = self.state["history"][-50:]
        self._save_state()

        return actions_taken

    def reset_on_success(self):
        """Reset escalation when system is healthy."""
        if self.state["escalation_level"] > 0 or self.state["recovery_count"] > 0:
            self.logger.info("System healthy — resetting recovery escalation level")
            self.state["escalation_level"] = 0
            self.state["recovery_count"] = 0
            self._save_state()

    def _restart_docker_edge(self) -> bool:
        """Restart tb-edge Docker container via SSH."""
        try:
            result = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=5",
                 self.config["edge_ssh_host"],
                 "docker restart tb-edge"],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                self.logger.info("Docker container tb-edge restarted successfully")
                # Wait for Edge to come back up
                self.logger.info("Waiting 30s for Edge to initialize...")
                time.sleep(30)
                return True
            else:
                self.logger.error(f"Docker restart failed: {result.stderr}")
                return False
        except Exception as e:
            self.logger.error(f"Docker restart error: {e}")
            return False

    def _reset_esp32_serial(self) -> bool:
        """Reset ESP32 via DTR toggle on serial port."""
        try:
            import serial
            port = self.config["serial_port"]
            self.logger.info(f"Opening {port} for DTR reset...")
            ser = serial.Serial(port, self.config["serial_baud"], timeout=1)
            time.sleep(0.1)
            # Toggle DTR to reset ESP32
            ser.dtr = False
            time.sleep(0.1)
            ser.dtr = True
            time.sleep(0.5)
            ser.close()
            self.logger.info(f"DTR reset sent on {port}")
            # Wait for node to boot
            self.logger.info("Waiting 60s for node to boot and register...")
            time.sleep(60)
            return True
        except ImportError:
            self.logger.warning("pyserial not installed — cannot reset via serial")
            return False
        except Exception as e:
            self.logger.error(f"Serial reset error: {e}")
            return False


# ──────────────────────────────────────────────────────────────
# Report Generation
# ──────────────────────────────────────────────────────────────

def save_report(health: HealthStatus, config: dict, logger: logging.Logger):
    """Save health check report as JSON."""
    report_dir = os.path.join(config["log_dir"], "reports")
    os.makedirs(report_dir, exist_ok=True)

    # Save latest
    latest_file = os.path.join(report_dir, "latest.json")
    with open(latest_file, "w") as f:
        json.dump(health.to_dict(), f, indent=2, default=str)

    # Save timestamped report (keep history)
    ts_file = os.path.join(
        report_dir,
        f"report_{datetime.now():%Y%m%d_%H%M%S}.json"
    )
    with open(ts_file, "w") as f:
        json.dump(health.to_dict(), f, indent=2, default=str)

    # Append to daily CSV for quick analysis
    csv_file = os.path.join(config["log_dir"], f"health_{datetime.now():%Y%m%d}.csv")
    csv_exists = os.path.exists(csv_file)
    with open(csv_file, "a") as f:
        if not csv_exists:
            f.write("timestamp,overall,edge_api,docker,device,telemetry,lwm2m,recovery_needed\n")

        check_statuses = {c["name"]: c["status"] for c in health.checks}
        f.write(
            f"{datetime.now():%H:%M:%S},"
            f"{health.overall},"
            f"{check_statuses.get('edge_api', '?')},"
            f"{check_statuses.get('docker', '?')},"
            f"{check_statuses.get('device_connectivity', '?')},"
            f"{check_statuses.get('telemetry', '?')},"
            f"{check_statuses.get('lwm2m', '?')},"
            f"{health.recovery_needed}\n"
        )

    logger.debug(f"Report saved: {ts_file}")


def print_health_summary(health: HealthStatus, logger: logging.Logger):
    """Print a formatted health summary."""
    status_icons = {"OK": "✓", "WARNING": "⚠", "CRITICAL": "✗"}

    logger.info("=" * 60)
    logger.info(f"  AMI Health Check — {health.timestamp:%Y-%m-%d %H:%M:%S UTC}")
    logger.info(f"  Overall: {status_icons.get(health.overall, '?')} {health.overall}")
    logger.info("=" * 60)

    for check in health.checks:
        icon = status_icons.get(check["status"], "?")
        logger.info(f"  {icon} {check['name']:.<30s} {check['status']:>8s}  {check['message']}")

    if health.recovery_needed:
        logger.info("-" * 60)
        logger.info("  *** RECOVERY NEEDED ***")
        for action in health.recovery_actions:
            logger.info(f"    → {action}")

    logger.info("=" * 60)


# ──────────────────────────────────────────────────────────────
# Main Watchdog Loop
# ──────────────────────────────────────────────────────────────

def run_health_check(config: dict, logger: logging.Logger, dry_run: bool = False) -> HealthStatus:
    """Execute all health checks and attempt recovery if needed."""
    health = HealthStatus()
    recovery = RecoveryManager(config, logger)

    # ── Check 1: Edge API ──
    logger.debug("Checking TB Edge API...")
    result = check_edge_reachable(config, logger)
    health.add_check("edge_api", result["status"], result["message"])

    if result["status"] == "CRITICAL":
        # If Edge is down, skip remaining checks
        logger.warning("Edge unreachable — skipping remaining checks")
        print_health_summary(health, logger)
        save_report(health, config, logger)
        return health

    # ── Check 2: Docker containers ──
    logger.debug("Checking Docker containers...")
    result = check_docker_containers(config, logger)
    health.add_check("docker", result["status"], result["message"],
                     result.get("details"))

    # ── Connect to TB API for remaining checks ──
    try:
        tb = TBEdgeClient(config["tb_edge_url"], config["tb_username"], config["tb_password"])

        # ── Check 3: Device connectivity ──
        logger.debug("Checking device connectivity...")
        result = check_device_connectivity(tb, config, logger)
        health.add_check("device_connectivity", result["status"],
                         result["message"], result.get("details"))

        # ── Check 4: Telemetry freshness ──
        logger.debug("Checking telemetry freshness...")
        result = check_telemetry_freshness(tb, config, logger)
        health.add_check("telemetry", result["status"], result["message"],
                         result.get("details"))

        # ── Check 5: LwM2M registration ──
        logger.debug("Checking LwM2M registration...")
        result = check_lwm2m_registration(tb, config, logger)
        health.add_check("lwm2m", result["status"], result["message"],
                         result.get("details"))

    except Exception as e:
        health.add_check("tb_api", "CRITICAL", f"TB API error: {e}")

    # ── Recovery decision ──
    if health.recovery_needed:
        if recovery.can_attempt_recovery():
            logger.warning("Initiating recovery...")
            actions = recovery.attempt_recovery(health, dry_run=dry_run)
            health.recovery_actions = actions
        else:
            health.recovery_actions = ["Recovery on cooldown or max attempts reached"]
            logger.warning("Recovery skipped (cooldown or max attempts)")
    else:
        # System healthy — reset recovery state
        recovery.reset_on_success()

    # ── Output & Save ──
    print_health_summary(health, logger)
    save_report(health, config, logger)

    return health


def daemon_loop(config: dict, logger: logging.Logger, interval: int, dry_run: bool):
    """Run health checks continuously."""
    logger.info(f"AMI Watchdog daemon started — checking every {interval}s")
    logger.info(f"Device: {config['device_name']} @ {config['tb_edge_url']}")
    logger.info(f"Logs: {config['log_dir']}")

    # Handle graceful shutdown
    running = True

    def signal_handler(sig, frame):
        nonlocal running
        logger.info("Shutdown signal received — stopping watchdog")
        running = False

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    check_count = 0
    while running:
        check_count += 1
        logger.info(f"\n{'─' * 60}")
        logger.info(f"Health check #{check_count} at {datetime.now():%H:%M:%S}")

        try:
            health = run_health_check(config, logger, dry_run=dry_run)

            if health.overall == "CRITICAL":
                logger.critical(
                    f"CRITICAL state detected — next check in {interval}s"
                )
        except Exception as e:
            logger.error(f"Health check failed with exception: {e}", exc_info=True)

        # Wait for next check
        for _ in range(interval):
            if not running:
                break
            time.sleep(1)

    logger.info("AMI Watchdog stopped")


# ──────────────────────────────────────────────────────────────
# CLI Entry Point
# ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="AMI Watchdog — Automated Monitoring & Recovery System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python ami_watchdog.py                     # Single health check
    python ami_watchdog.py --daemon            # Continuous monitoring (every 5min)
    python ami_watchdog.py --daemon -i 120     # Every 2 minutes
    python ami_watchdog.py --dry-run           # Check only, no recovery
    python ami_watchdog.py --stale-threshold 5 # Alert after 5min stale
        """,
    )
    parser.add_argument(
        "--daemon", "-d", action="store_true",
        help="Run continuously (default: single check)",
    )
    parser.add_argument(
        "--interval", "-i", type=int, default=300,
        help="Check interval in seconds for daemon mode (default: 300 = 5min)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Check only — do not execute recovery actions",
    )
    parser.add_argument(
        "--stale-threshold", type=int, default=None,
        help=f"Minutes before telemetry is considered stale (default: {CONFIG['stale_telemetry_minutes']})",
    )
    parser.add_argument(
        "--critical-threshold", type=int, default=None,
        help=f"Minutes before triggering recovery (default: {CONFIG['critical_telemetry_minutes']})",
    )
    parser.add_argument(
        "--log-dir", type=str, default=None,
        help=f"Log directory (default: {CONFIG['log_dir']})",
    )

    args = parser.parse_args()

    # Apply overrides
    config = CONFIG.copy()
    if args.stale_threshold:
        config["stale_telemetry_minutes"] = args.stale_threshold
    if args.critical_threshold:
        config["critical_telemetry_minutes"] = args.critical_threshold
    if args.log_dir:
        config["log_dir"] = args.log_dir

    # Setup logging
    logger = setup_logging(config["log_dir"])

    if args.daemon:
        daemon_loop(config, logger, args.interval, args.dry_run)
    else:
        health = run_health_check(config, logger, dry_run=args.dry_run)
        sys.exit(0 if health.overall == "OK" else 1)


if __name__ == "__main__":
    main()
