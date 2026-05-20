"""Re-provision TB Edge for the AMI LwM2M Thread fleet: device profile + the
7 node devices with NoSec LwM2M credentials.

Needed because a TB Edge re-install wipes the database — the AMI_LwM2M_Node
device profile and all device registrations are lost, so every node's
LwM2M Register is rejected with LwM2MAuthException and the fleet
reboot-loops. This script rebuilds exactly what the nodes need to register.

What it does (idempotent — safe to re-run):
  1. Creates/updates the "AMI_LwM2M_Node" device profile:
       - transport LWM2M, observe/telemetry/keyName for objects
         10242 (3-phase meter), 33000 (Thread+LwM2M diag), 3303 (SoC temp)
       - clientLwM2mSettings.defaultObjectIDVer pins the object versions
         (4=1.3, 10242=1.0, 33000=2.2, 3303=1.1) — without this TB Edge
         rejects observes with "Must be version" errors
  2. Creates the 7 node devices (ami-esp32c6-XXXX) under that profile
  3. Sets each device's credentials to LWM2M / NO_SEC, endpoint == device
     name (matches how the firmware registers)

After this, run tools/tb_edge_monitoring_setup.py to layer on the
alarms + rule chain + notifications + dashboard.

Usage:
    python tools/tb_edge_provision.py                 # provision r1000 edge
    python tools/tb_edge_provision.py --dry-run       # show plan, change nothing
    python tools/tb_edge_provision.py --nodes 1494,f7b4   # subset of nodes
"""
from __future__ import annotations

import argparse
import json
import sys

import fleet_common as fc

fc.bootstrap_venv()

import requests  # noqa: E402

PROFILE_NAME = "AMI_LwM2M_Node"

# The 7 fleet nodes — endpoint == device name == LwM2M NoSec identity.
ALL_NODES = ["1494", "f7b4", "fbb8", "14c8", "f6c8", "14bc", "1498"]
EP = "ami-esp32c6-{}"

# ── LwM2M observe config — reconstructed from the live AMI_LwM2M_Node
# profile captured before the TB Edge wipe. Objects:
#   10242 v1.0 — 3-phase power meter
#   33000 v2.2 — Thread + LwM2M diagnostics (RIDs 0..22)
#   3303  v1.1 — SoC die temperature (added v0.6.12)
KEY_NAME = {
    # --- Object 10242 v1.0 : 3-phase power meter ---
    "/10242_1.0/0/4": "voltage",
    "/10242_1.0/0/5": "current",
    "/10242_1.0/0/6": "activePower",
    "/10242_1.0/0/7": "reactivePower",
    "/10242_1.0/0/10": "apparentPower",
    "/10242_1.0/0/11": "powerFactor",
    "/10242_1.0/0/34": "activePower3P",
    "/10242_1.0/0/35": "reactivePower3P",
    "/10242_1.0/0/38": "apparentPower3P",
    "/10242_1.0/0/39": "powerFactor3P",
    "/10242_1.0/0/41": "activeEnergy",
    "/10242_1.0/0/42": "reactiveEnergy",
    "/10242_1.0/0/45": "apparentEnergy",
    "/10242_1.0/0/49": "frequency",
    # --- Object 33000 v2.2 : Thread + LwM2M diagnostics ---
    "/33000_1.0/0/0": "thread_role",
    "/33000_1.0/0/1": "thread_partition_id",
    "/33000_1.0/0/2": "mac_tx_total",
    "/33000_1.0/0/3": "mac_rx_total",
    "/33000_1.0/0/4": "mac_tx_unicast",
    "/33000_1.0/0/5": "mac_rx_unicast",
    "/33000_1.0/0/6": "mac_tx_broadcast",
    "/33000_1.0/0/7": "mac_rx_broadcast",
    "/33000_1.0/0/8": "mac_tx_err_abort",
    "/33000_1.0/0/9": "mac_rx_err_no_frame",
    "/33000_1.0/0/10": "uptime_s",
    "/33000_1.0/0/11": "reg_attempts",
    "/33000_1.0/0/12": "reg_success",
    "/33000_1.0/0/13": "notify_emitted",
    "/33000_1.0/0/14": "notify_throttled",
    "/33000_1.0/0/15": "recover_count",
    "/33000_1.0/0/16": "restart_success",
    "/33000_1.0/0/17": "last_error_code",
    "/33000_1.0/0/18": "last_error_uptime_s",
    "/33000_1.0/0/19": "watchdog_count",
    "/33000_1.0/0/20": "storm_backoff_applied",
    "/33000_1.0/0/21": "last_reset_reason",
    "/33000_1.0/0/22": "total_resets",
    # (v0.6.19 rollback) Firmware still populates RIDs 23-28 (post_mortem)
    # but advertises Object 33000 ver=2.2 so they're seen as opaque by
    # Leshan. Once the v2.4 schema path is validated end-to-end on an
    # isolated node, re-add these path/key mappings:
    #   23 hang_uptime_s   24 hang_heap_free   25 hang_heap_min_free
    #   26 hang_reg_age_s  27 hang_lwm2m_state 28 hang_thread_role
    # --- Object 3303 v1.1 : SoC die temperature ---
    "/3303_1.1/0/5700": "temperature",
    "/3303_1.1/0/5602": "temperature_max",
}

