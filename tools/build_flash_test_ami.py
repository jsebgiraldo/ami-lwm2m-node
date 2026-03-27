"""Build full AMI firmware, flash it, and test with proper USB timing."""
import subprocess
import serial
import serial.tools.list_ports
import time
import os
import sys

VENV_PY = r"C:\Users\User\Documents\FW\ESP32C6-XIAO\DLMS-COSEM\.venv\Scripts\python.exe"
BUILD_DIR = r"C:\Users\User\Documents\FW\ESP32C6-XIAO\DLMS-COSEM"
SRC_DIR = r"C:\Users\User\Documents\UNAL\ami-lwm2m-node"
BIN = os.path.join(BUILD_DIR, "build", "zephyr", "zephyr.bin")
RESULT = os.path.join(SRC_DIR, "full_ami_result.txt")
PORT = "COM11"

lines = []
def log(msg):
    print(msg, flush=True)
    lines.append(msg)

def save():
    with open(RESULT, "w") as f:
        f.write("\n".join(lines))

# Step 1: Build
log("=== STEP 1: Build full AMI firmware ===")
build_cmd = (
    f'Set-Location "{BUILD_DIR}" ; '
    f'.\\".venv\\Scripts\\Activate.ps1" ; '
    f'west build -p always -b xiao_esp32c6/esp32c6/hpcore "{SRC_DIR}" 2>&1 | '
    f'Select-Object -Last 25'
)
r = subprocess.run(
    ["powershell", "-ExecutionPolicy", "Bypass", "-Command", build_cmd],
    capture_output=True, text=True, timeout=300
)
log(f"Build exit: {r.returncode}")
# Show last lines of build output
for line in r.stdout.strip().split("\n")[-15:]:
    log(f"  {line}")
# Check binary exists (west may return 1 for warnings)
if not os.path.exists(BIN):
    log("BUILD FAILED - no binary produced")
    for line in r.stderr.strip().split("\n")[-10:]:
        log(f"  ERR: {line}")
    save()
    sys.exit(1)

binsize = os.path.getsize(BIN)
log(f"Binary: {binsize} bytes")
# Verify it's a full build (>500KB for full AMI)
if binsize < 100000:
    log(f"WARNING: Binary too small ({binsize}B), may be wrong config")


# Step 2: Flash with hard-reset
log("=== STEP 2: Flash ===")
# Release port first
try:
    s = serial.Serial(PORT, 115200, timeout=0.1)
    s.close()
except: pass
time.sleep(1)

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

# Step 3: Wait for USB re-enumeration
log("=== STEP 3: Wait for USB re-enum ===")
for i in range(8):
    present = PORT in [p.device for p in serial.tools.list_ports.comports()]
    log(f"  {i}s: COM11 {'PRESENT' if present else 'ABSENT'}")
    if present and i >= 3:
        break
    time.sleep(1)

# Wait for firmware boot (BOOT_DELAY=4000ms)
log("Waiting 8s for firmware to boot (BOOT_DELAY=4000)...")
time.sleep(8)

# Step 4: Read serial with defaults (dtr=True, rts=True)
log("=== STEP 4: Read serial ===")
try:
    s = serial.Serial(PORT, 115200, timeout=0.5)
    log(f"Port open. dtr={s.dtr} rts={s.rts}")
    
    # Send Enter to wake shell
    s.write(b"\r\n")
    
    buf = b""
    t = time.time()
    while time.time() - t < 20:
        c = s.read(512)
        if c:
            buf += c
            log(f"  [{time.time()-t:.1f}s] +{len(c)}B")
    s.close()
    
    log(f"TOTAL CAPTURED: {len(buf)} bytes")
    if buf:
        text = buf.decode("utf-8", errors="replace")
        log(f"TEXT:\n{text[:3000]}")
    else:
        log("NO OUTPUT - 0 bytes")
        log(">>> FIRMWARE CRASHES DURING INIT (networking/OpenThread) <<<")
except Exception as e:
    log(f"Serial error: {e}")

save()
log("Done.")
