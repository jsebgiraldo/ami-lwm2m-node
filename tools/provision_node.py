#!/usr/bin/env python3
"""
AMI Node Provisioning Tool — ThingsBoard Edge REST API
=======================================================
Registers a new XIAO ESP32-C6 node (from factory) in ThingsBoard Edge
so it can connect via LwM2M the first time it boots.

FLOW:
  1. Compute endpoint name from MAC (last 2 bytes): ami-esp32c6-XXYY
  2. Login to TB Edge / Cloud via REST API
  3. Check if device already exists (idempotent)
  4. Create device under profile C2000_Monofasico_v2
  5. Set LwM2M NoSec credentials (endpoint name)
  6. (Optional) verify with --verify flag

USAGE:
  # Single node – MAC on the label
  python provision_node.py --mac 98:a3:16:61:24:34

  # Specify endpoint manually (e.g. if MAC not available)
  python provision_node.py --endpoint ami-esp32c6-2434

  # Batch from CSV file (columns: mac or endpoint)
  python provision_node.py --csv nodes.csv

  # Against Cloud instead of Edge
  python provision_node.py --mac 98:a3:16:61:24:34 --host 192.168.1.159 --port 80

  # Delete a device (factory reset in cloud)
  python provision_node.py --mac 98:a3:16:61:24:34 --delete

REQUIRES: pip install requests
"""

import argparse
import csv
import json
import sys
import time
import requests

# ── Default connection params ──────────────────────────────────────────────────
EDGE_HOST = "192.168.1.111"
EDGE_PORT = 8090
CLOUD_HOST = "192.168.1.159"
CLOUD_PORT = 80
DEFAULT_USER = "tenant@thingsboard.org"
DEFAULT_PASS = "tenant"

# ── Profile name that ALL AMI LwM2M nodes must use ───────────────────────────
TARGET_PROFILE_NAME = "C2000_Monofasico_v2"

# ── Endpoint prefix (must match firmware build_endpoint_name()) ───────────────
ENDPOINT_PREFIX = "ami-esp32c6"


# ─────────────────────────────────────────────────────────────────────────────
def mac_to_endpoint(mac: str) -> str:
    """
    Derive LwM2M endpoint name from Ethernet/802.15.4 MAC address.

    Algorithm (matches firmware build_endpoint_name in main.c):
      endpoint = "ami-esp32c6-{mac[-2]:02x}{mac[-1]:02x}"

    Examples:
      98:a3:16:61:24:34  →  ami-esp32c6-2434
      98:a3:16:61:AB:CD  →  ami-esp32c6-abcd
    """
    parts = [p.strip() for p in mac.replace("-", ":").split(":")]
    if len(parts) < 2:
        raise ValueError(f"Invalid MAC: '{mac}' — need at least 6 octets")
    last2 = parts[-2:]
    return f"{ENDPOINT_PREFIX}-{last2[0].lower()}{last2[1].lower()}"


