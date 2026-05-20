#!/usr/bin/env python3
"""Direct LwM2M firmware OTA push, bypassing TB Edge's (absent) FOTA engine.

TB Edge does NOT drive LwM2M Object-5 firmware updates (the OTA orchestration is
a Cloud-only feature; the Edge just proxies). But TB Edge embeds Leshan and
exposes a generic LwM2M RPC over the device's *existing* registered connection.
So we push the image ourselves:

  1. Reset Object-5 state to Idle      (WriteReplace /5/0/1 = "")
  2. ONEWAY WriteReplace /5/0/0 = hex(image)
     -> Leshan block1-transfers the opaque to the device; firmware streams it
        into MCUboot slot1 (image-1). ONEWAY avoids the ~30 s Tomcat async-RPC
        timeout that kills two-way RPC on large payloads.
  3. Poll /5/0/3 State until 2 (Downloaded).
  4. Execute /5/0/2 -> firmware boot_request_upgrade(TEST) + cold reboot.
  5. Wait for re-registration and confirm /3/0/3 (Firmware Version) == target.

The node only accepts downlink from its registered server (its socket is
connect()'d to the Edge), which is exactly why this must go through the Edge's
Leshan rather than a raw CoAP client on the Pi4.

Usage:
    python tools/ota_push_direct.py --device ami-esp32c6-1494 --version 0.6.28
    python tools/ota_push_direct.py --device ami-esp32c6-1494 --version 0.6.28 --no-execute
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import requests

import fleet_common as fc

fc.bootstrap_venv()

EDGE_HOST, EDGE_PORT = "192.168.8.111", 8090
USER, PASS = "tenant@thingsboard.org", "tenant"
DEFAULT_BIN = (Path(fc.detect_env(verbose=False).west_workspace)
               / "build_ota" / "ami-lwm2m-node" / "zephyr" / "zephyr.signed.bin")


class Edge:
    def __init__(self, host, port, user, password):
        self.base = f"http://{host}:{port}"
        self.s = requests.Session()
        r = self.s.post(f"{self.base}/api/auth/login",
                        json={"username": user, "password": password}, timeout=15)
        r.raise_for_status()
        self.s.headers.update({"X-Authorization": f"Bearer {r.json()['token']}"})

    def device_id(self, endpoint):
        d = self.s.get(f"{self.base}/api/tenant/devices",
                       params={"pageSize": 1, "page": 0, "textSearch": endpoint},
                       timeout=20).json()["data"]
        if not d:
            raise SystemExit(f"device not found: {endpoint}")
        return d[0]["id"]["id"]

    def rpc(self, did, method, params, oneway=False, timeout_ms=30000):
        kind = "oneway" if oneway else "twoway"
        r = self.s.post(f"{self.base}/api/rpc/{kind}/{did}",
                        json={"method": method, "params": params, "timeout": timeout_ms},
                        timeout=(timeout_ms / 1000 + 15) if not oneway else 30)
        try:
            return r.json() if r.text else {}
        except Exception:
            return {"http": r.status_code, "text": r.text[:200]}

    def read_int(self, did, path):
        r = self.rpc(did, "Read", {"id": path}, timeout_ms=15000)
        v = r.get("value", "")
        # "LwM2mSingleResource [id=3, value=2, type=INTEGER]"
        if "value=" in str(v):
            try:
                return int(str(v).split("value=")[1].split(",")[0].split("]")[0])
            except ValueError:
                return None
        return None

    def read_str(self, did, path):
        r = self.rpc(did, "Read", {"id": path}, timeout_ms=15000)
        v = str(r.get("value", ""))
        if "value=" in v:
            raw = v.split("value=")[1].split(",")[0].split("]")[0].strip()
            # /3/0/3 comes back hex-encoded for OPAQUE-typed reads
            try:
                return bytes.fromhex(raw).decode("ascii", "replace") if raw else ""
            except ValueError:
                return raw
        return v


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", required=True, help="endpoint, e.g. ami-esp32c6-1494")
    ap.add_argument("--version", required=True, help="expected target version, e.g. 0.6.28")
    ap.add_argument("--bin", default=str(DEFAULT_BIN))
    ap.add_argument("--no-execute", action="store_true",
                    help="push to slot1 but do not Execute /5/0/2 (no reboot)")
    ap.add_argument("--dl-timeout", type=int, default=400, help="max seconds for download")
    args = ap.parse_args()

    binpath = Path(args.bin)
    if not binpath.exists():
        raise SystemExit(f"firmware not found: {binpath}")
    data = binpath.read_bytes()
    hexval = data.hex()
    print(f"[ota] {binpath.name}  size={len(data)}B  hex={len(hexval)} chars")

    e = Edge(EDGE_HOST, EDGE_PORT, USER, PASS)
    did = e.device_id(args.device)
    print(f"[ota] device {args.device} id={did}")
    print(f"[ota] current /3/0/3 = {e.read_str(did, '/3/0/3')!r}  state /5/0/3 = {e.read_int(did, '/5/0/3')}")

    # 1. reset Object-5 to Idle
    e.rpc(did, "WriteReplace", {"id": "/5/0/1", "value": ""}, timeout_ms=10000)
    time.sleep(3)
    st = e.read_int(did, "/5/0/3")
    print(f"[ota] state after reset = {st}")

    # 2. oneway block-write the image into slot1
    print(f"[ota] pushing image (oneway WriteReplace /5/0/0)...")
    t0 = time.time()
    r = e.rpc(did, "WriteReplace", {"id": "/5/0/0", "value": hexval}, oneway=True, timeout_ms=600000)
    print(f"[ota] oneway accepted in {time.time()-t0:.1f}s: {r or 'OK'}")

    # 3. poll State until Downloaded (2) or Idle-with-error
    deadline = time.time() + args.dl_timeout
    last = None
    while time.time() < deadline:
        st = e.read_int(did, "/5/0/3")
        if st != last:
            print(f"[ota] [{time.strftime('%H:%M:%S')}] state={st} ({_state(st)})  +{time.time()-t0:.0f}s")
            last = st
        if st == 2:
            res = e.read_int(did, "/5/0/5")
            print(f"[ota] DOWNLOADED. UpdateResult /5/0/5 = {res}")
            break
        if st == 0 and time.time() - t0 > 30:
            print(f"[ota] state returned to Idle unexpectedly (result /5/0/5={e.read_int(did,'/5/0/5')}) — push may have failed")
            return 2
        time.sleep(8)
    else:
        print("[ota] TIMEOUT waiting for Downloaded")
        return 3

    if args.no_execute:
        print("[ota] --no-execute: stopping before Update. slot1 holds the image.")
        return 0

    # 4. Execute /5/0/2 -> upgrade + reboot
    print("[ota] Execute /5/0/2 (apply + reboot)...")
    e.rpc(did, "Execute", {"id": "/5/0/2"}, oneway=True, timeout_ms=20000)

    # 5. wait for reboot + re-register at target version
    print(f"[ota] waiting for reboot + re-register reporting {args.version} ...")
    deadline = time.time() + 240
    while time.time() < deadline:
        time.sleep(12)
        ver = e.read_str(did, "/3/0/3")
        print(f"[ota] [{time.strftime('%H:%M:%S')}] /3/0/3 = {ver!r}")
        if ver == args.version:
            print(f"\n[ota] ✅ OTA COMPLETE — {args.device} now reports {ver}")
            return 0
    print("[ota] target version not confirmed within window (node may still be swapping)")
    return 4


def _state(s):
    return {0: "Idle", 1: "Downloading", 2: "Downloaded", 3: "Updating"}.get(s, "?")


if __name__ == "__main__":
    sys.exit(main())
