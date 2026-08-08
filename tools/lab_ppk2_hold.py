#!/usr/bin/env python3
"""Keep the PPK2 supplying the DUT for as long as this process lives.

The PPK2 drops DUT power whenever a new API session claims the device, so any
script that opens it - even just to take a measurement - silently power-cycles
the node. That wrecks anything that depends on surviving state: RAM-retained
panic forensics, a chip parked in the ROM bootloader, an uptime soak.

Run this once in the background and leave it. It holds the session open and the
output on, so every other tool can use the console (and the operator can use the
BOOT/RESET buttons) without the rail dropping underneath them.

    python tools/lab_ppk2_hold.py --voltage 5000

Stop it to cut power. Nothing else should open the PPK2 while it runs.
"""
import argparse
import sys
import time

from ppk2_api.ppk2_api import PPK2_API


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", default="COM9", help="PPK2 serial port")
    ap.add_argument("--voltage", type=int, default=5000,
                    help="source-meter output in mV (800-5000)")
    args = ap.parse_args()

    sys.stdout.reconfigure(line_buffering=True)

    ppk = PPK2_API(args.port)
    ppk.get_modifiers()
    ppk.use_source_meter()
    ppk.set_source_voltage(args.voltage)
    ppk.toggle_DUT_power("ON")
    print(f"[hold] {args.port} sourcing {args.voltage} mV - DUT power ON")
    print("[hold] holding session open; stop this process to cut power")

    try:
        while True:
            time.sleep(30)
            # Re-assert rather than assume: cheap, and it papers over a stray
            # session that stole the device and left the output off.
            ppk.toggle_DUT_power("ON")
    except KeyboardInterrupt:
        print("[hold] interrupted - releasing PPK2 (power will drop)")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
