"""Reset ESP32-C6 via esptool ROM commands, then monitor boot"""
import serial
import serial.tools.list_ports
import time
import sys
import subprocess

PORT = "COM11"
LOG = r"C:\tmp\ami_log.txt"

# Step 1: Use esptool to connect and run stub, which does a proper reset on exit
print(f"[{time.strftime('%H:%M:%S')}] Resetting ESP32-C6 via esptool...")

# esptool --port COM11 --chip esp32c6 --after hard_reset read_mac
# This connects, runs stub, reads MAC, then does hard_reset via RTS
result = subprocess.run([
    sys.executable, "-m", "esptool",
    "--port", PORT, "--chip", "esp32c6",
    "--after", "hard_reset",
    "read_mac"
], capture_output=True, text=True, timeout=30)

print(result.stdout[-200:] if result.stdout else "no stdout")
if result.stderr:
    print(f"stderr: {result.stderr[-200:]}")

print(f"[{time.strftime('%H:%M:%S')}] Reset done (exit={result.returncode})")

# Step 2: Wait for chip to boot
time.sleep(3)

# Step 3: Open serial and capture boot
print(f"[{time.strftime('%H:%M:%S')}] Opening {PORT} for monitoring...")
try:
    ser = serial.Serial(PORT, 115200, timeout=1, write_timeout=5)
except Exception as e:
    print(f"Failed to open: {e}")
    print("Port may be corrupted. Waiting for power cycle...")
    
    for i in range(60):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        if PORT not in ports:
            print(f"  {PORT} gone! Waiting for reappear...")
            for j in range(60):
                time.sleep(0.5)
                ports2 = [p.device for p in serial.tools.list_ports.comports()]
                if PORT in ports2:
                    print(f"  {PORT} back! Waiting 3s...")
                    time.sleep(3)
                    ser = serial.Serial(PORT, 115200, timeout=1, write_timeout=5)
                    break
            else:
                print("COM11 didn't come back")
                sys.exit(1)
            break
        time.sleep(0.5)
    else:
        print("COM11 didn't disappear - trying to open anyway")
        sys.exit(1)

print(f"[{time.strftime('%H:%M:%S')}] Port open, reading...")

log_lines = []
start = time.time()
blank_count = 0

while time.time() - start < 90:
    try:
        line = ser.readline().decode('utf-8', errors='replace').rstrip()
    except Exception as e:
        print(f"Read error: {e}")
        break
    
    if line:
        blank_count = 0
        ts = time.strftime('%H:%M:%S')
        entry = f"[{ts}] {line}"
        print(entry)
        log_lines.append(entry)
        
        if "AMI ALIVE" in line or "uart:~$" in line:
            time.sleep(2)
            for cmd in ["ot state", "ot ipaddr", "ot rloc16", "ot dataset active"]:
                ser.write(f"{cmd}\r\n".encode())
                time.sleep(0.5)
    else:
        blank_count += 1
        if blank_count >= 10:
            ser.write(b"ot state\r\n")
            blank_count = 0

with open(LOG, "w") as f:
    for l in log_lines:
        f.write(l + "\n")

print(f"\n[{time.strftime('%H:%M:%S')}] Done. {len(log_lines)} lines saved to {LOG}")
ser.close()
