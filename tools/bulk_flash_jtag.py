"""Bulk-flash via OpenOCD program_esp (no CDC needed).

Use when esptool's standard CDC route fails (driver inversion to WinUSB,
USB stack degraded post-program_esp, hub partial enumeration etc.). Each
board takes ~10 s of OpenOCD setup + ~6 s for app flash + reset.

Usage: python tools/bulk_flash_jtag.py <mac1> <mac2> ...
"""
import pathlib
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
OPENOCD = REPO / "tools" / "openocd-esp32" / "openocd-esp32" / "bin" / "openocd.exe"
SCRIPTS = REPO / "tools" / "openocd-esp32" / "openocd-esp32" / "share" / "openocd" / "scripts"

ZP = pathlib.Path.home() / "Documents" / "ESP32" / "zephyrproject"
# v0.6.69+ uses single-image build (no sysbuild), so binary lives directly
# under build_ota_ftd/zephyr/. MCUboot is already on every target from the
# v0.6.67 flash earlier today — flashing only the signed app at 0x20000.
APP = ZP / "build_ota_ftd" / "zephyr" / "zephyr.signed.bin"


def flash(mac):
    mac = mac.upper()
    args = [
        str(OPENOCD), "-s", str(SCRIPTS),
        "-c", f"adapter serial {mac}",
        "-c", "gdb port 13334", "-c", "telnet port 14445", "-c", "tcl port 16670",
        "-f", "board/esp32c6-builtin.cfg",
        "-c", "init",
        "-c", f"program_esp {APP.as_posix().replace('/c/', 'C:/')} 0x20000 reset",
        "-c", "shutdown",
    ]
    # Fix path expansion if as_posix returned MSYS style
    args = [a.replace("\\", "/") for a in args]

    t0 = time.time()
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return False, "timeout", time.time() - t0
    text = r.stdout + r.stderr
    elapsed = time.time() - t0
    if "Programming Finished" in text:
        return True, "OK", elapsed
    if "could not find or open device" in text:
        return False, "USB_FAIL", elapsed
    if "stub_version" in text:
        # Stub version warnings are non-fatal, look for what came after
        if "Programming Finished" in text:
            return True, "OK", elapsed
    return False, "UNKNOWN", elapsed


def main():
    if len(sys.argv) < 2:
        print("usage: bulk_flash_jtag.py <mac1> <mac2> ...")
        sys.exit(1)
    macs = sys.argv[1:]
    print(f"Flashing {len(macs)} boards via JTAG (program_esp)...")
    ok = 0
    for mac in macs:
        success, note, dur = flash(mac)
        status = "OK " if success else "FAIL"
        print(f"  {mac}: {status} ({note}) in {dur:.1f}s")
        if success:
            ok += 1
    print(f"\nResult: {ok}/{len(macs)} OK")


if __name__ == "__main__":
    main()
