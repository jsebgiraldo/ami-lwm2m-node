#!/usr/bin/env python3
"""End-to-end onboarding of a single AMI node.

Steps:
  1. Detect or accept a COM port (ESP32-C6 native USB)
  2. Read MAC via esptool, derive endpoint ami-esp32c6-XXXX
  3. (Skip if same fw_sha already deployed for that endpoint)
  4. west flash --esp-device COMx
  5. Provision device + LwM2M creds in TB Edge (idempotent)
  6. Poll TB until Active=True and at least one sensor key arrives
  7. Append result to tools/deployment_ledger.csv

Designed to be re-runnable. Used standalone or driven by batch_onboard.py.

Examples:
    python tools/onboard_node.py                       # auto-detect single ESP32-C6
    python tools/onboard_node.py --com COM8
    python tools/onboard_node.py --com COM8 --skip-flash --verify-only
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time as _time

import fleet_common as fc

fc.bootstrap_venv()

# ensure we can import the existing TBClient
sys.path.insert(0, str(fc.TOOLS_DIR))
from provision_node import TBClient, provision_single  # noqa: E402

os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def pick_com(arg_com: str | None) -> str:
    if arg_com:
        return arg_com
    ports = fc.list_esp32c6_ports()
    if not ports:
        raise SystemExit("No ESP32-C6 native USB port found. Plug a board or pass --com.")
    if len(ports) > 1:
        msg = ", ".join(p.com for p in ports)
        raise SystemExit(f"Multiple ESP32-C6 ports detected ({msg}). Pass --com explicitly.")
    return ports[0].com


def west_flash(env: fc.ToolEnv, com: str, variant: str = fc.DEFAULT_VARIANT,
               mesh: str = fc.DEFAULT_MESH, baud: str = "460800") -> None:
    west = env.venv_scripts / "west.exe"
    bdir = fc.build_dir(env, variant, mesh)
    cmd = [str(west), "flash", "--build-dir", str(bdir),
           "--esp-device", com, "--esp-baud-rate", baud]
    print(f"\n[flash] variant={variant} mesh={mesh} {com} @ {baud}")
    res = subprocess.run(cmd, cwd=str(env.west_workspace), env=env.env_for_subprocess())
    if res.returncode != 0:
        raise RuntimeError(f"west flash failed (rc={res.returncode})")


def wait_for_telemetry(tb: TBClient, endpoint: str, timeout_s: int = 120) -> tuple[bool, list[str]]:
    """Poll TB until the device is Active and has at least one non-log telemetry key."""
    deadline = _time.time() + timeout_s
    last_state = ""
    while _time.time() < deadline:
        try:
            dev = tb.find_device_by_name(endpoint)
            if dev:
                did = dev["id"]["id"]
                tel = tb.get_latest_telemetry(did) or {}
                sensors = sorted(k for k in tel.keys() if k != "transportLog")
                state = f"active={dev.get('active')} sensors={len(sensors)}"
                if state != last_state:
                    print(f"  [verify] {state}")
                    last_state = state
                if dev.get("active") and sensors:
                    return True, sensors
        except Exception as e:
            print(f"  [verify] error: {e}")
        _time.sleep(5)
    return False, []


def find_existing_endpoint(endpoint: str) -> dict | None:
    """Look up endpoint in our local ledger (latest record only)."""
    rows = [r for r in fc.ledger_read() if r.get("endpoint") == endpoint]
    return rows[-1] if rows else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--com", default=None, help="COM port (auto-detected if a single ESP32-C6 is connected)")
    ap.add_argument("--baud", default="460800")
    # --host/--port default to None so we can auto-resolve from --mesh; if user
    # passes them explicitly we keep their value.
    ap.add_argument("--host", default=None,
                    help="TB Edge host. Default: derived from --mesh.")
    ap.add_argument("--port", type=int, default=None,
                    help="TB Edge port. Default: derived from --mesh.")
    ap.add_argument("--user", default=fc.EDGE_TENANT_USER)
    ap.add_argument("--password", default=fc.EDGE_TENANT_PASS)
    ap.add_argument("--profile", default=fc.EDGE_PROFILE)
    ap.add_argument("--variant", default=fc.DEFAULT_VARIANT, choices=fc.VARIANTS,
                    help=f"Firmware variant to flash. Default: {fc.DEFAULT_VARIANT} "
                         "(MED). Use 'ftd' for router-eligible nodes.")
    ap.add_argument("--mesh", default=fc.DEFAULT_MESH, choices=fc.MESH_TARGETS,
                    help=f"Target Thread mesh. Default: {fc.DEFAULT_MESH}. "
                         "Selects build artifact (build_<variant>[_<mesh>]) and "
                         "TB Edge host unless --host is overridden.")
    ap.add_argument("--skip-flash", action="store_true")
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--verify-timeout", type=int, default=180,
                    help="seconds to wait for first telemetry (boards new to TB Edge "
                         "may need 120-180 s for Thread attach + register + first push)")
    ap.add_argument("--force-reflash", action="store_true",
                    help="Reflash even if same fw_sha is recorded for this endpoint")
    ap.add_argument("--no-erase", action="store_true",
                    help="Skip esptool erase-flash. Default: erase before first flash of a board "
                         "not yet in the ledger (factory-fresh boards otherwise often fail to boot).")
    args = ap.parse_args()

    # Resolve TB Edge host/port from --mesh if user didn't override.
    mesh_host, mesh_port = fc.edge_for_mesh(args.mesh)
    if args.host is None:
        args.host = mesh_host
    if args.port is None:
        args.port = mesh_port

    env = fc.detect_env(verbose=False)
    com = pick_com(args.com)
    print(f"[onboard] target COM={com}  variant={args.variant}  mesh={args.mesh}  "
          f"Edge=http://{args.host}:{args.port}  profile={args.profile}")

    # 1. MAC + endpoint
    print(f"[onboard] reading MAC on {com}...")
    mac = fc.read_mac(env, com)
    endpoint = fc.mac_to_endpoint(mac)
    fw_sha = fc.firmware_hash(env, args.variant, args.mesh)
    print(f"[onboard] mac={mac}  endpoint={endpoint}  fw_sha={fw_sha or '(no build)'}")

    notes = []

    # 2. Skip if same fw_sha + variant + mesh on same endpoint already deployed
    if not args.force_reflash and not args.verify_only and not args.skip_flash:
        prev = find_existing_endpoint(endpoint)
        if (prev
                and prev.get("fw_sha") == fw_sha
                and prev.get("variant", fc.DEFAULT_VARIANT) == args.variant
                and prev.get("mesh", fc.DEFAULT_MESH) == args.mesh
                and prev.get("status", "").startswith("ok")):
            print(f"[onboard] endpoint already deployed with same fw_sha+variant+mesh — skipping flash")
            args.skip_flash = True
            notes.append("skip-flash:ledger-match")

    # 3. Flash (with optional pre-erase)
    if not (args.skip_flash or args.verify_only):
        if not fc.firmware_bin(env, args.variant, args.mesh).exists():
            raise SystemExit(
                f"No build artifact for variant={args.variant} mesh={args.mesh}.\n"
                f"Run: python tools/build_firmware.py --variant {args.variant} --mesh {args.mesh}"
            )
        # Erase if board never seen here, last attempt didn't reach 'ok',
        # variant/mesh changed, or --force-reflash.
        prev_rec = find_existing_endpoint(endpoint)
        last_ok = bool(prev_rec and prev_rec.get("status", "").startswith("ok"))
        same_variant = bool(prev_rec and prev_rec.get("variant", fc.DEFAULT_VARIANT) == args.variant)
        same_mesh = bool(prev_rec and prev_rec.get("mesh", fc.DEFAULT_MESH) == args.mesh)
        do_erase = (not args.no_erase) and (args.force_reflash or not (last_ok and same_variant and same_mesh))
        if do_erase:
            fc.erase_flash(env, com)
            notes.append("erase-flash")
        west_flash(env, com, args.variant, args.mesh, args.baud)

    # 4. Provision in TB Edge (idempotent)
    tb = TBClient(args.host, args.port, args.user, args.password)
    tb.login()
    if not args.verify_only:
        provision_single(tb, endpoint, args.profile, dry_run=False)

    # 5. Hard reset (DTR/RTS pulse) before verify, replicates the unplug/replug
    # that empirically makes first-boot OpenThread NVS settings stick.
    if not args.verify_only:
        try:
            fc.hard_reset(com, label="post-flash reset")
        except Exception as e:
            print(f"[onboard] post-flash reset skipped ({e})")
        # Boot delay (CONFIG_BOOT_DELAY=4000) + brief settle
        _time.sleep(6)

    # 6. Verify Active + telemetry
    print(f"[onboard] waiting up to {args.verify_timeout}s for first telemetry...")
    ok, sensors = wait_for_telemetry(tb, endpoint, args.verify_timeout)

    # 7. If first verify fails, do one auto-retry with another DTR/RTS pulse
    if not ok and not args.verify_only:
        print(f"[onboard] first verify timed out — pulsing reset and retrying once")
        try:
            fc.hard_reset(com, label="retry reset")
        except Exception as e:
            print(f"[onboard] retry reset skipped ({e})")
        _time.sleep(6)
        retry_timeout = max(60, args.verify_timeout // 2)
        print(f"[onboard] retrying verify up to {retry_timeout}s")
        ok, sensors = wait_for_telemetry(tb, endpoint, retry_timeout)
        if ok:
            notes.append("recovered-after-retry-reset")

    # 8. Ledger
    dev = tb.find_device_by_name(endpoint)
    tb_id = (dev or {}).get("id", {}).get("id", "")
    status = "ok" if ok else "no-telemetry"
    fc.ledger_append({
        "ts_iso": fc.now_iso(),
        "endpoint": endpoint,
        "mac": mac,
        "com": com,
        "board": fc.DEFAULT_BOARD,
        "variant": args.variant,
        "mesh": args.mesh,
        "fw_sha": fw_sha,
        "tb_host": args.host,
        "status": status,
        "tb_device_id": tb_id,
        "notes": ";".join(notes),
    })

    if ok:
        print(f"\n[onboard] {endpoint}  OK  sensors={sensors[:6]}{'...' if len(sensors) > 6 else ''}")
        return 0
    print(f"\n[onboard] {endpoint}  FAILED — Active=False or no sensor telemetry within {args.verify_timeout}s")
    return 1


if __name__ == "__main__":
    sys.exit(main())
