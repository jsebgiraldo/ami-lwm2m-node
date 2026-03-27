"""
Recover ESP32-C6 from crash loop.
Tries to flash using esptool with timeout handling.
Writes results to recover_result.txt.
"""
import subprocess
import sys
import os
import time

VENV_PYTHON = r"C:\Users\User\Documents\FW\ESP32C6-XIAO\DLMS-COSEM\.venv\Scripts\python.exe"
BIN_DIR = r"C:\Users\User\Documents\FW\ESP32C6-XIAO\DLMS-COSEM"
BIN = os.path.join(BIN_DIR, "build", "zephyr", "zephyr.bin")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "recover_result.txt")
PORT = "COM11"

def log(msg):
    print(msg, flush=True)
    with open(OUT, "a") as f:
        f.write(msg + "\n")

# Clear old result
with open(OUT, "w") as f:
    f.write(f"=== Recovery attempt {time.strftime('%H:%M:%S')} ===\n")

# Check binary exists
if not os.path.exists(BIN):
    log(f"ERROR: Binary not found: {BIN}")
    sys.exit(1)

bin_size = os.path.getsize(BIN)
log(f"Binary: {BIN} ({bin_size} bytes)")

# Attempt 1: default-reset (normal path)
for attempt in range(1, 4):
    log(f"\n--- Flash attempt {attempt}/3 (default-reset, baud 460800) ---")
    try:
        r = subprocess.run(
            [VENV_PYTHON, "-m", "esptool",
             "--chip", "esp32c6", "--port", PORT, "--baud", "460800",
             "--before", "default-reset", "--after", "hard-reset",
             "write-flash", "--flash-mode", "dio", "--flash-freq", "80m",
             "--flash-size", "4MB", "0x0", BIN],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0:
            log("FLASH SUCCESS!")
            log(r.stdout[-300:] if len(r.stdout) > 300 else r.stdout)
            log("\n=== RECOVERY COMPLETE ===")
            # Now capture serial output
            log("\nCapturing serial output for 15 seconds...")
            import serial
            time.sleep(3)
            try:
                s = serial.Serial(PORT, 115200, timeout=0.5)
                s.dtr = True
                s.rts = False
                buf = b""
                t = time.time()
                while time.time() - t < 15:
                    c = s.read(512)
                    if c:
                        buf += c
                s.close()
                text = buf.decode("utf-8", errors="replace")
                log(f"CAPTURED: {len(buf)} bytes")
                log(f"TEXT: {text[:1000]}")
            except Exception as e:
                log(f"Serial read error: {e}")
            sys.exit(0)
        else:
            log(f"Failed (exit={r.returncode})")
            log(f"stderr tail: {r.stderr[-200:]}")
    except subprocess.TimeoutExpired:
        log(f"Timeout (30s)")
    except Exception as e:
        log(f"Error: {e}")
    time.sleep(2)

# Attempt 2: try with lower baud
log("\n--- Trying lower baud rate (115200) ---")
try:
    r = subprocess.run(
        [VENV_PYTHON, "-m", "esptool",
         "--chip", "esp32c6", "--port", PORT, "--baud", "115200",
         "--before", "default-reset", "--after", "hard-reset",
         "write-flash", "--flash-mode", "dio", "--flash-freq", "80m",
         "--flash-size", "4MB", "0x0", BIN],
        capture_output=True, text=True, timeout=120
    )
    if r.returncode == 0:
        log("FLASH SUCCESS (115200)!")
        log("\n=== RECOVERY COMPLETE ===")
        sys.exit(0)
    else:
        log(f"Failed: {r.stderr[-200:]}")
except subprocess.TimeoutExpired:
    log("Timeout at 115200 too")
except Exception as e:
    log(f"Error: {e}")

log("\n=== ALL AUTOMATIC ATTEMPTS FAILED ===")
log(">>> MANUAL RECOVERY NEEDED <<<")
log("1. On the XIAO ESP32-C6 board:")
log("   - Hold the BOOT button (small button)")  
log("   - Press and release RESET")
log("   - Release BOOT")
log("2. The device is now in bootloader mode")
log("3. Run this script again, or flash manually:")
log(f'   {VENV_PYTHON} -m esptool --chip esp32c6 --port {PORT} --baud 460800 --before no-reset --after hard-reset write-flash --flash-mode dio --flash-freq 80m --flash-size 4MB 0x0 "{BIN}"')
