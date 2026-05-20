#!/usr/bin/env python3
"""ONE-TIME USB migration to the MCUboot/OTA firmware (v0.6.27+).

After this, all future updates go over-the-air via TB Edge Object 5 — no
more per-node USB reflashing.

What it does, per node:
  1. Read MAC via esptool -> derive endpoint ami-esp32c6-XXXX
  2. esptool write-flash (40 MHz DIO, erase-all):
        0x0      <- build_ota/mcuboot/zephyr/zephyr.bin       (MCUboot)
        0x20000  <- build_ota/.../zephyr/zephyr.signed.bin    (signed app, slot0)
  3. Provision the device + LwM2M creds in TB Edge (idempotent)
  4. Hard-reset, wait for telemetry, confirm Active

CRITICAL: this REPLACES the monolithic-at-0x0 layout. A node migrated here
boots MCUboot -> slot0. Do NOT mix with tools/onboard_node.py (which flashes
the legacy monolithic image at 0x0) on the same board afterwards unless you
re-migrate.

The two build_ota artifacts come from:
  cd <zephyrproject>
  PATH=<venv>/Scripts:$PATH \
  west build --sysbuild --build-dir build_ota -b xiao_esp32c6/esp32c6/hpcore \
    <app> -- -DEXTRA_CONF_FILE="med.conf;r1000.conf;ota.conf"

Usage:
    python tools/flash_ota_migrate.py --com COM17
    python tools/flash_ota_migrate.py --com COM17 --skip-provision
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time as _time
from pathlib import Path

import fleet_common as fc

fc.bootstrap_venv()
sys.path.insert(0, str(fc.TOOLS_DIR))
from provision_node import TBClient, provision_single  # noqa: E402

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# build_ota lives in the west workspace next to the legacy build dirs.
def ota_artifacts(env: fc.ToolEnv) -> tuple[Path, Path]:
    ws = env.west_workspace
    mcuboot = ws / "build_ota" / "mcuboot" / "zephyr" / "zephyr.bin"
    app = ws / "build_ota" / "ami-lwm2m-node" / "zephyr" / "zephyr.signed.bin"
    return mcuboot, app


def flash_ota(env: fc.ToolEnv, com: str, baud: str = "460800") -> None:
    mcuboot, app = ota_artifacts(env)
    for p in (mcuboot, app):
        if not p.exists():
            raise SystemExit(
                f"OTA artifact missing: {p}\n"
                f"Build first with: west build --sysbuild --build-dir build_ota "
                f"-b xiao_esp32c6/esp32c6/hpcore <app> -- "
                f'-DEXTRA_CONF_FILE="med.conf;r1000.conf;ota.conf"'
            )
    cmd = ["python", "-m", "esptool",
           "--chip", "esp32c6", "--port", com, "--baud", baud,
           "--before", "default-reset", "--after", "hard-reset",
           "write-flash", "--erase-all",
           "--flash-freq", "40m", "--flash-mode", "dio",
           "0x0", str(mcuboot),
           "0x20000", str(app)]
    print(f"\n[ota-flash] {com}: mcuboot@0x0 + app.signed@0x20000 (40m DIO)")
    print(f"  mcuboot: {mcuboot.stat().st_size} B")
    print(f"  app:     {app.stat().st_size} B")
    res = subprocess.run(cmd, env=env.env_for_subprocess())
    if res.returncode != 0:
        raise RuntimeError(f"esptool write-flash failed (rc={res.returncode})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--com", required=True, help="COM port of the node")
    ap.add_argument("--baud", default="460800")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--user", default=fc.EDGE_TENANT_USER)
    ap.add_argument("--password", default=fc.EDGE_TENANT_PASS)
    ap.add_argument("--profile", default=fc.EDGE_PROFILE)
    ap.add_argument("--mesh", default=fc.DEFAULT_MESH, choices=fc.MESH_TARGETS)
    ap.add_argument("--skip-provision", action="store_true")
    ap.add_argument("--verify-timeout", type=int, default=180)
    args = ap.parse_args()

    mesh_host, mesh_port = fc.edge_for_mesh(args.mesh)
    if args.host is None:
        args.host = mesh_host
    if args.port is None:
        args.port = mesh_port

    env = fc.detect_env(verbose=False)
    com = args.com

    print(f"[ota-migrate] {com}  mesh={args.mesh}  Edge=http://{args.host}:{args.port}")
    mac = fc.read_mac(env, com)
    endpoint = fc.mac_to_endpoint(mac)
    print(f"[ota-migrate] mac={mac}  endpoint={endpoint}")

    flash_ota(env, com, args.baud)

    if not args.skip_provision:
        tb = TBClient(args.host, args.port, args.user, args.password)
        tb.login()
        provision_single(tb, endpoint, args.profile, dry_run=False)

    try:
        fc.hard_reset(com, label="post-OTA-flash reset")
    except Exception as e:
        print(f"[ota-migrate] post-flash reset skipped ({e})")
    _time.sleep(6)

    if not args.skip_provision:
        print(f"[ota-migrate] waiting up to {args.verify_timeout}s for telemetry...")
        ok, sensors = _wait_telemetry(tb, endpoint, args.verify_timeout)
        if ok:
            print(f"\n[ota-migrate] {endpoint}  OK (MCUboot)  sensors={sensors[:6]}")
            return 0
        print(f"\n[ota-migrate] {endpoint}  FLASHED but no telemetry yet "
              f"(check mesh/Edge)")
        return 1
    print(f"\n[ota-migrate] {endpoint}  FLASHED (provision skipped)")
    return 0


def _wait_telemetry(tb, endpoint, timeout_s):
    deadline = _time.time() + timeout_s
    last = ""
    while _time.time() < deadline:
        try:
            dev = tb.find_device_by_name(endpoint)
            if dev:
                did = dev["id"]["id"]
                tel = tb.get_latest_telemetry(did) or {}
                sensors = sorted(k for k in tel.keys() if k != "transportLog")
                state = f"active={dev.get('active')} sensors={len(sensors)}"
                if state != last:
                    print(f"  [verify] {state}")
                    last = state
                if dev.get("active") and sensors:
                    return True, sensors
        except Exception as e:
            print(f"  [verify] error: {e}")
        _time.sleep(5)
    return False, []


if __name__ == "__main__":
    sys.exit(main())
