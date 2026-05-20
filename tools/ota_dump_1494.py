#!/usr/bin/env python3
"""One-shot: dump ALL fw/version attributes for 1494 on the Edge, split by scope,
so we can tell the DEVICE-reported running version (CLIENT scope) apart from the
ASSIGNED target package (SHARED scope), plus fw_state progress."""
from __future__ import annotations
import sys
import requests
import fleet_common as fc
fc.bootstrap_venv()

HOST, PORT = "192.168.8.111", 8090
USER, PASS = "tenant@thingsboard.org", "tenant"
ENDPOINT = "ami-esp32c6-1494"


def main() -> int:
    s = requests.Session()
    r = s.post(f"http://{HOST}:{PORT}/api/auth/login",
               json={"username": USER, "password": PASS}, timeout=15)
    r.raise_for_status()
    s.headers.update({"X-Authorization": f"Bearer {r.json()['token']}"})

    dev = s.get(f"http://{HOST}:{PORT}/api/tenant/devices",
                params={"pageSize": 1, "page": 0, "textSearch": ENDPOINT}, timeout=20).json()["data"][0]
    dev_id = dev["id"]["id"]
    print(f"device={ENDPOINT} id={dev_id}")
    print(f"assigned firmwareId={(dev.get('firmwareId') or {}).get('id')}")

    for scope in ("CLIENT_SCOPE", "SHARED_SCOPE", "SERVER_SCOPE"):
        r = s.get(f"http://{HOST}:{PORT}/api/plugins/telemetry/DEVICE/{dev_id}/values/attributes/{scope}", timeout=20)
        if not r.ok:
            print(f"\n[{scope}] HTTP {r.status_code}")
            continue
        rows = [a for a in r.json()
                if any(k in a["key"].lower() for k in ("fw", "firmware", "version", "ota", "5_0"))]
        print(f"\n[{scope}]")
        for a in sorted(rows, key=lambda x: x["key"]):
            print(f"  {a['key']:28s} = {a['value']}")

    # latest fw_state timeseries point
    r = s.get(f"http://{HOST}:{PORT}/api/plugins/telemetry/DEVICE/{dev_id}/values/timeseries",
              params={"keys": "fw_state", "limit": 5}, timeout=20)
    if r.ok and r.json():
        print(f"\n[timeseries fw_state] {r.json()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
