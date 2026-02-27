#!/usr/bin/env python3
"""Serial diagnostic with retry logic for ESP32 on COM13."""
import serial
import time
import sys

PORT = "COM13"
BAUD = 115200

print(f"Opening {PORT} at {BAUD}...")
try:
    ser = serial.Serial(PORT, BAUD, timeout=3, write_timeout=3)
except Exception as e:
    print(f"ERROR: Cannot open {PORT}: {e}")
    # List available ports
    from serial.tools.list_ports import comports
    print("\nAvailable ports:")
    for p in comports():
        print(f"  {p.device}: {p.description}")
    sys.exit(1)

time.sleep(1)

# Flush everything
ser.reset_input_buffer()
ser.reset_output_buffer()

# Send a few enters to wake up the shell
print("Waking up shell...")
for _ in range(3):
    ser.write(b'\r\n')
    time.sleep(0.3)

# Read any buffered output
time.sleep(1)
data = ser.read(ser.in_waiting or 1)
if data:
    print(f"Initial output: {data.decode('utf-8', errors='replace')}")

def send_cmd(cmd, wait=3):
    ser.reset_input_buffer()
    print(f"\n>>> {cmd}")
    ser.write((cmd + '\r\n').encode())
    
    # Read with timeout
    output = b''
    deadline = time.time() + wait
    while time.time() < deadline:
        chunk = ser.read(ser.in_waiting or 1)
        if chunk:
            output += chunk
        time.sleep(0.1)
    
    text = output.decode('utf-8', errors='replace')
    for line in text.split('\n'):
        line = line.strip()
        if line:
            print(f"  {line}")
    return text

# Try basic kernel command first
result = send_cmd('kernel version', 3)
if not result.strip():
    print("\n*** No response from shell. Trying 'help'... ***")
    result = send_cmd('help', 3)

if not result.strip():
    print("\n*** Still no response. Reading any output for 5 seconds... ***")
    deadline = time.time() + 5
    while time.time() < deadline:
        chunk = ser.read(ser.in_waiting or 1)
        if chunk:
            print(chunk.decode('utf-8', errors='replace'), end='')
        time.sleep(0.1)
    
    print("\n\n*** Node may be stuck or need physical reset ***")
    print("*** Try pressing the RESET button on the XIAO board ***")
else:
    # Shell works, run diagnostics
    send_cmd('ot state', 3)
    send_cmd('ot channel', 2)
    send_cmd('ot panid', 2)
    send_cmd('ot networkname', 2) 
    send_cmd('ot ipaddr', 3)
    send_cmd('ot ifconfig', 2)
    send_cmd('ot thread', 2)

ser.close()
print("\n--- Done ---")
