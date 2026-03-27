"""Flash pre-built full AMI firmware and test serial output."""
import subprocess
import serial
import serial.tools.list_ports
import time
import os
import sys

VENV_PY = r"C:\Users\User\Documents\FW\ESP32C6-XIAO\DLMS-COSEM\.venv\Scripts\python.exe"
BIN = r"C:\Users\User\Documents\FW\ESP32C6-XIAO\DLMS-COSEM\build\zephyr\zephyr.bin"
RESULT = r"C:\Users\User\Documents\UNAL\ami-lwm2m-node\full_ami_result.txt"
PORT = "COM11"

lines = []
def log(msg):
    print(msg, flush=True)
    lines.append(msg)

def save():
    with open(RESULT, "w") as f:
        f.write("\n".join(lines))

# Check binary
binsize = os.path.getsize(BIN) if os.path.exists(BIN) else 0
log(f"Binary: {binsize} bytes ({BIN})")
mod = time.ctime(os.path.getmtime(BIN)) if os.path.exists(BIN) else "N/A"
log(f"Modified: {mod}")

if binsize < 100000:
    log("Binary too small or missing!")
    save()
    sys.exit(1)

# Release port
try:
    s = serial.Serial(PORT, 115200, timeout=0.1)
    s.close()
except: pass
time.sleep(1)

# Flash
log("=== Flashing ===")
r = subprocess.run([
    VENV_PY, "-m", "esptool",
    "--chip", "esp32c6", "--port", PORT, "--baud", "460800",
    "--before", "default-reset", "--after", "hard_reset",
    "write_flash", "--flash_mode", "dio", "--flash_freq", "80m",
    "--flash_size", "4MB", "0x0", BIN
], capture_output=True, text=True, timeout=60)
log(f"Flash exit: {r.returncode}")
if r.returncode != 0:
    log(f"FLASH FAILED: {r.stderr[-300:]}")
    save()
    sys.exit(1)
log("Flash OK!")

# Wait for USB re-enumeration
log("=== Waiting for USB re-enum ===")
for i in range(8):
    present = PORT in [p.device for p in serial.tools.list_ports.comports()]
    log(f"  {i}s: COM11 {'PRESENT' if present else 'ABSENT'}")
    if present and i >= 3:
        break
    time.sleep(1)

# Wait for firmware boot (BOOT_DELAY=4000 + init)
log("Waiting 8s for firmware boot...")
time.sleep(8)

# Read serial (defaults: dtr=True, rts=True)
log("=== Reading serial ===")
s = serial.Serial(PORT, 115200, timeout=0.5)
log(f"Port open. dtr={s.dtr} rts={s.rts}")
s.write(b"\r\n")  # Wake shell

buf = b""
t = time.time()
while time.time() - t < 20:
    c = s.read(512)
    if c:
        buf += c
        log(f"  [{time.time()-t:.1f}s] +{len(c)}B")
s.close()

log(f"\nTOTAL CAPTURED: {len(buf)} bytes")
if buf:
    text = buf.decode("utf-8", errors="replace")
    log(f"TEXT:\n{text[:3000]}")
    if "uart:~$" in text:
        log(">>> SHELL PROMPT DETECTED - FIRMWARE IS ALIVE <<<")
    if "AMI" in text:
        log(">>> AMI FIRMWARE RUNNING <<<")
else:
    log("NO OUTPUT - 0 bytes")
    log(">>> LIKELY CRASH DURING NETWORKING/OPENTHREAD INIT <<<")

save()
log("Done.")
