#!/usr/bin/env python3
"""Read ESP32 serial WITHOUT resetting it (DTR/RTS disabled)."""
import serial
import time
import re
import sys

PORT = "COM13"
BAUD = 115200

print(f"Opening {PORT} (DTR/RTS disabled to avoid reset)...")
try:
    ser = serial.Serial()
    ser.port = PORT
    ser.baudrate = BAUD
    ser.timeout = 2
    ser.dtr = False
    ser.rts = False
    ser.open()
    # Ensure DTR/RTS stay low
    ser.dtr = False
    ser.rts = False
except Exception as e:
    print(f"ERROR: {e}")
    sys.exit(1)

time.sleep(0.5)

# Read any buffered data first
data = ser.read(ser.in_waiting or 1)
if data:
    text = re.sub(r'\x1b\[[0-9;]*m', '', data.decode('utf-8', errors='replace'))
    print(f"BUFFERED:\n{text}")

# Now send commands
def send_cmd(cmd, wait=3):
    ser.reset_input_buffer()
    print(f"\n>>> {cmd}")
    ser.write((cmd + '\r\n').encode())
    
    output = b''
    deadline = time.time() + wait
    while time.time() < deadline:
        if ser.in_waiting:
            output += ser.read(ser.in_waiting)
        time.sleep(0.1)
    
    text = re.sub(r'\x1b\[[0-9;]*m', '', output.decode('utf-8', errors='replace'))
    for line in text.split('\n'):
        line = line.strip()
        if line:
            print(f"  {line}")
    return text

# Try waking up shell
print("Sending ENTER to wake shell...")
ser.write(b'\r\n')
time.sleep(1)
data = ser.read(ser.in_waiting or 1)
if data:
    text = re.sub(r'\x1b\[[0-9;]*m', '', data.decode('utf-8', errors='replace'))
    print(f"Response: {text.strip()}")

# Diagnostics
send_cmd('kernel version', 2)
send_cmd('ot state', 3)
send_cmd('ot channel', 2)
send_cmd('ot panid', 2)
send_cmd('ot networkname', 2)
send_cmd('ot ipaddr', 3)
send_cmd('ot ifconfig', 2)
send_cmd('ot thread', 2)

# Check uptime
send_cmd('kernel uptime', 2)

ser.close()
print("\n--- Done ---")
