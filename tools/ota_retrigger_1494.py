#!/usr/bin/env python3
"""Reset a stuck OTA (fw_state=FAILED) on one device by unassign->reassign of the
firmware package on TB Central. TB re-runs the FW update state machine and starts
a fresh block push (/5/0/0) once the assignment syncs to the Edge.

Usage:
    python tools/ota_retrigger_1494.py --endpoint ami-esp32c6-1494 --version 0.6.27
"""
from __future__ import annotations

import argparse
import sys
import time

import requests

import fleet_common as fc

fc.bootstrap_venv()

CENTRAL_HOST = "192.168.8.124"
CENTRAL_PORT = 8080
CENTRAL_USER = "tenant@thingsboard.org"
CENTRAL_PASS = "tenant"


class TB:
    def __init__(self, base, user, password):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        r = self.s.post(f"{self.base}/api/auth/login",
                        json={"username": user, "password": password}, timeout=15)
        r.raise_for_status()
        self.s.headers.update({"X-Authorization": f"Bearer {r.json()['token']}"})

    def get(self, path, **params):
        r = self.s.get(f"{self.base}{path}", params=params or None, timeout=20)
        r.raise_for_status()
        return r.json()

    def post_json(self, path, body):
        r = self.s.post(f"{self.base}{path}", json=body, timeout=30)
        if not r.ok:
            raise RuntimeError(f"POST {path} -> {r.status_code}: {r.text[:400]}")
        return r.json() if r.text else {}

    def device(self, name):
        data = self.get("/api/tenant/devices", pageSize=1, page=0, textSearch=name)["data"]
        if not data:
            raise SystemExit(f"device not found: {name}")
        return data[0]

    def ota_packages(self):
        return self.get("/api/otaPackages", pageSize=200, page=0)["data"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default="ami-esp32c6-1494")
    ap.add_argument("--version", default="0.6.27")
    ap.add_argument("--host", default=CENTRAL_HOST)
    ap.add_argument("--port", type=int, default=CENTRAL_PORT)
    args = ap.parse_args()

    tb = TB(f"http://{args.host}:{args.port}", CENTRAL_USER, CENTRAL_PASS)

    pkg = next((p for p in tb.ota_packages()
                if p.get("version") == args.version and p.get("type") == "FIRMWARE"), None)
    if not pkg:
        raise SystemExit(f"no FIRMWARE package version={args.version} on Central")
    pkg_id = pkg["id"]["id"]
    print(f"[ota] package {args.version} id={pkg_id}")

    dev = tb.device(args.endpoint)
    dev_id = dev["id"]["id"]
    cur = (dev.get("firmwareId") or {}).get("id")
    print(f"[ota] device {args.endpoint} id={dev_id} firmwareId={cur}")

    # 1. unassign (clears the FAILED state machine)
    dev["firmwareId"] = None
    tb.post_json("/api/device", dev)
    print("[ota] firmware UNASSIGNED -> waiting 12s for edge sync + device attr clear...")
    time.sleep(12)

    # 2. reassign -> fresh push
    dev = tb.device(args.endpoint)
    dev["firmwareId"] = {"entityType": "OTA_PACKAGE", "id": pkg_id}
    tb.post_json("/api/device", dev)
    print(f"[ota] firmware REASSIGNED ({args.version}) -> TB will push /5/0/0 after sync")
    print("[ota] done. Watch fw_state: FAILED->DOWNLOADING->DOWNLOADED->UPDATING->UPDATED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
