"""Read SoC die temperatures remotely from all nodes via TB Edge RPC.

Reads IPSO Object 3303 (Temperature Sensor) v1.1:
  - /3303_1.1/0/5700  current value (Float)
  - /3303_1.1/0/5601  min measured since boot
  - /3303_1.1/0/5602  max measured since boot

Returned as OPAQUE bytes (IEEE 754 double big-endian) because TB Edge
does not have the IPSO 3303 v1.1 XML loaded server-side. We decode
the bytes locally.

Usage:
    python tools/check_temps.py
    python tools/check_temps.py ami-esp32c6-1494 ami-esp32c6-fbb8
"""
from __future__ import annotations

import argparse
import re
import struct
import sys

import fleet_common as fc
fc.bootstrap_venv()

import requests  # noqa: E402


def login(host: str, port: int, user: str, password: str) -> requests.Session:
    s = requests.Session()
    r = s.post(f"http://{host}:{port}/api/auth/login",
               json={"username": user, "password": password}, timeout=10)
    r.raise_for_status()
    s.headers.update({"Authorization": f"Bearer {r.json()['token']}",
                      "Content-Type": "application/json"})
    return s


def list_active_endpoints(s: requests.Session, base: str) -> list[str]:
    r = s.get(f"{base}/api/tenant/deviceInfos",
              params={"pageSize": 100, "page": 0, "textSearch": "ami-esp32c6"},
              timeout=10)
    r.raise_for_status()
    return [d["name"] for d in r.json().get("data", []) if d.get("active")]


def device_id(s: requests.Session, base: str, endpoint: str) -> str | None:
    r = s.get(f"{base}/api/tenant/deviceInfos",
              params={"pageSize": 100, "page": 0, "textSearch": endpoint},
              timeout=10)
    r.raise_for_status()
    for d in r.json().get("data", []):
        if d["name"].startswith(endpoint):
            return d["id"]["id"]
    return None


def decode_opaque_double(value_str: str) -> float | None:
    m = re.search(r"value=([0-9a-f]+)\s+type=OPAQUE", value_str, re.I)
    if not m:
        return None
    raw = bytes.fromhex(m.group(1))
    if len(raw) != 8:
        return None
    return struct.unpack(">d", raw)[0]


def read_resource(s: requests.Session, base: str, dev_id: str, path: str,
                  timeout_ms: int = 15000) -> float | None:
    body = {"method": "Read", "params": {"id": path},
            "persistent": False, "timeout": timeout_ms}
    try:
        r = s.post(f"{base}/api/plugins/rpc/twoway/{dev_id}", json=body,
                   timeout=(timeout_ms / 1000) + 5)
        r.raise_for_status()
        d = r.json()
    except Exception:
        return None
    if d.get("result") != "CONTENT":
        return None
    return decode_opaque_double(str(d.get("value", "")))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("endpoints", nargs="*",
                    help="endpoints to read (default: all active)")
    ap.add_argument("--mesh", default=fc.DEFAULT_MESH, choices=fc.MESH_TARGETS)
    ap.add_argument("--user", default=fc.EDGE_TENANT_USER)
    ap.add_argument("--password", default=fc.EDGE_TENANT_PASS)
    args = ap.parse_args()

    host, port = fc.edge_for_mesh(args.mesh)
    base = f"http://{host}:{port}"
    s = login(host, port, args.user, args.password)

    endpoints = args.endpoints or list_active_endpoints(s, base)
    if not endpoints:
        print("No active endpoints to read.")
        return 1

    print(f"{'endpoint':<22}  {'current':>9}  {'min':>9}  {'max':>9}  unit")
    print("-" * 70)
    for ep in sorted(endpoints):
        dev = device_id(s, base, ep)
        if dev is None:
            print(f"{ep:<22}  {'N/A':>9}  {'N/A':>9}  {'N/A':>9}  (device not found)")
            continue
        cur = read_resource(s, base, dev, "/3303_1.1/0/5700")
        mn  = read_resource(s, base, dev, "/3303_1.1/0/5601")
        mx  = read_resource(s, base, dev, "/3303_1.1/0/5602")
        cur_s = f"{cur:.2f}" if cur is not None else "N/A"
        mn_s  = f"{mn:.2f}"  if mn  is not None else "N/A"
        mx_s  = f"{mx:.2f}"  if mx  is not None else "N/A"
        print(f"{ep:<22}  {cur_s:>9}  {mn_s:>9}  {mx_s:>9}  Cel")

    return 0


if __name__ == "__main__":
    sys.exit(main())
