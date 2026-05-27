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
# build_dir selects the variant: "build_ota" (MED, default fleet) or
# "build_ota_ftd" (FTD router-eligible — flash ~5-10% of the fleet as routers).
def ota_artifacts(env: fc.ToolEnv, build_dir: str = "build_ota") -> tuple[Path, Path]:
    ws = env.west_workspace
    mcuboot = ws / build_dir / "mcuboot" / "zephyr" / "zephyr.bin"
    app = ws / build_dir / "ami-lwm2m-node" / "zephyr" / "zephyr.signed.bin"
    return mcuboot, app


def flash_ota(env: fc.ToolEnv, com: str, baud: str = "460800",
              build_dir: str = "build_ota",
              flash_mode: str = "dout", flash_freq: str = "20m") -> None:
    # NOTE on flash_mode/flash_freq defaults (dout/20m, NOT dio/40m):
    # The SuperMini's BOYA flash (mfr 0x46) varies unit-to-unit. With dio/40m
    # in the bootloader header, the ESP32-C6 BOOT ROM on marginal units reads
    # the flash unreliably -> "Checksum failure ... ets_main.c 331" -> TG0_WDT
    # boot-loop (soft-brick). esptool's own write+verify still passes (it reads
    # via the stub at safe speed), so the brick is invisible until first boot.
    # dout (single-data-line) + 20 MHz is the most compatible ROM read and
    # boots reliably on every unit tested. Override via --flash-mode/--flash-freq.
    mcuboot, app = ota_artifacts(env, build_dir)
    for p in (mcuboot, app):
        if not p.exists():
            raise SystemExit(
                f"OTA artifact missing: {p}\n"
                f"Build first with: west build --sysbuild --build-dir build_ota "
                f"-b xiao_esp32c6/esp32c6/hpcore <app> -- "
                f'-DEXTRA_CONF_FILE="med.conf;r1000.conf;ota.conf"'
            )
    # CRITICAL: full chip erase as a SEPARATE pre-step. write-flash --erase-all
    # in esptool 5.x respects partition table and was leaving the NVS partition
    # un-erased on this chip. Persistent NVS corruption was the root cause of:
    #   - "fleet goes mute overnight" (whole fleet stuck in fs_nvs init hang)
    #   - 15d8 Illegal-Instruction panic stack rooted in ot_setting_read_cb
    #   - Chronic "marginal" nodes that never recovered across reflashes
    # esptool erase-flash blindly nukes the entire 4 MB regardless of partition
    # table, so this is the only reliable way to guarantee a clean NVS.
    print(f"\n[ota-flash] {com}: full chip erase (nukes NVS too)")
    erase_cmd = ["python", "-m", "esptool",
                 "--chip", "esp32c6", "--port", com, "--baud", baud,
                 "--before", "default-reset", "--after", "hard-reset",
                 "erase-flash"]
    res = subprocess.run(erase_cmd, env=env.env_for_subprocess())
    if res.returncode != 0:
        raise RuntimeError(f"esptool erase-flash failed (rc={res.returncode})")

    cmd = ["python", "-m", "esptool",
           "--chip", "esp32c6", "--port", com, "--baud", baud,
           "--before", "default-reset", "--after", "hard-reset",
           "write-flash",
           "--flash-freq", flash_freq, "--flash-mode", flash_mode,
           "0x0", str(mcuboot),
           "0x20000", str(app)]
    print(f"[ota-flash] {com}: write mcuboot@0x0 + app.signed@0x20000 ({flash_freq} {flash_mode.upper()})")
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
    ap.add_argument("--build-dir", default="build_ota_ftd",
                    help="west build dir / variant. v0.6.38+ default = "
                         "build_ota_ftd (all-FTD: every node router-eligible so "
                         "the server can dynamically promote/demote via Object "
                         "33001 Thread Role Control). build_ota = MED (MTD "
                         "compile-time) only for power-constrained deployments.")
    ap.add_argument("--flash-mode", default="dout",
                    help="flash mode (default dout — most compatible; dio/40m "
                         "soft-bricks marginal BOYA units via ROM read failure)")
    ap.add_argument("--flash-freq", default="20m", help="flash freq (default 20m)")
    ap.add_argument("--map-csv", default="tools/mac_com_map.csv",
                    help="append COM<->MAC<->endpoint mapping here for record-keeping")
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

    # Record-keeping: tie this COM/MAC to its endpoint + variant so the fleet
    # (physically numbered 1..30) can be mapped after a flashing round.
    try:
        import csv as _csv, os as _os, datetime as _dt
        _new = not _os.path.exists(args.map_csv)
        with open(args.map_csv, "a", newline="") as _f:
            _w = _csv.writer(_f)
            if _new:
                _w.writerow(["ts_iso", "com", "mac", "endpoint", "variant", "flash"])
            _w.writerow([_dt.datetime.now().isoformat(timespec="seconds"), com, mac,
                         endpoint, args.build_dir, f"{args.flash_freq}/{args.flash_mode}"])
        print(f"[ota-migrate] mapping appended -> {args.map_csv}")
    except Exception as _e:
        print(f"[ota-migrate] map csv skip: {_e}")

    flash_ota(env, com, args.baud, args.build_dir, args.flash_mode, args.flash_freq)

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
