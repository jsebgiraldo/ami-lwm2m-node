"""Stream serial output from an ESP32-C6 USB-Serial-JTAG to a log file
incrementally (flush on every line) so it can be tailed live.

Wraps monitor._RawComPort to get the correct Win32 CreateFileW handling that
pyserial chokes on. Unlike monitor.monitor() which only writes outfile at the
END, this one appends each chunk as it arrives so a live `tail -f` works.

Usage:
    python tools/serial_stream.py --port COM24 --out logs/f79c.log --seconds 1800
"""
import argparse
import sys
import os
import time
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import monitor as mon


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seconds", type=float, default=1800)
    args = ap.parse_args()

    port = mon.open_port(args.port, write_access=True)
    if not port:
        print(f"FATAL: could not open {args.port}")
        sys.exit(1)

    deadline = time.time() + args.seconds
    print(f"streaming {args.port} -> {args.out} for {args.seconds:.0f}s")
    with open(args.out, "a", encoding="utf-8", buffering=1) as f:
        f.write(f"\n===== stream start {datetime.datetime.now().isoformat()} =====\n")
        f.flush()
        buf = b""
        while time.time() < deadline:
            try:
                chunk = port.read(4096)
            except OSError as e:
                f.write(f"[serial_stream] OSError: {e}\n")
                f.flush()
                time.sleep(0.5)
                port = mon.open_port(args.port, write_access=True)
                if not port:
                    f.write("[serial_stream] reopen failed, exiting\n")
                    break
                continue
            if chunk:
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    f.write(line.decode("utf-8", errors="replace") + "\n")
                f.flush()
            else:
                time.sleep(0.05)
        if buf:
            f.write(buf.decode("utf-8", errors="replace"))
        f.write(f"\n===== stream end {datetime.datetime.now().isoformat()} =====\n")


if __name__ == "__main__":
    main()