# Everything the firmware pushes is telemetry (nothing as TB attributes).
TELEMETRY = sorted(KEY_NAME.keys())

# Resources TB Edge actively sets an Observe on. The firmware emits Notifies
# for these; the rest still arrive as Register-payload telemetry.
OBSERVE = [
    "/10242_1.0/0/4", "/10242_1.0/0/5", "/10242_1.0/0/6",
    "/33000_1.0/0/17", "/33000_1.0/0/18", "/33000_1.0/0/19",
    "/33000_1.0/0/20", "/33000_1.0/0/21", "/33000_1.0/0/22",
    "/3303_1.1/0/5700", "/3303_1.1/0/5602",
]

# pmin/pmax write-attributes (seconds). Meter resources: 60 s floor.
# Diag/temp: slower. Reset counters: notify-on-change (pmin 0), hourly cap.
ATTRIBUTE_LWM2M = {}
for _p in ("/10242_1.0/0/4", "/10242_1.0/0/5", "/10242_1.0/0/6",
           "/10242_1.0/0/7", "/10242_1.0/0/10", "/10242_1.0/0/11",
           "/10242_1.0/0/34", "/10242_1.0/0/35", "/10242_1.0/0/38",
           "/10242_1.0/0/39", "/10242_1.0/0/41", "/10242_1.0/0/42",
           "/10242_1.0/0/45", "/10242_1.0/0/49"):
    ATTRIBUTE_LWM2M[_p] = {"pmin": 60, "pmax": 0}
for _p in ("/33000_1.0/0/17", "/33000_1.0/0/18", "/33000_1.0/0/19",
           "/33000_1.0/0/20"):
    ATTRIBUTE_LWM2M[_p] = {"pmin": 300, "pmax": 900}
for _p in ("/33000_1.0/0/21", "/33000_1.0/0/22"):
    ATTRIBUTE_LWM2M[_p] = {"pmin": 0, "pmax": 3600}
ATTRIBUTE_LWM2M["/3303_1.1/0/5700"] = {"pmin": 60, "pmax": 900}
ATTRIBUTE_LWM2M["/3303_1.1/0/5602"] = {"pmin": 300, "pmax": 3600}

# clientLwM2mSettings.defaultObjectIDVer — a SINGLE default object version
# string (e.g. "1.0"), NOT a per-object map. TB Edge's API stores it as a
# plain String and LwM2mClient.getObjectIDVerFromDeviceProfile() feeds that
# string straight into Leshan's Version() (which splits on '.' and demands
# exactly 2 parts). Passing a JSON map — string-encoded or as an object —
# both fail: the string form throws "version (...) MUST be composed of
# 2 parts", the object form is rejected by the API as a type mismatch.
# Per-object versions (33000=2.2, 4=1.3, 3303=1.1 ...) are not needed here:
# the node declares each object's version in its LwM2M Register payload
# (e.g. </33000>;ver=2.2), and TB Edge learns them from that. This value is
# only the fallback for objects that don't carry an explicit version.
DEFAULT_OBJECT_ID_VER = "1.0"


def build_profile_body() -> dict:
    return {
        "name": PROFILE_NAME,
        "description": "AMI LwM2M Node — Thread power meter + diagnostics. "
                       "Recreated by tools/tb_edge_provision.py.",
        "type": "DEFAULT",
        "transportType": "LWM2M",
        "provisionType": "DISABLED",
        "profileData": {
            "configuration": {"type": "DEFAULT"},
            "transportConfiguration": {
                "type": "LWM2M",
                "observeAttr": {
                    "keyName": KEY_NAME,
                    "observe": OBSERVE,
                    "attribute": [],
                    "telemetry": TELEMETRY,
                    "attributeLwm2m": ATTRIBUTE_LWM2M,
                    "observeStrategy": "SINGLE",
                },
                "bootstrapServerUpdateEnable": False,
                "bootstrap": [],
                "clientLwM2mSettings": {
                    # v0.6.27 OTA: our firmware (src/firmware_update.c) reports
                    # update State/Result via Object 5 RID 3 / RID 5 — the LwM2M
                    # native path — NOT Object 19 (Binary App Data Container).
                    # With useObject19ForOtaInfo=True, TB Edge would poll a
                    # nonexistent Object 19 for progress and the OTA state
                    # machine would stall. Set False so TB Edge reads /5/0/3 and
                    # /5/0/5 directly. fwUpdateStrategy=1 = PUSH: TB Edge writes
                    # the image block-wise to /5/0/0, which block_received_cb
                    # streams into slot1.
                    "useObject19ForOtaInfo": False,
                    "fwUpdateStrategy": 1,
                    "swUpdateStrategy": 1,
                    "clientOnlyObserveAfterConnect": 1,
                    "powerMode": "DRX",
                    "psmActivityTimer": 10000,
                    "edrxCycle": 81000,
                    "pagingTransmissionWindow": 10000,
                    "fwUpdateResource": None,
                    "swUpdateResource": None,
                    "defaultObjectIDVer": DEFAULT_OBJECT_ID_VER,
                },
            },
            "provisionConfiguration": {"type": "DISABLED",
                                       "provisionDeviceSecret": None},
            "alarms": None,
        },
    }


