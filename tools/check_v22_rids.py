"""Read v2.2 diagnostic RIDs (17-20) from Object 33000 v2.2 on the
provided endpoints via TB Edge two-way RPC.

  RID 17 = last_error_code         (S32)  — last LwM2M errno (negative)
  RID 18 = last_error_uptime_s     (U32)
  RID 19 = watchdog_count          (U32)  — times the liveness watchdog fired
  RID 20 = storm_backoff_applied   (U32)  — times NETWORK_ERROR doubled backoff

Usage:
    python tools/check_v22_rids.py ami-esp32c6-1494 ami-esp32c6-f7b4
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import fleet_common as fc
fc.bootstrap_venv()

import requests  # noqa: E402

OBJ_PATH = "/33000_2.2/0"
RIDS = {
    # v2.1 golden (control: should be U32)
    10: "uptime_s",
    11: "reg_attempts",
    12: "reg_success",
    13: "notify_emitted",
    14: "notify_throttled",
    15: "recover_count",
    16: "restart_success",
    # v2.2 (new)
    17: "last_error_code",
    18: "last_error_uptime_s",
    19: "watchdog_count",
    20: "storm_backoff_applied",
}


class Tb:
    def __init__(self, host: str, port: int, user: str, password: str):
        self.base = f"http://{host}:{port}"
        self.s = requests.Session()
        r = self.s.post(f"{self.base}/api/auth/login",
                        json={"username": user, "password": password}, timeout=10)
        r.raise_for_status()
        self.s.headers.update({"Authorization": f"Bearer {r.json()['token']}",
                               "Content-Type": "application/json"})

    def device_id(self, name: str) -> str:
        r = self.s.get(f"{self.base}/api/tenant/deviceInfos",
                       params={"pageSize": 100, "page": 0, "textSearch": name}, timeout=10)
        r.raise_for_status()
        for d in r.json().get("data", []):
            if d["name"].startswith(name) or d.get("label") == name:
                return d["id"]["id"]
        raise SystemExit(f"device '{name}' not found")

    def rpc_read(self, dev_id: str, path: str, timeout_s: int = 15) -> dict:
        body = {"method": "Read", "params": {"id": path}, "timeout": timeout_s * 1000}
        r = self.s.post(f"{self.base}/api/rpc/twoway/{dev_id}", json=body,
                        timeout=timeout_s + 5)
        r.raise_for_status()
        return r.json()


def parse_value(resp: dict) -> object:
    """TB Edge LwM2M-RPC returns the resource value under 'value' (with type info).
    Tolerate the multiple shapes seen across Edge versions."""
    if not isinstance(resp, dict):
        return resp
    if "value" in resp:
        v = resp["value"]
        if isinstance(v, dict) and "value" in v:
            return v["value"]
        return v
    if "result" in resp:
        return resp["result"]
    return resp


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("endpoints", nargs="+")
    ap.add_argument("--mesh", default=fc.DEFAULT_MESH, choices=fc.MESH_TARGETS)
    ap.add_argument("--user", default=fc.EDGE_TENANT_USER)
    ap.add_argument("--password", default=fc.EDGE_TENANT_PASS)
    args = ap.parse_args()

    host, port = fc.edge_for_mesh(args.mesh)
    print(f"[v22-rids] Edge=http://{host}:{port}  mesh={args.mesh}")
    tb = Tb(host, port, args.user, args.password)

    overall_ok = True
    for ep in args.endpoints:
        print(f"\n=== {ep} ===")
        try:
            dev_id = tb.device_id(ep)
        except SystemExit as e:
            print(f"  [SKIP] {e}")
            overall_ok = False
            continue

        for rid, label in RIDS.items():
            path = f"{OBJ_PATH}/{rid}"
            try:
                t0 = time.time()
                resp = tb.rpc_read(dev_id, path, timeout_s=15)
                elapsed = time.time() - t0
                val = parse_value(resp)
                print(f"  [{rid:>2} {label:24s}] = {val!r:>14}   ({elapsed:.2f}s)")
            except requests.HTTPError as e:
                body = e.response.text[:200] if e.response is not None else ""
                print(f"  [{rid:>2} {label:24s}]  HTTP {e.response.status_code if e.response else '??'}  {body}")
                overall_ok = False
            except Exception as e:
                print(f"  [{rid:>2} {label:24s}]  ERR {type(e).__name__}: {e}")
                overall_ok = False
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
