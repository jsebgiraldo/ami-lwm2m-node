#!/usr/bin/env python3
"""Poll Edge for 1494 fw_state + reported fw version until UPDATED/changed."""
from __future__ import annotations
import sys, time
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

    def attrs():
        r = s.get(f"http://{HOST}:{PORT}/api/plugins/telemetry/DEVICE/{dev_id}/values/attributes", timeout=20)
        return {a["key"]: a["value"] for a in r.json()} if r.ok else {}

    print(f"Watching {ENDPOINT} fw OTA over-the-air (~20 min)...\n", flush=True)
    last = None
    deadline = time.time() + 1200
    while time.time() < deadline:
        a = attrs()
        st = a.get("fw_state"); ver = a.get("current_fw_version") or a.get("fw_version")
        title = a.get("current_fw_title")
        line = f"fw_state={st} ver={ver} title={title} err={a.get('fw_error')}"
        ts = time.strftime("%H:%M:%S")
        mark = " <--" if line != last else ""
        print(f"[{ts}] {line}{mark}", flush=True)
        last = line
        if ver == "0.6.28":
            print(f"\n[{ts}] OTA COMPLETE over-the-air: state={st} ver={ver}", flush=True)
            return 0
        time.sleep(20)
    print("\n[timeout] no UPDATED within window", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
