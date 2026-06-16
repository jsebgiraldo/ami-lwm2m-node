"""Monitor v0.6.67 keepalive_emit_count via JTAG — verify the worker fires.

Usage: python tools/keepalive_monitor.py <MAC> [ticks] [interval_s]
"""
import pathlib
import re
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
OPENOCD = REPO / "tools" / "openocd-esp32" / "openocd-esp32" / "bin" / "openocd.exe"
SCRIPTS = REPO / "tools" / "openocd-esp32" / "openocd-esp32" / "share" / "openocd" / "scripts"

# Symbol addresses (v0.6.67 ELF, verified via objdump 2026-06-06)
SYMS = {
    "uptime":      0x4084633c,   # lwm2m_uptime_s
    "role":        0x408462a4,   # thread_role_atomic
    "regS":        0x408462e0,   # lwm2m_diag_reg_success
    "regA":        0x408462e4,   # lwm2m_diag_reg_attempts
    "emit":        0x408462e8,   # last_emit_uptime
    "err_c":       0x408462d4,
    "recov":       0x408462dc,
    "wdog":        0x408462cc,
    "noreg":       0x408462c4,
    "resets":      0x408462bc,
    "ka_count":    0x4084639c,   # v0.6.67 keepalive_emit_count
}


def read(mac):
    mac = mac.upper()
    args = [
        str(OPENOCD), "-s", str(SCRIPTS),
        "-c", f"adapter serial {mac}",
        "-c", "gdb port 13334", "-c", "telnet port 14445", "-c", "tcl port 16670",
        "-f", "board/esp32c6-builtin.cfg",
        "-c", "init", "-c", "halt",
    ]
    for _, addr in SYMS.items():
        args += ["-c", f"mdw 0x{addr:08x} 1"]
    args += ["-c", "resume", "-c", "shutdown"]

    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return None
    text = r.stdout + r.stderr
    if "Target halted" not in text:
        return None
    out = {}
    for name, addr in SYMS.items():
        m = re.search(rf"0x{addr:08x}:\s+([0-9a-f]+)", text)
        if m:
            v = int(m.group(1), 16)
            out[name] = v if v < 0x80000000 else v - 0x100000000
    return out


def main():
    if len(sys.argv) < 2:
        print("usage: keepalive_monitor.py <MAC> [ticks=20] [interval_s=30]")
        sys.exit(1)
    mac = sys.argv[1]
    ticks = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    interval = int(sys.argv[3]) if len(sys.argv) > 3 else 30

    print(f"=== v0.6.67 keepalive monitor on {mac} ({ticks} ticks @ {interval}s) ===")
    print(f"{'tick':<5}{'up':<7}{'role':<6}{'regS/A':<10}{'emit':<6}{'ka':<5}"
          f"{'recov':<6}{'wdog':<5}{'err':<7}{'resets':<7}")
    last_ka = None
    for i in range(1, ticks + 1):
        r = read(mac)
        if not r:
            print(f"{i:<5} JTAG_FAIL")
        else:
            ka = r.get("ka_count", 0)
            mark = ""
            if last_ka is not None and ka > last_ka:
                mark = " <-- keepalive fired"
            last_ka = ka
            role_map = {0: "OFF", 1: "Det", 2: "Chld", 3: "Rtr", 4: "Ldr"}
            print(f"{i:<5}{r.get('uptime',0):<7}"
                  f"{role_map.get(r.get('role'), '?'):<6}"
                  f"{r.get('regS',0)}/{r.get('regA',0):<7}"
                  f"{r.get('emit',0):<6}"
                  f"{ka:<5}"
                  f"{r.get('recov',0):<6}"
                  f"{r.get('wdog',0):<5}"
                  f"{r.get('err_c',0):<7}"
                  f"{r.get('resets',0):<7}"
                  f"{mark}", flush=True)
        if i < ticks:
            time.sleep(interval)


if __name__ == "__main__":
    main()