# ─────────────────────────────────────────────────────────────────────────────
class TBClient:
    """Thin ThingsBoard REST client."""

    def __init__(self, host: str, port: int, user: str, password: str):
        self.base = f"http://{host}:{port}"
        self.user = user
        self.password = password
        self.token = None
        self.session = requests.Session()

    def login(self) -> None:
        url = f"{self.base}/api/auth/login"
        r = self.session.post(url, json={"username": self.user, "password": self.password}, timeout=10)
        r.raise_for_status()
        self.token = r.json()["token"]
        self.session.headers.update({"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        print(f"  [OK] Authenticated as {self.user}")

    def _get(self, path: str, params: dict = None):
        r = self.session.get(f"{self.base}{path}", params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict):
        r = self.session.post(f"{self.base}{path}", json=body, timeout=10)
        r.raise_for_status()
        return r.json()

    def _put(self, path: str, body: dict):
        r = self.session.put(f"{self.base}{path}", json=body, timeout=10)
        r.raise_for_status()
        return r.json()

    def _delete(self, path: str):
        r = self.session.delete(f"{self.base}{path}", timeout=10)
        r.raise_for_status()

    # ── Profile resolution ───────────────────────────────────────────────────
    def get_profile_id(self, profile_name: str) -> str:
        """Return profile ID for a given name, None if not found."""
        data = self._get("/api/deviceProfiles", {"pageSize": 50, "page": 0})
        for p in data.get("data", []):
            if p["name"] == profile_name:
                return p["id"]["id"]
        return None

    def get_profile_full(self, profile_id: str) -> dict:
        return self._get(f"/api/deviceProfile/{profile_id}")

    # ── Device CRUD ──────────────────────────────────────────────────────────
    def find_device_by_name(self, name: str) -> dict | None:
        """Return device dict if it exists, None otherwise."""
        try:
            data = self._get("/api/tenant/deviceInfos", {"pageSize": 100, "page": 0, "textSearch": name})
            for d in data.get("data", []):
                if d["name"] == name:
                    return d
        except requests.HTTPError:
            pass
        return None

    def create_device(self, name: str, profile_id: str, label: str = None) -> dict:
        """Create a device and return it."""
        body = {
            "name": name,
            "label": label or name,
            "deviceProfileId": {"entityType": "DEVICE_PROFILE", "id": profile_id},
        }
        return self._post("/api/device", body)

    def get_device_credentials(self, device_id: str) -> dict:
        return self._get(f"/api/device/{device_id}/credentials")

    def set_lwm2m_nosec_credentials(self, device_id: str, endpoint: str) -> dict:
        """Set LwM2M NoSec credentials (endpoint name = identity)."""
        creds_value = json.dumps({
            "client": {
                "securityConfigClientMode": "NO_SEC",
                "endpoint": endpoint,
            },
            "bootstrap": {
                "bootstrapServer": {"securityMode": "NO_SEC"},
                "lwm2mServer": {"securityMode": "NO_SEC"},
            },
        })
        # First read current credentials to get the id field
        existing = self.get_device_credentials(device_id)
        body = {
            "id": existing.get("id"),
            "deviceId": {"entityType": "DEVICE", "id": device_id},
            "credentialsType": "LWM2M_CREDENTIALS",
            "credentialsId": endpoint,
            "credentialsValue": creds_value,
        }
        return self._put("/api/device/credentials", body)

    def delete_device(self, device_id: str) -> None:
        self._delete(f"/api/device/{device_id}")

    def get_latest_telemetry(self, device_id: str) -> dict:
        try:
            return self._get(f"/api/plugins/telemetry/DEVICE/{device_id}/values/timeseries")
        except Exception:
            return {}

    def get_device_attributes(self, device_id: str) -> dict:
        try:
            return self._get(f"/api/plugins/telemetry/DEVICE/{device_id}/values/attributes")
        except Exception:
            return {}


# ─────────────────────────────────────────────────────────────────────────────
def provision_single(tb: TBClient, endpoint: str, profile_name: str, dry_run: bool = False) -> dict:
    """
    Pre-register one node in ThingsBoard.

    Returns: {"endpoint": ..., "device_id": ..., "status": "created"|"exists"|"error"}
    """
    print(f"\n{'─'*60}")
    print(f"  Endpoint : {endpoint}")

    # Resolve profile
    profile_id = tb.get_profile_id(profile_name)
    if not profile_id:
        print(f"  [ERROR] Profile '{profile_name}' not found!")
        print(f"          Available profiles: check TB Edge → Device Profiles")
        return {"endpoint": endpoint, "status": "error", "reason": "profile_not_found"}

    print(f"  Profile  : {profile_name}  ({profile_id[:8]}...)")

    # Check existence
    existing = tb.find_device_by_name(endpoint)
    if existing:
        dev_id = existing["id"]["id"]
        print(f"  [SKIP] Device already exists: {dev_id}")
        active = existing.get("active", False)
        print(f"  Active   : {active}")
        return {"endpoint": endpoint, "device_id": dev_id, "status": "exists"}

    if dry_run:
        print(f"  [DRY-RUN] Would create device '{endpoint}' → profile '{profile_name}'")
        return {"endpoint": endpoint, "status": "dry_run"}

    # Create device
    dev = tb.create_device(endpoint, profile_id, label=endpoint)
    dev_id = dev["id"]["id"]
    print(f"  [OK] Device created: {dev_id}")

    # Set LwM2M NoSec credentials
    tb.set_lwm2m_nosec_credentials(dev_id, endpoint)
    print(f"  [OK] Credentials set: LWM2M_CREDENTIALS / NO_SEC / endpoint={endpoint}")

    return {"endpoint": endpoint, "device_id": dev_id, "status": "created"}


def delete_single(tb: TBClient, endpoint: str) -> None:
    existing = tb.find_device_by_name(endpoint)
    if not existing:
        print(f"  [SKIP] Device '{endpoint}' not found.")
        return
    dev_id = existing["id"]["id"]
    tb.delete_device(dev_id)
    print(f"  [OK] Deleted: {endpoint} ({dev_id})")


def verify_single(tb: TBClient, endpoint: str) -> None:
    dev = tb.find_device_by_name(endpoint)
    if not dev:
        print(f"  [FAIL] Device '{endpoint}' NOT found in ThingsBoard")
        return
    dev_id = dev["id"]["id"]
    creds = tb.get_device_credentials(dev_id)
    tel = tb.get_latest_telemetry(dev_id)
    print(f"  Device ID   : {dev_id}")
    print(f"  Active      : {dev.get('active', False)}")
    print(f"  Profile     : {dev.get('deviceProfileName', '?')}")
    print(f"  Cred type   : {creds.get('credentialsType', '?')}")
    print(f"  Cred ID     : {creds.get('credentialsId', '?')}")
    if tel:
        for key, vals in list(tel.items())[:5]:
            v = vals[0] if isinstance(vals, list) else vals
            print(f"  Telemetry   : {key} = {v.get('value', '?') if isinstance(v, dict) else v}")
    else:
        print(f"  Telemetry   : (no data yet)")


# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Pre-register AMI nodes in ThingsBoard Edge (factory provisioning)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # Source: MAC, endpoint, or CSV
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--mac", help="Device MAC (e.g. 98:a3:16:61:24:34)")
    src.add_argument("--endpoint", help="Explicit endpoint (e.g. ami-esp32c6-2434)")
    src.add_argument("--csv", help="CSV file with column 'mac' or 'endpoint'")

    # Connection
    parser.add_argument("--host", default=EDGE_HOST, help=f"TB host (default: {EDGE_HOST})")
    parser.add_argument("--port", type=int, default=EDGE_PORT, help=f"TB port (default: {EDGE_PORT})")
    parser.add_argument("--user", default=DEFAULT_USER, help=f"TB user (default: {DEFAULT_USER})")
    parser.add_argument("--password", default=DEFAULT_PASS, help="TB password")
    parser.add_argument("--profile", default=TARGET_PROFILE_NAME, help=f"Device profile (default: {TARGET_PROFILE_NAME})")

    # Actions
    parser.add_argument("--delete", action="store_true", help="Delete the device instead of creating")
    parser.add_argument("--verify", action="store_true", help="Show device status + latest telemetry")
    parser.add_argument("--dry-run", action="store_true", help="Print what would happen without making changes")

    args = parser.parse_args()

    # Effective profile name
    profile_name = args.profile

    # Build list of endpoints to process
    endpoints = []
    if args.mac:
        endpoints.append(mac_to_endpoint(args.mac))
    elif args.endpoint:
        endpoints.append(args.endpoint)
    elif args.csv:
        with open(args.csv, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if "mac" in row and row["mac"].strip():
                    endpoints.append(mac_to_endpoint(row["mac"].strip()))
                elif "endpoint" in row and row["endpoint"].strip():
                    endpoints.append(row["endpoint"].strip())

    if not endpoints:
        print("[ERROR] No endpoints to process.")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  AMI Node Provisioner")
    print(f"  Target : http://{args.host}:{args.port}")
    print(f"  Profile: {profile_name}")
    print(f"  Nodes  : {len(endpoints)}")
    print(f"  Action : {'DELETE' if args.delete else 'VERIFY' if args.verify else 'PROVISION' + (' [DRY-RUN]' if args.dry_run else '')}")
    print(f"{'='*60}")

    # Authenticate
    tb = TBClient(args.host, args.port, args.user, args.password)
    try:
        tb.login()
    except Exception as e:
        print(f"\n[ERROR] Login failed: {e}")
        print(f"  Check: host={args.host}:{args.port}, user={args.user}")
        sys.exit(2)

    # Process each endpoint
    results = []
    for ep in endpoints:
        try:
            if args.delete:
                delete_single(tb, ep)
            elif args.verify:
                print(f"\n{'─'*60}")
                print(f"  Endpoint : {ep}")
                verify_single(tb, ep)
            else:
                r = provision_single(tb, ep, profile_name, dry_run=args.dry_run)
                results.append(r)
        except Exception as e:
            print(f"  [ERROR] {ep}: {e}")
            results.append({"endpoint": ep, "status": "error", "reason": str(e)})

    # Summary
    if results:
        print(f"\n{'='*60}")
        created = sum(1 for r in results if r.get("status") == "created")
        exists  = sum(1 for r in results if r.get("status") == "exists")
        errors  = sum(1 for r in results if r.get("status") == "error")
        print(f"  SUMMARY: {len(results)} total | {created} created | {exists} already existed | {errors} errors")
        print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
