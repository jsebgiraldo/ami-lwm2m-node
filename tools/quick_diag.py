#!/usr/bin/env python3
"""Quick serial diagnostic for the ESP32 node on COM13."""
import serial
import time
import sys

PORT = "COM13"
BAUD = 115200

print(f"Opening {PORT} at {BAUD}...")
try:
    ser = serial.Serial(PORT, BAUD, timeout=2)
except Exception as e:
    print(f"ERROR: Cannot open {PORT}: {e}")
    sys.exit(1)

time.sleep(0.5)
ser.reset_input_buffer()

def send_cmd(cmd, wait=3):
    print(f"\n>>> {cmd}")
    ser.write((cmd + '\r\n').encode())
    time.sleep(wait)
    data = ser.read(ser.in_waiting or 4096)
    text = data.decode('utf-8', errors='replace')
    for line in text.split('\n'):
        line = line.strip()
        if line:
            print(f"  {line}")
    return text

# Check OpenThread state
send_cmd('ot state', 2)

# Check Thread role
send_cmd('ot role', 2)

# Check dataset
send_cmd('ot dataset active', 2)

# Check channel  
send_cmd('ot channel', 2)

# Check panid
send_cmd('ot panid', 2)

# Check network name
send_cmd('ot networkname', 2)

# Check IPv6 addresses
send_cmd('ot ipaddr', 2)

# Check if Thread interface is enabled
send_cmd('ot ifconfig', 2)
send_cmd('ot thread', 2)

# Check LwM2M status
send_cmd('lwm2m read 0/0/0 -s', 2)

ser.close()
print("\n--- Done ---")
