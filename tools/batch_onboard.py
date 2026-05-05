#!/usr/bin/env python3
"""Plug-and-go batch onboarding for fleet deployment.

Workflow:
  1. Run tools/edge_health.py once before starting.
  2. Run tools/build_firmware.py once.
  3. Run this script. It snapshots currently-plugged ESP32-C6 boards,
     then enters a loop:
        - waits for a NEW board to appear on USB
        - runs onboard_node logic (read MAC, flash, provision, verify)
        - waits for the operator to unplug it before listening for the next
  4. Ctrl+C to stop. State is preserved in tools/deployment_ledger.csv.

Examples:
    python tools/batch_onboard.py
    python tools/batch_onboard.py --target 60   # stop after 60 successful nodes
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback

import fleet_common as fc

fc.bootstrap_venv()

sys.path.insert(0, str(fc.TOOLS_DIR))
import onboard_node  # noqa: E402
from provision_node import TBClient  # noqa: E402


def _set(ports):
    return {p.com for p in ports}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=0, help="Stop after N successful nodes (0 = unlimited)")
    ap.add_argument("--host", default=None,
                    help="TB Edge host. Default: derived from --mesh.")
    ap.add_argument("--port", type=int, default=None,
                    help="TB Edge port. Default: derived from --mesh.")
    ap.add_argument("--user", default=fc.EDGE_TENANT_USER)
    ap.add_argument("--password", default=fc.EDGE_TENANT_PASS)
    ap.add_argument("--profile", default=fc.EDGE_PROFILE)
    ap.add_argument("--variant", default=fc.DEFAULT_VARIANT, choices=fc.VARIANTS,
                    help="Firmware variant to flash (default: med)")
    ap.add_argument("--mesh", default=fc.DEFAULT_MESH, choices=fc.MESH_TARGETS,
                    help=f"Target Thread mesh. Default: {fc.DEFAULT_MESH}")
    ap.add_argument("--baud", default="460800")
    ap.add_argument("--verify-timeout", type=int, default=180)
    ap.add_argument("--continue-on-fail", action="store_true",
                    help="Keep running after a failed node (default: stop)")
    args = ap.parse_args()

    # Resolve TB Edge from --mesh if --host/--port not given.
    mesh_host, mesh_port = fc.edge_for_mesh(args.mesh)
    if args.host is None:
        args.host = mesh_host
    if args.port is None:
        args.port = mesh_port

    env = fc.detect_env(verbose=True)
    if not fc.firmware_bin(env, args.variant, args.mesh).exists():
        raise SystemExit(
            f"No build artifact for variant={args.variant} mesh={args.mesh}.\n"
            f"Run: python tools/build_firmware.py --variant {args.variant} --mesh {args.mesh}"
        )
    fw_sha = fc.firmware_hash(env, args.variant, args.mesh)
    print(f"[batch] variant={args.variant}  mesh={args.mesh}  Edge=http://{args.host}:{args.port}  fw_sha={fw_sha}")

    # share one TB session across the loop (re-login on auth errors)
    tb = TBClient(args.host, args.port, args.user, args.password)
    tb.login()

    baseline = _set(fc.list_esp32c6_ports())
    if baseline:
        print(f"[batch] baseline (will be ignored): {sorted(baseline)}")
    print("[batch] waiting for new ESP32-C6 board... (Ctrl+C to stop)")

    successful = 0
    failed = 0
    start = time.time()

    try:
        while True:
            cur = _set(fc.list_esp32c6_ports())
            new = sorted(cur - baseline)
            if not new:
                time.sleep(1.0)
                continue

            com = new[0]
            print(f"\n{'=' * 60}\n[batch] new board on {com}")
            print(f"[batch] running ({successful} ok, {failed} fail so far)")

            ok = False
            try:
                ok = _onboard_one(tb, env, com, args, fw_sha)
            except KeyboardInterrupt:
                raise
            except Exception:
                traceback.print_exc()

            if ok:
                successful += 1
            else:
                failed += 1
                if not args.continue_on_fail:
                    print(f"\n[batch] STOP — last node failed. Pass --continue-on-fail to ignore.")
                    break

            print(f"\n[batch] unplug {com} when ready for next board...")
            while com in _set(fc.list_esp32c6_ports()):
                time.sleep(1.0)
            baseline = _set(fc.list_esp32c6_ports())

            if args.target and successful >= args.target:
                print(f"[batch] reached target of {args.target} — stopping.")
                break
    except KeyboardInterrupt:
        print("\n[batch] interrupted by operator")

    elapsed = int(time.time() - start)
    print(f"\n[batch] DONE  successful={successful}  failed={failed}  elapsed={elapsed//60}m{elapsed%60}s")
    return 0 if failed == 0 else 1


def _onboard_one(tb: TBClient, env: fc.ToolEnv, com: str, args, fw_sha: str) -> bool:
    """Body equivalent to a single tools/onboard_node.py invocation, but reusing the TB session."""
    from provision_node import provision_single

    # 1. MAC + endpoint
    mac = fc.read_mac(env, com)
    endpoint = fc.mac_to_endpoint(mac)
    print(f"[onboard] mac={mac}  endpoint={endpoint}")

    notes: list[str] = []

    # 2. Skip if same fw_sha + variant + mesh already deployed for this endpoint
    prev = onboard_node.find_existing_endpoint(endpoint)
    same_variant = bool(prev and prev.get("variant", fc.DEFAULT_VARIANT) == args.variant)
    same_mesh = bool(prev and prev.get("mesh", fc.DEFAULT_MESH) == args.mesh)
    skip_flash = bool(prev
                      and prev.get("fw_sha") == fw_sha
                      and same_variant
                      and same_mesh
                      and prev.get("status", "").startswith("ok"))
    if skip_flash:
        print(f"[onboard] ledger says endpoint deployed with same fw_sha+variant+mesh — skipping flash")
        notes.append("skip-flash:ledger-match")

    # 3. Flash (auto-erase if no prior 'ok' record OR variant/mesh changed)
    if not skip_flash:
        last_ok = bool(prev and prev.get("status", "").startswith("ok"))
        if not (last_ok and same_variant and same_mesh):
            fc.erase_flash(env, com)
            notes.append("erase-flash")
        onboard_node.west_flash(env, com, args.variant, args.mesh, args.baud)

    # 4. Provision (idempotent)
    try:
        provision_single(tb, endpoint, args.profile, dry_run=False)
    except Exception as e:
        # session may have expired; retry once with fresh login
        print(f"[onboard] provision error ({e}), re-logging in")
        tb.login()
        provision_single(tb, endpoint, args.profile, dry_run=False)

    # 5. Hard reset (DTR/RTS pulse) replicates unplug/replug for first-boot OT NVS
    try:
        fc.hard_reset(com, label="post-flash reset")
    except Exception as e:
        print(f"[onboard] post-flash reset skipped ({e})")
    time.sleep(6)

    # 6. Verify
    ok, sensors = onboard_node.wait_for_telemetry(tb, endpoint, args.verify_timeout)
    # Auto-retry once with extra reset pulse if first verify times out
    if not ok:
        print(f"[onboard] first verify timed out — pulsing reset and retrying")
        try:
            fc.hard_reset(com, label="retry reset")
        except Exception as e:
            print(f"[onboard] retry reset skipped ({e})")
        time.sleep(6)
        ok, sensors = onboard_node.wait_for_telemetry(tb, endpoint, max(60, args.verify_timeout // 2))
        if ok:
            notes.append("recovered-after-retry-reset")

    # 6. Ledger
    dev = tb.find_device_by_name(endpoint)
    tb_id = (dev or {}).get("id", {}).get("id", "")
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
        "status": "ok" if ok else "no-telemetry",
        "tb_device_id": tb_id,
        "notes": ";".join(notes),
    })

    if ok:
        head = sensors[:6]
        suffix = "..." if len(sensors) > 6 else ""
        print(f"[onboard] {endpoint}  OK  sensors={head}{suffix}")
    else:
        print(f"[onboard] {endpoint}  FAILED")
    return ok


if __name__ == "__main__":
    sys.exit(main())
