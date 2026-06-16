"""Snapshot diag counters via JTAG for one ESP32-C6 board.

Usage: python tools/jtag_snapshot.py <MAC>
"""
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
OPENOCD = REPO / "tools" / "openocd-esp32" / "openocd-esp32" / "bin" / "openocd.exe"
SCRIPTS = REPO / "tools" / "openocd-esp32" / "openocd-esp32" / "share" / "openocd" / "scripts"

# (name, address, size_in_bytes)
SYMS = [
    ("up",       0x40845cdc),  # lwm2m_uptime_s
    ("role",     0x40845c44),  # thread_role_atomic
    ("regS",     0x40845c80),  # lwm2m_diag_reg_success
    ("regA",     0x40845c84),  # lwm2m_diag_reg_attempts
    ("emit",     0x40845c88),  # last_emit_uptime
    ("err_c",    0x40845c74),  # last_error_code
    ("err_t",    0x40845c70),  # last_error_uptime
    ("recov",    0x40845c7c),  # lwm2m_diag_recover_count
    ("wdog",     0x40845c6c),  # lwm2m_diag_watchdog_count
    ("noreg",    0x40845c64),  # lwm2m_diag_noreg_boots
    ("resets",   0x40845c5c),  # lwm2m_diag_total_resets
    ("restart",  0x40845c78),  # lwm2m_diag_restart_success
]


def snapshot(mac):
    args = [
        str(OPENOCD), "-s", str(SCRIPTS),
        "-c", f"adapter serial {mac}",
        "-c", "gdb port 13334", "-c", "telnet port 14445", "-c", "tcl port 16670",
        "-f", "board/esp32c6-builtin.cfg",
        "-c", "init", "-c", "halt",
        "-c", "mdb 0x42801ae0 8",  # version string
        "-c", "reg pc",
    ]
    for _, addr in SYMS:
        args += ["-c", f"mdw 0x{addr:08x} 1"]
    args += ["-c", "resume", "-c", "shutdown"]

    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return None, "JTAG_TIMEOUT"
    text = r.stdout + r.stderr
    if "could not find or open device" in text or "LIBUSB_ERROR" in text:
        return None, "JTAG_USB_FAIL"
    if "Target halted" not in text:
        return None, "JTAG_NO_HALT"

    # Parse version
    vm = re.search(r"0x42801ae0:\s+((?:[0-9a-f]{2}\s+){8})", text)
    if vm:
        b = [int(x, 16) for x in vm.group(1).split()]
        ver = bytes(b).split(b"\x00")[0].decode("ascii", errors="replace")
    else:
        ver = "?"

    # Parse PC
    pc_m = re.search(r"pc \(/32\):\s+0x([0-9a-f]+)", text)
    pc = pc_m.group(1) if pc_m else "?"

    out = {"ver": ver, "pc": pc}
    for name, addr in SYMS:
        m = re.search(rf"0x{addr:08x}:\s+([0-9a-f]+)", text)
        if m:
            v = int(m.group(1), 16)
            sv = v if v < 0x80000000 else v - 0x100000000
            out[name] = sv
        else:
            out[name] = "?"
    return out, None


def main():
    mac = sys.argv[1] if len(sys.argv) > 1 else None
    if not mac:
        print("usage: jtag_snapshot.py <MAC>")
        sys.exit(1)
    data, err = snapshot(mac)
    if err:
        print(f"FAIL: {err}")
        sys.exit(2)
    print(f"ver={data['ver']} pc=0x{data['pc']}")
    print(" ".join(f"{k}={v}" for k, v in data.items() if k not in ("ver", "pc")))


if __name__ == "__main__":
    main()
