#!/usr/bin/env python3
"""Flash all 3 SuperMini (1494, f7b4, fbb8) with resprobe+kafix via OpenOCD JTAG.

All three already carry MCUboot (their current send/resprobe builds are sysbuild),
so we flash only the signed app at slot0 (0x20000) and reset. Uniform instrumented
build (Object 33000 + post_mortem + resource_probe) makes all 3 examinable over
JTAG, and validates the v0.7.3-kafix keepalive fix on all 3 at once.

Usage: python tools/flash_3nodes_kafix.py [mac ...]   # default = the 3 SuperMini
Serial = MAC uppercase (ESP32-C6 USB-Serial-JTAG; libusb is case-sensitive).
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

NODES = {
    "1494": "10:51:db:1c:14:94",
    "f7b4": "10:51:db:1b:f7:b4",
    "fbb8": "10:51:db:1b:fb:b8",
}


def flash(mac):
    mac = mac.upper()
    app_arg = APP.as_posix().replace("/c/", "C:/")
    # unique ports per call avoid collisions if a prior OpenOCD lingered
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
    if "could not find or open device" in text or "LIBUSB_ERROR" in text:
        return False, "USB_FAIL (replug needed?)", elapsed
    tail = "\n".join(text.splitlines()[-6:])
    return False, f"UNKNOWN\n{tail}", elapsed


def main():
    if not APP.exists():
        print(f"ERROR: artifact missing: {APP}")
        sys.exit(2)
    args = sys.argv[1:]
    targets = ({m: m for m in args} if args else NODES)
    print(f"Flashing {len(targets)} node(s) with {APP.name} (v0.7.3-kafix), app@0x20000\n")
    ok = 0
    for name, mac in targets.items():
        success, note, dur = flash(mac)
        print(f"  {name} ({mac}): {'OK ' if success else 'FAIL'} ({note}) in {dur:.1f}s", flush=True)
        if success:
            ok += 1
    print(f"\nResult: {ok}/{len(targets)} flashed. Boards reset into v0.7.3-kafix.")
    print("Expect: all 3 survive PAST uptime 706s; total_resets stops climbing.")


if __name__ == "__main__":
    main()
