"""v0.6.69 deep JTAG diagnostic — full state of a board.

Symbol addresses extracted from the v0.6.69 ELF (build with TIER1+TIER2
RAM cuts; heap pool 96→64 KB shifted BSS layout from 0x4084xxxx to
0x4083cxxx). v0.6.69 adds: keepalive_consec_fail, last_emit_uptime,
in_recovery — and the recover_work re-entry guard means recovering_flag
should be 1 while recover is in flight.

Usage: python tools/deep_diag_v069.py <MAC>
"""
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
OPENOCD = REPO / "tools" / "openocd-esp32" / "openocd-esp32" / "bin" / "openocd.exe"
SCRIPTS = REPO / "tools" / "openocd-esp32" / "openocd-esp32" / "share" / "openocd" / "scripts"

# v0.6.69 ELF symbol addresses (riscv64-zephyr-elf-nm verified 2026-06-06)
SYMS = {
    # uptime / role
    "uptime_s":           (0x4083c16c, "u32"),
    "thread_role":        (0x4083c0c4, "role"),
    # keepalive (v0.6.67 + v0.6.69 expansions)
    "ka_emit_count":      (0x4083c1d0, "u32"),
    "ka_consec_fail":     (0x4083c1cc, "u32"),  # v0.6.69 new
    # registration
    "reg_success":        (0x4083c100, "u32"),
    "reg_attempts":       (0x4083c104, "u32"),
    "first_register_done": (0x4083c0d8, "u32"),
    "reached_network":    (0x4083c0d4, "u32"),
    "probe_baseline_ok":  (0x4083c0c0, "u32"),
    # error / recovery (post-v0.6.69 the recovering flag is held longer)
    "last_error_code":    (0x4083c0f4, "i32"),
    "last_error_uptime":  (0x4083c0f0, "u32"),
    "in_recovery":        (0x4083c0e0, "u32"),
    "recovering_flag":    (0x4083c0cc, "u32"),
    "diag_recover_cnt":   (0x4083c0fc, "u32"),
    "restart_success":    (0x4083c0f8, "u32"),
    "storm_backoff":      (0x4083c0e8, "u32"),
    "noreg_boots":        (0x4083c0e4, "u32"),
    # watchdog / resets
    "watchdog_count":     (0x4083c0ec, "u32"),
    "total_resets":       (0x4083c0dc, "u32"),
    "last_reset_reason":  (0x4080d99c, "i32"),
    "last_emit_uptime":   (0x4083c108, "u32"),  # v0.6.69 — silence watchdog heartbeat
    # connection
    "lwm2m_connected":    (0x4083c475, "u8"),
    # conn_monitor snapshots
    "snap_notify_emit":   (0x4083c160, "u32"),
    "snap_notify_throt":  (0x4083c15c, "u32"),
}

ROLE_MAP = {0: "OFF/Disabled", 1: "Detached", 2: "Child", 3: "Router", 4: "Leader"}
RESET_REASONS = {
    0: "POWERON", 1: "EXT_PIN", 2: "BROWNOUT", 3: "SOFTWARE_LOWPOWER",
    4: "WDOG_DEEPSLEEP", 5: "WDOG_INT_RTC", 8: "SOFTWARE", 16: "WDOG_INT",
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
            out[name] = raw
    return out, text


def main():
    if len(sys.argv) < 2:
        print("usage: deep_diag_v069.py <MAC>")
        sys.exit(1)
    mac = sys.argv[1]
    print(f"=== Deep diag v0.6.69 on {mac.upper()} ===\n")
    s, _ = read_board(mac)
    if s is None:
        print("JTAG_FAIL")
        sys.exit(2)

    print(f"## Identity / Time")
    print(f"  uptime_s         = {s.get('uptime_s')} s ({s.get('uptime_s', 0)/60:.1f} min)")
    print(f"  thread_role      = {ROLE_MAP.get(s.get('thread_role', 0) & 0xff, '?')}")
    print()
    print(f"## Keepalive (v0.6.69: + consec_fail)")
    print(f"  ka_emit_count    = {s.get('ka_emit_count')}")
    print(f"  ka_consec_fail   = {s.get('ka_consec_fail')}  (>=3 triggers recover)")
    print(f"  last_emit_uptime = {s.get('last_emit_uptime')} s  (silence watchdog feed)")
    print()
    print(f"## Registration")
    print(f"  reg_success      = {s.get('reg_success')}")
    print(f"  reg_attempts     = {s.get('reg_attempts')}")
    print(f"  first_register   = {bool(s.get('first_register_done'))}")
    print(f"  reached_network  = {bool(s.get('reached_network'))}")
    print(f"  probe_baseline   = {bool(s.get('probe_baseline_ok'))}")
    print(f"  lwm2m_connected  = {bool(s.get('lwm2m_connected'))}")
    print()
    print(f"## Errors / Recovery (v0.6.69: re-entry guard active)")
    print(f"  last_error_code  = {s.get('last_error_code')}")
    print(f"  last_error_uptime= {s.get('last_error_uptime')} s")
    print(f"  in_recovery      = {bool(s.get('in_recovery'))}")
    print(f"  recovering_flag  = {bool(s.get('recovering_flag'))}")
    print(f"  diag_recover_cnt = {s.get('diag_recover_cnt')}")
    print(f"  restart_success  = {s.get('restart_success')}")
    print(f"  storm_backoff    = {s.get('storm_backoff')}")
    print(f"  noreg_boots      = {s.get('noreg_boots')}")
    print()
    print(f"## Watchdog / Resets")
    print(f"  watchdog_count   = {s.get('watchdog_count')}")
    print(f"  total_resets     = {s.get('total_resets')}")
    rr = s.get('last_reset_reason', 0)
    print(f"  last_reset_reason= {RESET_REASONS.get(rr & 0xff, f'?({rr})')}")
    print()
    print(f"## Conn Monitor Snapshots")
    print(f"  notify_emit      = {s.get('snap_notify_emit')}")
    print(f"  notify_throt     = {s.get('snap_notify_throt')}")


if __name__ == "__main__":
    main()