class TB:
    def __init__(self, base, user, password):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        r = self.s.post(f"{self.base}/api/auth/login",
                        json={"username": user, "password": password}, timeout=15)
        r.raise_for_status()
        self.s.headers.update({"Authorization": f"Bearer {r.json()['token']}",
                               "Content-Type": "application/json"})

    def get(self, path, params=None):
        r = self.s.get(f"{self.base}{path}", params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    def post(self, path, body):
        r = self.s.post(f"{self.base}{path}", data=json.dumps(body), timeout=30)
        if not r.ok:
            raise RuntimeError(f"POST {path} -> {r.status_code}: {r.text[:400]}")
        return r.json() if r.text else {}


def apply_profile(tb: TB, dry: bool) -> str:
    profiles = tb.get("/api/deviceProfiles", {"pageSize": 100, "page": 0})["data"]
    existing = next((p for p in profiles if p["name"] == PROFILE_NAME), None)
    body = build_profile_body()
    if existing:
        full = tb.get(f"/api/deviceProfile/{existing['id']['id']}")
        body["id"] = full["id"]
        # preserve any alarms already on the profile (monitoring setup owns them)
        body["profileData"]["alarms"] = full["profileData"].get("alarms")
        action = "update"
    else:
        action = "create"
    print(f"[profile] '{PROFILE_NAME}': {action}  "
          f"(LWM2M, {len(OBSERVE)} observed / {len(TELEMETRY)} telemetry keys, "
          f"objs 10242+33000+3303)")
    if dry:
        return existing["id"]["id"] if existing else "<dry-run>"
    res = tb.post("/api/deviceProfile", body)
    pid = res["id"]["id"]
    print(f"[profile] OK  id={pid}")
    return pid


def provision_device(tb: TB, endpoint: str, profile_id: str, dry: bool):
    # find existing (TB Edge sometimes mangles names; match by exact name)
    found = tb.get("/api/tenant/deviceInfos",
                   {"pageSize": 100, "page": 0, "textSearch": endpoint})["data"]
    dev = next((d for d in found if d["name"] == endpoint), None)

    if dev:
        dev_id = dev["id"]["id"]
        state = "exists"
    else:
        state = "create"
    print(f"[device]  {endpoint:22s} {state}", end="")
    if dry:
        print("  (dry-run)")
        return
    if not dev:
        dev = tb.post("/api/device", {
            "name": endpoint, "label": endpoint,
            "deviceProfileId": {"entityType": "DEVICE_PROFILE", "id": profile_id},
        })
        dev_id = dev["id"]["id"]

    # set LwM2M NoSec credentials (endpoint == identity)
    creds_value = json.dumps({
        "client": {"securityConfigClientMode": "NO_SEC", "endpoint": endpoint},
        "bootstrap": {
            "bootstrapServer": {"securityMode": "NO_SEC"},
            "lwm2mServer": {"securityMode": "NO_SEC"},
        },
    })
    existing_creds = tb.get(f"/api/device/{dev_id}/credentials")
    tb.post("/api/device/credentials", {
        "id": existing_creds.get("id"),
        "deviceId": {"entityType": "DEVICE", "id": dev_id},
        "credentialsType": "LWM2M_CREDENTIALS",
        "credentialsId": endpoint,
        "credentialsValue": creds_value,
    })
    print("  -> LWM2M / NO_SEC creds set OK")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mesh", default=fc.DEFAULT_MESH, choices=fc.MESH_TARGETS)
    ap.add_argument("--user", default=fc.EDGE_TENANT_USER)
    ap.add_argument("--password", default=fc.EDGE_TENANT_PASS)
    ap.add_argument("--nodes", default=",".join(ALL_NODES),
                    help="comma-separated node suffixes (default: all 7)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    host, port = fc.edge_for_mesh(args.mesh)
    base = f"http://{host}:{port}"
    nodes = [n.strip() for n in args.nodes.split(",") if n.strip()]
    print(f"=== TB Edge provision -> {base}  "
          f"{'(DRY RUN)' if args.dry_run else ''} ===")
    print(f"    profile: {PROFILE_NAME}   nodes: {nodes}")

    tb = TB(base, args.user, args.password)
    try:
        pid = apply_profile(tb, args.dry_run)
        for suffix in nodes:
            provision_device(tb, EP.format(suffix), pid, args.dry_run)
    except Exception as e:
        print(f"FAILED: {e}")
        return 1
    print("=== done — nodes should register within ~1-2 boot cycles ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
