#!/usr/bin/env python3
"""End-to-end OTA on 1494 without polling during the block1 transfer (avoids
interfering with Leshan's stream). reset -> oneway write -> fixed wait ->
read state -> Execute -> wait reboot -> verify /3/0/3."""
import sys, time
sys.path.insert(0, "tools")
import fleet_common as fc
fc.bootstrap_venv()
import requests
from pathlib import Path

H, P = "192.168.8.111", 8090
DID = "8aa5c720-5180-11f1-be35-8f7a9e586a76"
BIN = "C:/Users/jsgir/Documents/ESP32/zephyrproject/build_ota/ami-lwm2m-node/zephyr/zephyr.signed.bin"
TARGET = "0.6.29"


def main():
    s = requests.Session()
    s.headers.update({"X-Authorization": "Bearer " + s.post(
        f"http://{H}:{P}/api/auth/login",
        json={"username": "tenant@thingsboard.org", "password": "tenant"}, timeout=10).json()["token"]})

    def rpc(method, params, oneway=False, t=30000):
        kind = "oneway" if oneway else "twoway"
        try:
            r = s.post(f"http://{H}:{P}/api/rpc/{kind}/{DID}",
                       json={"method": method, "params": params, "timeout": t},
                       timeout=(30 if oneway else t/1000+10))
            return r.json() if r.text else {}
        except Exception as e:
            return {"exc": str(e)[:60]}

    def rd(path):
        v = str(rpc("Read", {"id": path}, t=12000).get("value", ""))
        if "value=" in v:
            return v.split("value=")[1].split(",")[0].split("]")[0].strip()
        return v

    print(f"[e2e] start ver={rd('/3/0/3')} state={rd('/5/0/3')}", flush=True)
    data = Path(BIN).read_bytes()
    print(f"[e2e] image {len(data)}B (target {TARGET})", flush=True)

    rpc("WriteReplace", {"id": "/5/0/1", "value": ""}, t=10000)
    time.sleep(3)
    print(f"[e2e] reset, state={rd('/5/0/3')}", flush=True)

    t0 = time.time()
    rpc("WriteReplace", {"id": "/5/0/0", "value": data.hex()}, oneway=True, t=600000)
    print(f"[e2e] oneway write fired, transferring (no polling)...", flush=True)

    # fixed wait, then probe state (don't poll during transfer)
    deadline = time.time() + 420
    st = None
    while time.time() < deadline:
        time.sleep(45)
        st = rd("/5/0/3")
        print(f"[e2e] +{time.time()-t0:.0f}s state={st}", flush=True)
        if st == "2":
            break
    if st != "2":
        print(f"[e2e] NOT downloaded (state={st}); last result /5/0/5={rd('/5/0/5')}", flush=True)
        return 2
    print(f"[e2e] DOWNLOADED. Execute /5/0/2 ...", flush=True)
    rpc("Execute", {"id": "/5/0/2"}, oneway=True, t=20000)

    # wait reboot + swap + reregister
    print(f"[e2e] waiting for reboot/swap/reregister...", flush=True)
    deadline = time.time() + 240
    while time.time() < deadline:
        time.sleep(15)
        v = rd("/3/0/3")
        print(f"[e2e] [{time.strftime('%H:%M:%S')}] /3/0/3={v!r}", flush=True)
        if v == TARGET:
            print(f"\n[e2e] ✅ END-TO-END OTA SUCCESS — 1494 now runs {v}", flush=True)
            return 0
    print(f"[e2e] target {TARGET} not confirmed in window (node may still be swapping)", flush=True)
    return 4


if __name__ == "__main__":
    sys.exit(main())
