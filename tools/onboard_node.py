#!/usr/bin/env python3
"""
End-to-end onboarding for a new AMI node (WROOM/XIAO).

Flow:
1) Build firmware (optional)
2) Flash firmware (optional)
3) Provision endpoint in ThingsBoard Edge/Cloud
4) Verify credentials/device exists

Examples:
  python tools/onboard_node.py --mac 98:a3:16:61:9e:70 --com COM13
  python tools/onboard_node.py --endpoint ami-esp32c6-9e70 --com COM13 --skip-build
  python tools/onboard_node.py --endpoint ami-esp32c6-9e70 --skip-build --skip-flash --verify-only
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

from provision_node import (
    EDGE_HOST,
    EDGE_PORT,
    DEFAULT_PASS,
    DEFAULT_USER,
    TARGET_PROFILE_NAME,
    TBClient,
    mac_to_endpoint,
    provision_single,
    verify_single,
)


def run_cmd(cmd: list[str], cwd: pathlib.Path | None = None) -> None:
    print(f"\n[CMD] {' '.join(cmd)}")
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AMI node onboarding automation")

    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--mac", help="Node MAC (used to derive endpoint)")
    src.add_argument("--endpoint", help="Explicit endpoint, e.g. ami-esp32c6-9e70")

    parser.add_argument("--com", default="COM13", help="Serial port for west flash (default: COM13)")
    parser.add_argument(
        "--board",
        default="esp32c6_devkitc/esp32c6/hpcore",
        help="Zephyr board target (default: esp32c6_devkitc/esp32c6/hpcore)",
    )
    parser.add_argument("--baud", default="460800", help="west flash baud (default: 460800)")

    parser.add_argument("--host", default=EDGE_HOST, help=f"ThingsBoard host (default: {EDGE_HOST})")
    parser.add_argument("--port", type=int, default=EDGE_PORT, help=f"ThingsBoard port (default: {EDGE_PORT})")
    parser.add_argument("--user", default=DEFAULT_USER, help="ThingsBoard username")
    parser.add_argument("--password", default=DEFAULT_PASS, help="ThingsBoard password")
    parser.add_argument("--profile", default=TARGET_PROFILE_NAME, help="Device profile name")

    parser.add_argument("--west-workspace", default=r"C:\Users\User\Documents\FW\ESP32C6-XIAO\DLMS-COSEM")
    parser.add_argument("--app-path", default=r"C:\Users\User\Documents\UNAL\ami-lwm2m-node")

    parser.add_argument("--skip-build", action="store_true", help="Skip west build")
    parser.add_argument("--skip-flash", action="store_true", help="Skip west flash")
    parser.add_argument("--verify-only", action="store_true", help="Only verify device in TB")

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    endpoint = args.endpoint if args.endpoint else mac_to_endpoint(args.mac)

    ws = pathlib.Path(args.west_workspace)
    app = pathlib.Path(args.app_path)

    print("=" * 68)
    print("AMI ONBOARDING")
    print(f"Endpoint : {endpoint}")
    print(f"Board    : {args.board}")
    print(f"COM      : {args.com}")
    print(f"TB       : http://{args.host}:{args.port}")
    print("=" * 68)

    try:
        if not args.verify_only:
            if not args.skip_build:
                run_cmd(["west", "build", "-p", "always", "-b", args.board, str(app)], cwd=ws)

            if not args.skip_flash:
                run_cmd(
                    [
                        "west",
                        "flash",
                        "--esp-device",
                        args.com,
                        "--esp-baud-rate",
                        args.baud,
                    ],
                    cwd=ws,
                )

        tb = TBClient(args.host, args.port, args.user, args.password)
        tb.login()

        if not args.verify_only:
            provision_single(tb, endpoint, args.profile, dry_run=False)

        print("\n[VERIFY]")
        verify_single(tb, endpoint)

        print("\n[NEXT]")
        print("1) Open serial and run: ami status")
        print("2) Run: ami test lwm2m")
        print("3) Confirm LwM2M is registered")

    except subprocess.CalledProcessError as e:
        print(f"\n[ERROR] Command failed with exit code {e.returncode}")
        return e.returncode
    except Exception as e:
        print(f"\n[ERROR] {e}")
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
