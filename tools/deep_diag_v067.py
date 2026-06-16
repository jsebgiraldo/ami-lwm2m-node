"""v0.6.67 deep JTAG diagnostic — read full state of a board.

Reads ALL known LwM2M/Thread/keepalive diagnostic symbols and decodes them.
Use to understand why a stuck board is stuck or why a recovered board recovered.

Usage: python tools/deep_diag_v067.py <MAC>
"""
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
OPENOCD = REPO / "tools" / "openocd-esp32" / "openocd-esp32" / "bin" / "openocd.exe"
SCRIPTS = REPO / "tools" / "openocd-esp32" / "openocd-esp32" / "share" / "openocd" / "scripts"

# v0.6.67 ELF symbol addresses (riscv64-zephyr-elf-nm verified 2026-06-06)
SYMS = {
    # uptime / role
    "uptime_s":           (0x4084633c, "u32"),
    "thread_role":        (0x408462a4, "role"),
    # keepalive
    "ka_emit_count":      (0x4084639c, "u32"),
    # registration
    "reg_success":        (0x408462e0, "u32"),
    "reg_attempts":       (0x408462e4, "u32"),
    "first_register_done": (0x408462b8, "u32"),
    "reached_network":    (0x408462b4, "u32"),
    "probe_baseline_ok":  (0x408462a0, "u32"),
    # error / recovery
    "last_error_code":    (0x408462d4, "i32"),
    "last_error_uptime":  (0x408462d0, "u32"),
    "in_recovery":        (0x408462c0, "u32"),
    "recovering_flag":    (0x408462ac, "u32"),
    "diag_recover_cnt":   (0x408462dc, "u32"),
    "restart_success":    (0x408462d8, "u32"),
    "storm_backoff":      (0x408462c8, "u32"),
    "noreg_boots":        (0x408462c4, "u32"),
    # watchdog / resets
    "watchdog_count":     (0x408462cc, "u32"),
    "total_resets":       (0x408462bc, "u32"),
    "last_reset_reason":  (0x4080da84, "i32"),
    # connection
    "lwm2m_connected":    (0x40846651, "u8"),
    # snapshots (from thread_conn_monitor.c)
    "snap_uptime":        (0x4084633c, "u32"),
    "snap_notify_emit":   (0x40846330, "u32"),
    "snap_notify_throt":  (0x4084632c, "u32"),
    "snap_recover":       (0x40846328, "u32"),
    "snap_restart":       (0x40846324, "u32"),
    "snap_last_err_up":   (0x4084631c, "u32"),
    "snap_wdog":          (0x40846318, "u32"),
    "snap_storm":         (0x40846314, "u32"),
}

ROLE_MAP = {0: "OFF/Disabled", 1: "Detached", 2: "Child", 3: "Router", 4: "Leader"}
RESET_REASONS = {
    0: "POWERON",
    1: "EXT_PIN",
    2: "BROWNOUT",
    3: "SOFTWARE_LOWPOWER",
    4: "WDOG_DEEPSLEEP",
    5: "WDOG_INT_RTC",
    6: "RESERVED6",
    7: "RESERVED7",
    8: "SOFTWARE",
    9: "DEEPSLEEP",
    10: "BROWNOUT",
    11: "SYS_RTC",
    12: "WDOG_SDIO",
    13: "WDOG_TG0",
    14: "WDOG_TG1",
    15: "RESERVED15",
    16: "WDOG_INT",
    17: "PWR_GLITCH",
    18: "EFUSE",
    19: "USB_UART",
    20: "USB_JTAG",
}


def read_board(mac):
    mac = mac.upper()
    args = [
        str(OPENOCD), "-s", str(SCRIPTS),
        "-c", f"adapter serial {mac}",
        "-c", "gdb port 13334", "-c", "telnet port 14445", "-c", "tcl port 16670",
        "-f", "board/esp32c6-builtin.cfg",
        "-c", "init", "-c", "halt",
    ]
    for name, (addr, _) in SYMS.items():
        args += ["-c", f"mdw 0x{addr:08x} 1"]
    args += ["-c", "resume", "-c", "shutdown"]
    r = subprocess.run(args, capture_output=True, text=True, timeout=30)
    text = r.stdout + r.stderr
    if "Target halted" not in text:
        return None, text
    out = {}
    for name, (addr, t) in SYMS.items():
        m = re.search(rf"0x{addr:08x}:\s+([0-9a-f]+)", text)
        if m:
            raw = int(m.group(1), 16)
            if t == "i32" and raw >= 0x80000000:
                raw -= 0x100000000
            elif t == "u8":
                raw &= 0xff
            elif t == "role":
                pass
            out[name] = raw
    return out, text


def fmt_role(v):
    return ROLE_MAP.get(v & 0xff, f"?({v})")


def fmt_reset(v):
    return RESET_REASONS.get(v & 0xff, f"?({v})")


def main():
    if len(sys.argv) < 2:
        print("usage: deep_diag_v067.py <MAC>")
        sys.exit(1)
    mac = sys.argv[1]
    print(f"=== Deep diag v0.6.67 on {mac.upper()} ===\n")
    s, _ = read_board(mac)
    if s is None:
        print("JTAG_FAIL — could not halt target")
        sys.exit(2)

    print(f"## Identity / Time")
    print(f"  uptime_s         = {s.get('uptime_s')} s ({s.get('uptime_s', 0)/60:.1f} min)")
    print(f"  thread_role      = {fmt_role(s.get('thread_role', 0))}")
    print()
    print(f"## Keepalive (v0.6.67 feature)")
    print(f"  ka_emit_count    = {s.get('ka_emit_count')}  (1 fire / 5min after 60s grace)")
    print()
    print(f"## Registration")
    print(f"  reg_success      = {s.get('reg_success')}")
    print(f"  reg_attempts     = {s.get('reg_attempts')}")
    print(f"  first_register   = {bool(s.get('first_register_done'))}")
    print(f"  reached_network  = {bool(s.get('reached_network'))}")
    print(f"  probe_baseline   = {bool(s.get('probe_baseline_ok'))}")
    print(f"  lwm2m_connected  = {bool(s.get('lwm2m_connected'))}")
    print()
    print(f"## Errors / Recovery")
    print(f"  last_error_code  = {s.get('last_error_code')}  ({'OK' if s.get('last_error_code')==0 else 'ERR'})")
    print(f"  last_error_uptime= {s.get('last_error_uptime')} s")
    print(f"  in_recovery      = {bool(s.get('in_recovery'))}")
    print(f"  recovering_flag  = {bool(s.get('recovering_flag'))}")
    print(f"  diag_recover_cnt = {s.get('diag_recover_cnt')}")
    print(f"  restart_success  = {s.get('restart_success')}")
    print(f"  storm_backoff    = {s.get('storm_backoff')}")
    print(f"  noreg_boots      = {s.get('noreg_boots')}  (boots that NEVER registered)")
    print()
    print(f"## Watchdog / Resets")
    print(f"  watchdog_count   = {s.get('watchdog_count')}")
    print(f"  total_resets     = {s.get('total_resets')}")
    print(f"  last_reset_reason= {fmt_reset(s.get('last_reset_reason', 0))}")
    print()
    print(f"## Conn Monitor Snapshots")
    print(f"  notify_emit      = {s.get('snap_notify_emit')}")
    print(f"  notify_throt     = {s.get('snap_notify_throt')}")


if __name__ == "__main__":
    main()
