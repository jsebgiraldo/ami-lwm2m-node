"""
flash_and_monitor.py - Flash via JTAG then immediately monitor serial output.

The definitive workflow for the XIAO ESP32-C6:
  1. Flash firmware via OpenOCD JTAG  (no buttons, no DTR/RTS)
  2. Wait 3s for USB re-enumeration + BOOT_DELAY
  3. Open COM11 and stream output for the specified duration

Usage:
    python flash_and_monitor.py                      # flash + monitor 3 min
    python flash_and_monitor.py --seconds 60         # flash + monitor 60s
    python flash_and_monitor.py --no-flash           # monitor only (device running)
    python flash_and_monitor.py --bin path/to/fw.bin # flash specific binary
    python flash_and_monitor.py --out capture.txt    # save output to file
"""
import sys
import os
import time
import argparse

# Allow running from project root OR tools/
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import flash_jtag
import monitor as mon


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-flash", action="store_true", help="Skip flash, only monitor")
    parser.add_argument("--bin", default=None, help="Binary to flash (default: build/zephyr/zephyr.bin)")
    parser.add_argument("--seconds", type=float, default=180, help="Monitor duration in seconds (default 180)")
    parser.add_argument("--port", default="COM11")
    parser.add_argument("--out", default=None, help="Save captured output to this file")
    args = parser.parse_args()

    if not args.no_flash:
        ok = flash_jtag.flash(args.bin)
        if not ok:
            print("\nTo monitor the currently-running firmware without flashing:")
            print(f"  python flash_and_monitor.py --no-flash --seconds {int(args.seconds)}")
            sys.exit(1)
        # No explicit sleep — open_port() retries every 1.5s until COM port
        # appears after USB re-enumeration (typically 1-2s after JTAG reset).
        # This ensures we connect before the 4s BOOT_DELAY expires.

    print(f"Monitoring {args.port} for {args.seconds:.0f}s...")
    buf = mon.monitor(port=args.port, duration=args.seconds, outfile=args.out)

    # Status summary
    text = buf.decode("utf-8", errors="replace")
    checks = [
        ("Boot banner",       "AMI ALIVE" in text),
        ("Thread attached",   "Thread attached" in text),
        ("LwM2M started",     "RD Client started" in text),
        ("LwM2M registered",  "Registration complete" in text or "204" in text),
        ("DLMS cycle",        "Meter poll cycle" in text),
        ("DLMS connected",    "HDLC UA" in text or "Connected to meter" in text),
    ]
    print("\n=== Node Status ===")
    for label, ok in checks:
        print(f"  {'OK' if ok else '--'} {label}")


if __name__ == "__main__":
    main()
