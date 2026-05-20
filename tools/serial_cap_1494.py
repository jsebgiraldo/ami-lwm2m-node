#!/usr/bin/env python3
"""Capture COM17 (node 1494) serial for N seconds, echo FW/OTA-relevant lines."""
from __future__ import annotations
import sys, time
import serial

PORT = "COM17"
BAUD = 115200
DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 180
RAW = "tools/serial_1494_ota.log"
MARK = ("fw:", "fota", "firmware", "/5/0", "block", "download", "upgrade", "reboot", "mcuboot")


def main() -> int:
    try:
        ser = serial.Serial(PORT, BAUD, timeout=1)
    except Exception as e:
        print(f"[serial] open {PORT} failed: {e}", flush=True)
        return 2
    print(f"[serial] capturing {PORT} for {DURATION}s -> {RAW}", flush=True)
    end = time.time() + DURATION
    with open(RAW, "w", encoding="utf-8", errors="replace") as f:
        while time.time() < end:
            try:
                line = ser.readline().decode("utf-8", "replace").rstrip()
            except Exception as e:
                print(f"[serial] read err: {e}", flush=True)
                break
            if not line:
                continue
            f.write(line + "\n"); f.flush()
            if any(m in line.lower() for m in MARK):
                print(time.strftime("[%H:%M:%S] ") + line, flush=True)
    ser.close()
    print("[serial] capture done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
