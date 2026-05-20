#!/usr/bin/env python3
"""Poll Edge API readiness, then wait for 1494 to become active again."""
import sys, time
sys.path.insert(0, "tools")
import fleet_common as fc
fc.bootstrap_venv()
import requests

H, P = "192.168.8.111", 8090
DID = "8aa5c720-5180-11f1-be35-8f7a9e586a76"


def ts():
    return time.strftime("%H:%M:%S")


def login():
    try:
        r = requests.post(f"http://{H}:{P}/api/auth/login",
                          json={"username": "tenant@thingsboard.org", "password": "tenant"}, timeout=8)
        return r.json()["token"] if r.status_code == 200 else None
    except Exception:
        return None


def main():
    deadline = time.time() + 240
    tok = None
    while time.time() < deadline:
        tok = login()
        if tok:
            print(f"[{ts()}] Edge API UP")
            break
        print(f"[{ts()}] waiting for Edge API...")
        time.sleep(10)
    if not tok:
        print("TIMEOUT waiting for Edge")
        return 1
    s = requests.Session()
    s.headers.update({"X-Authorization": f"Bearer {tok}"})
    deadline = time.time() + 240
    while time.time() < deadline:
        try:
            at = s.get(f"http://{H}:{P}/api/plugins/telemetry/DEVICE/{DID}/values/attributes", timeout=10).json()
            act = {x["key"]: x["value"] for x in at}.get("active")
        except Exception:
            act = None
        print(f"[{ts()}] 1494 active={act}")
        if act:
            return 0
        time.sleep(12)
    print("1494 not active within window")
    return 2


if __name__ == "__main__":
    sys.exit(main())
