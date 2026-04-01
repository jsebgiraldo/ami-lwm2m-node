"""
flash_jtag.py - Flash ESP32-C6 via OpenOCD USB-JTAG (MI_02 interface).

NO BUTTONS NEEDED — but requires a clean JTAG state.

IMPORTANT LIMITATION:
  Once Zephyr boots and its USB CDC driver initializes (at PRE_KERNEL_1), it
  takes over the ESP32-C6 USB Serial/JTAG hardware controller and the JTAG
  endpoint becomes non-functional until a hardware reset (physical replug).

  WORKFLOW:
    1. Plug in the XIAO ESP32-C6 (or unplug + replug)
    2. Run this script within ~30s — JTAG is accessible before/during Zephyr boot
    3. After the first flash, JTAG breaks when the new firmware initializes USB CDC
    4. For the next flash: replug again, then run this script

  You do NOT need to press any buttons. Just replug before each flash.

Usage:
    python flash_jtag.py                   # flash build/zephyr/zephyr.bin
    python flash_jtag.py path/to/file.bin  # flash a specific binary
"""
import subprocess
import sys
import os
import time

OPENOCD = r"C:\Users\User\.espressif\tools\openocd-esp32\v0.12.0-esp32-20250707\openocd-esp32\bin\openocd.exe"
SCRIPTS  = r"C:\Users\User\.espressif\tools\openocd-esp32\v0.12.0-esp32-20250707\openocd-esp32\share\openocd\scripts"

# Binary resolution: prefer the freshest build between the west workspace and
# the app-local build directory so a stale cache never sneaks in.
_CANDIDATES = [
    r"C:\Users\User\Documents\FW\ESP32C6-XIAO\DLMS-COSEM\build\zephyr\zephyr.bin",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "build", "zephyr", "zephyr.bin"),
]

def _latest_bin():
    found = [(p, os.path.getmtime(p)) for p in _CANDIDATES if os.path.exists(p)]
    if not found:
        return _CANDIDATES[0]  # will trigger "not found" error downstream
    best = max(found, key=lambda x: x[1])
    return best[0]

DEFAULT_BIN = _latest_bin()


def _run_openocd(bin_fwd):
    return subprocess.run(
        [OPENOCD, "-s", SCRIPTS,
         "-f", "board/esp32c6-builtin.cfg",
         "-c", f"program_esp {bin_fwd} 0x0 verify reset exit"],
        capture_output=True, text=True, timeout=120
    )


def flash(bin_path=None):
    if bin_path is None:
        bin_path = DEFAULT_BIN
    if not os.path.exists(bin_path):
        print(f"ERROR: binary not found: {bin_path}")
        sys.exit(1)
    bin_fwd = bin_path.replace("\\", "/")
    size = os.path.getsize(bin_path)
    print(f"Flashing {size//1024}KB  →  {bin_path}")
    print(f"Using OpenOCD JTAG (no bootloader mode needed)...")
    t0 = time.time()
    result = _run_openocd(bin_fwd)
    combined = result.stdout + result.stderr
    elapsed = time.time() - t0

    if "Verify OK" in combined and "Programming Finished" in combined:
        print(f"FLASH OK in {elapsed:.1f}s  (verified)")
        return True

    # Detect JTAG broken by Zephyr USB CDC takeover
    jtag_broken = (
        "libusb_get_string_descriptor_ascii() failed with -9" in combined or
        "could not find or open device" in combined or
        "Unsupported DTM version" in combined
    )
    if jtag_broken:
        print()
        print("=" * 60)
        print("JTAG BROKEN — Zephyr took over the USB JTAG controller.")
        print()
        print("FIX: Unplug and replug the XIAO ESP32-C6 USB cable,")
        print("     then run this script again within ~30 seconds.")
        print("     (No buttons needed — just replug once.)")
        print("=" * 60)
        return False

    print("FLASH FAILED. OpenOCD output:")
    for line in combined.splitlines():
        if any(x in line for x in ["Error", "Warn", "failed", "FAILED"]):
            print(f"  {line}")
    print(f"\nFull log saved to flash_jtag_debug.txt")
    with open("flash_jtag_debug.txt", "w") as f:
        f.write(combined)
    return False


if __name__ == "__main__":
    bin_path = sys.argv[1] if len(sys.argv) > 1 else None
    ok = flash(bin_path)
    sys.exit(0 if ok else 1)
