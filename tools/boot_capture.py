#!/usr/bin/env python3
"""Reset an ESP32-C6 node over its USB-CDC console and capture the boot log with
elapsed timestamps. Used to see what a node does in the first ~40s of uptime
(e.g. whether the DLMS poll thread stalls after the first telemetry push).

Usage: python tools/boot_capture.py --com COM31 --secs 80
"""
import argparse, sys, time
import serial


def reset_via_lines(ser):
    """Classic esptool 'run' reset: RTS->EN(reset), DTR->IO0(boot=high=run).
    Works on the C6 USB-Serial-JTAG too (it emulates the RTS/DTR mapping)."""
    ser.setDTR(False)   # IO0 high -> normal boot (run app)
    ser.setRTS(True)    # EN low -> hold in reset
    time.sleep(0.15)
    ser.setRTS(False)   # EN high -> release -> boot
    time.sleep(0.05)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--com", required=True)
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--secs", type=int, default=80)
    a = ap.parse_args()

    ser = serial.Serial(a.com, a.baud, timeout=0.5)
    time.sleep(0.3)
    ser.reset_input_buffer()
    print(f"[boot-capture] {a.com} @ {a.baud} — resetting, capturing {a.secs}s", flush=True)
    reset_via_lines(ser)

    t0 = time.time()
    buf = b""
    while time.time() - t0 < a.secs:
        data = ser.read(4096)
        if data:
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                txt = line.decode("utf-8", "replace").rstrip("\r")
                if txt:
                    print(f"[{time.time()-t0:6.1f}s] {txt}", flush=True)
    ser.close()
    print(f"[boot-capture] done ({a.secs}s)", flush=True)


if __name__ == "__main__":
    main()
