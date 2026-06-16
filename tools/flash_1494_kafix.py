#!/usr/bin/env python3
"""Flash board 1494 with the resprobe+kafix artifact via OpenOCD JTAG.

Mirrors tools/bulk_flash_jtag.py's OpenOCD program_esp invocation, but points
at build_resprobe/zephyr/zephyr.signed.bin (the v0.7.3-kafix keepalive fix) so
the ONLY change vs the looping firmware is the one-line logic fix. MCUboot is
already on the target; we flash only the signed app at 0x20000 and reset.

Usage: python tools/flash_1494_kafix.py
(SuperMini must be in JTAG/WinUSB mode — no COM. Serial = MAC uppercase.)
"""
import pathlib
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
OPENOCD = REPO / "tools" / "openocd-esp32" / "openocd-esp32" / "bin" / "openocd.exe"
SCRIPTS = REPO / "tools" / "openocd-esp32" / "openocd-esp32" / "share" / "openocd" / "scripts"
ZP = pathlib.Path.home() / "Documents" / "ESP32" / "zephyrproject"
APP = ZP / "build_resprobe" / "ami-lwm2m-node" / "zephyr" / "zephyr.signed.bin"

MAC_1494 = "10:51:db:1c:14:94"


def flash(mac, app):
    mac = mac.upper()
    app_arg = app.as_posix().replace("/c/", "C:/")
    args = [
        str(OPENOCD), "-s", str(SCRIPTS),
        "-c", f"adapter serial {mac}",
        "-c", "gdb port 13334", "-c", "telnet port 14445", "-c", "tcl port 16670",
        "-f", "board/esp32c6-builtin.cfg",
        "-c", "init",
        "-c", f"program_esp {app_arg} 0x20000 reset",
        "-c", "shutdown",
    ]
    args = [a.replace("\\", "/") for a in args]
    t0 = time.time()
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return False, "timeout", time.time() - t0
    text = r.stdout + r.stderr
    elapsed = time.time() - t0
    if "Programming Finished" in text:
        return True, "OK", elapsed
    if "could not find or open device" in text:
        return False, "USB_FAIL (board in JTAG mode? serial=MAC uppercase?)", elapsed
    tail = "\n".join(text.splitlines()[-8:])
    return False, f"UNKNOWN\n{tail}", elapsed


def main():
    if not APP.exists():
        print(f"ERROR: artifact missing: {APP}\nRun: python tools/build_resprobe_kafix.py")
        sys.exit(2)
    print(f"Flashing 1494 ({MAC_1494}) with {APP.name} (v0.7.3-kafix)...")
    ok, note, dur = flash(MAC_1494, APP)
    print(f"  1494: {'OK ' if ok else 'FAIL'} ({note}) in {dur:.1f}s")
    if ok:
        print("\nFlashed + reset. Watch the soak: 1494 should now survive PAST"
              " uptime 706s (no more ~12-min reboot loop). total_resets should"
              " stop climbing.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
