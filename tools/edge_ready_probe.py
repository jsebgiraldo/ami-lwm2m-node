"""Probe TB Edge readiness + device count. Exits 0 when ready."""
import json
import sys
import urllib.request


def main():
    base = "http://192.168.8.111:8090"
    try:
        r = urllib.request.Request(
            base + "/api/auth/login",
            data=json.dumps({"username": "tenant@thingsboard.org", "password": "tenant"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        tok = json.loads(urllib.request.urlopen(r, timeout=5).read())["token"]
        r2 = urllib.request.Request(
            base + "/api/tenant/devices?pageSize=100&page=0",
            headers={"X-Authorization": f"Bearer {tok}"},
        )
        devs = json.loads(urllib.request.urlopen(r2, timeout=5).read())
        esp32 = [d for d in devs["data"] if d["name"].startswith("ami-esp32c6-")]
        print(f"EDGE_READY total={len(devs['data'])} ami={len(esp32)}")
        sys.exit(0)
    except Exception as e:
        print(f"still_init: {str(e)[:80]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
