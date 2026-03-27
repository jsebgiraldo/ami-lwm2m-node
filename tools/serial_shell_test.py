#!/usr/bin/env python3
"""Test serial shell interaction with ESP32-C6 USB Serial/JTAG."""
import serial
import time
import sys

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM11"
BAUD = 115200

def main():
    try:
        ser = serial.Serial(PORT, BAUD, timeout=0.5, write_timeout=1)
        ser.dtr = False
        ser.rts = False
        print(f"Port {PORT} opened at {BAUD} baud")
    except Exception as e:
        print(f"Cannot open {PORT}: {e}")
        return

    # Read any buffered data
    d = ser.read(4096)
    if d:
        print(f"Buffered ({len(d)} bytes): {d!r}")

    # Send shell commands
    cmds = [b"\r\n", b"\r\n", b"help\r\n", b"\r\n", b"ot state\r\n", b"\r\n"]
    for cmd in cmds:
        time.sleep(0.3)
        try:
            ser.write(cmd)
            label = cmd.strip() or b"<enter>"
            print(f"Sent: {label.decode()}")
        except Exception as e:
            print(f"Write failed: {e}")
        time.sleep(0.5)
        data = ser.read(4096)
        if data:
            text = data.decode("utf-8", errors="replace")
            print(f"  Response ({len(data)} B): {text}")
        else:
            print("  No response")

    # Listen for more data
    print("\nListening for 15 more seconds...")
    start = time.time()
    total = 0
    while time.time() - start < 15:
        data = ser.read(1024)
        if data:
            total += len(data)
            text = data.decode("utf-8", errors="replace")
            sys.stdout.write(text)
            sys.stdout.flush()
        time.sleep(0.05)

    print(f"\n\nTotal received after commands: {total} bytes")
    ser.close()

if __name__ == "__main__":
    main()
