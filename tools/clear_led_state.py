#!/usr/bin/env python3
"""Clear LwM2M Object 3311 (Light Control) state on all 30 devices via TB Edge RPC.

Sends WriteReplace /3311/0/5850=false (On/Off=OFF) and /3311/0/5851=0 (Dimmer=0)
to every active node. This both clears TB Edge's persisted snapshot AND tells
the device's firmware (via on_off_cb, dimmer_cb) that the operator wants OFF.

After running this, the LED should stay dark even with CONFIG_AMI_LED_QUIET_MODE=n.
Useful to verify whether the GREEN-stuck-LED on the fleet is firmware-driven
(server is re-pushing old "on" state) or hardware-stuck (LED won't respond).
"""
from __future__ import annotations
import requests, sys

EDGE = "http://192.168.8.111:8090"
USER, PASS = "tenant@thingsboard.org", "tenant"
PREFIX = "ami-esp32c6-"


def main():
    s = requests.Session()
    r = s.post(f"{EDGE}/api/auth/login",
               json={"username": USER, "password": PASS}, timeout=15)
    r.raise_for_status()
    s.headers.update({"X-Authorization": f"Bearer {r.json()['token']}"})

    out = s.get(f"{EDGE}/api/tenant/deviceInfos?pageSize=200&page=0", timeout=20).json()
    devs = [x for x in out.get("data", []) if x["name"].startswith(PREFIX)]
    print(f"[clear] {len(devs)} devices found")

    sent = ok = 0
    for d in devs:
        did = d["id"]["id"]
        name = d["name"]
        active = d.get("active", False)
        if not active:
            print(f"  SKIP {name}: not active in TB"); continue
        sent += 2
        try:
            r1 = s.post(f"{EDGE}/api/rpc/twoway/{did}",
                        json={"method": "WriteReplace",
                              "params": {"id": "/3311/0/5850", "value": False},
                              "timeout": 10000}, timeout=15)
            r2 = s.post(f"{EDGE}/api/rpc/twoway/{did}",
                        json={"method": "WriteReplace",
                              "params": {"id": "/3311/0/5851", "value": 0},
                              "timeout": 10000}, timeout=15)
            r1j = r1.json() if r1.text else {}
            r2j = r2.json() if r2.text else {}
            ok1 = "CHANGED" in str(r1j) or "result" not in r1j
            ok2 = "CHANGED" in str(r2j) or "result" not in r2j
            if ok1: ok += 1
            if ok2: ok += 1
            print(f"  {name}: 5850={r1j.get('result','?')} 5851={r2j.get('result','?')}")
        except Exception as e:
            print(f"  {name}: ERR {e}")

    print(f"\n[clear] {ok}/{sent} writes accepted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
