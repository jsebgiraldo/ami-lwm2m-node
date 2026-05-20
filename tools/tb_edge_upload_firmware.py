#!/usr/bin/env python3
"""Upload a signed firmware image to TB Edge as an OTA package and (optionally)
assign it to a device or the whole AMI_LwM2M_Node profile.

TB Edge then pushes it over LwM2M Object 5 (fwUpdateStrategy=1 = PUSH:
block-wise write to /5/0/0) to every assigned device whose reported
firmware version differs from the package version.

Two-step ThingsBoard OTA API:
  1. POST /api/otaPackage          -> create package metadata (returns id)
  2. POST /api/otaPackage/{id}?... -> multipart upload of the binary

Then set device.firmwareId (or deviceProfile.firmwareId) = package id.

Usage:
    # upload + assign to one device
    python tools/tb_edge_upload_firmware.py --version 0.6.27 --assign-device ami-esp32c6-1494

    # upload + assign to the whole profile (fleet-wide OTA)
    python tools/tb_edge_upload_firmware.py --version 0.6.27 --assign-profile

    # just upload, assign later from the UI
    python tools/tb_edge_upload_firmware.py --version 0.6.27
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import requests

import fleet_common as fc

fc.bootstrap_venv()

DEFAULT_BIN = (Path(fc.detect_env(verbose=False).west_workspace)
               / "build_ota" / "ami-lwm2m-node" / "zephyr" / "zephyr.signed.bin")
PROFILE_NAME = fc.EDGE_PROFILE


class TB:
    def __init__(self, base, user, password):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        r = self.s.post(f"{self.base}/api/auth/login",
                        json={"username": user, "password": password}, timeout=15)
        r.raise_for_status()
        self.s.headers.update({"X-Authorization": f"Bearer {r.json()['token']}"})

    def get(self, path, **params):
        r = self.s.get(f"{self.base}{path}", params=params or None, timeout=20)
        r.raise_for_status()
        return r.json()

    def post_json(self, path, body):
        r = self.s.post(f"{self.base}{path}", json=body, timeout=30)
        if not r.ok:
            raise RuntimeError(f"POST {path} -> {r.status_code}: {r.text[:400]}")
        return r.json() if r.text else {}

    def profile_id(self, name):
        for p in self.get("/api/deviceProfiles", pageSize=100, page=0)["data"]:
            if p["name"] == name:
                return p["id"]["id"]
        raise SystemExit(f"profile not found: {name}")

    def device(self, name):
        data = self.get("/api/tenant/devices", pageSize=1, page=0, textSearch=name)["data"]
        if not data:
            raise SystemExit(f"device not found: {name}")
        return data[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True, help="OTA package version, e.g. 0.6.27")
    ap.add_argument("--title", default="ami-lwm2m-node")
    ap.add_argument("--bin", default=str(DEFAULT_BIN), help="signed firmware .bin")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--user", default=fc.EDGE_TENANT_USER)
    ap.add_argument("--password", default=fc.EDGE_TENANT_PASS)
    ap.add_argument("--mesh", default=fc.DEFAULT_MESH, choices=fc.MESH_TARGETS)
    ap.add_argument("--assign-device", default=None, help="endpoint to assign, e.g. ami-esp32c6-1494")
    ap.add_argument("--assign-profile", action="store_true", help="assign to whole profile (fleet OTA)")
    args = ap.parse_args()

    host, port = fc.edge_for_mesh(args.mesh)
    if args.host: host = args.host
    if args.port: port = args.port

    binpath = Path(args.bin)
    if not binpath.exists():
        raise SystemExit(f"firmware not found: {binpath}\nBuild the OTA image first (build_ota).")
    data = binpath.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    print(f"[fw] file={binpath.name} size={len(data)} sha256={sha256[:16]}...")

    tb = TB(f"http://{host}:{port}", args.user, args.password)
    prof_id = tb.profile_id(PROFILE_NAME)

    # 1. create package metadata
    info = tb.post_json("/api/otaPackage", {
        "deviceProfileId": {"entityType": "DEVICE_PROFILE", "id": prof_id},
        "type": "FIRMWARE",
        "title": args.title,
        "version": args.version,
        "tag": f"{args.title}-{args.version}",
        "usesUrl": False,
        "additionalInfo": {"description": f"AMI node firmware {args.version} (MCUboot signed)"},
    })
    pkg_id = info["id"]["id"]
    print(f"[fw] package created: id={pkg_id} title={args.title} version={args.version}")

    # 2. upload the binary (multipart)
    url = (f"http://{host}:{port}/api/otaPackage/{pkg_id}"
           f"?checksumAlgorithm=SHA256&checksum={sha256}")
    files = {"file": (binpath.name, data, "application/octet-stream")}
    r = tb.s.post(url, files=files, timeout=120)
    if not r.ok:
        raise RuntimeError(f"binary upload failed {r.status_code}: {r.text[:400]}")
    print(f"[fw] binary uploaded OK ({len(data)} bytes)")

    # 3. assignment
    if args.assign_profile:
        full = tb.get(f"/api/deviceProfile/{prof_id}")
        full["firmwareId"] = {"entityType": "OTA_PACKAGE", "id": pkg_id}
        tb.post_json("/api/deviceProfile", full)
        print(f"[fw] assigned to PROFILE {PROFILE_NAME} — fleet-wide OTA armed")
    elif args.assign_device:
        dev = tb.device(args.assign_device)
        dev["firmwareId"] = {"entityType": "OTA_PACKAGE", "id": pkg_id}
        tb.post_json("/api/device", dev)
        print(f"[fw] assigned to DEVICE {args.assign_device} — OTA armed")
    else:
        print(f"[fw] uploaded but NOT assigned. Assign from UI or re-run with "
              f"--assign-device / --assign-profile.")

    print(f"\n[fw] done. TB Edge will push to assigned device(s) whose reported "
          f"fw version != {args.version}. Watch:")
    print(f"     docker logs -f pi4-edge-v2 | grep -iE 'fw|firmware|/5/0'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
