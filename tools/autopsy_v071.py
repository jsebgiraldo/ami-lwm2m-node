"""JTAG autopsy for v0.7.1-pmax60 boards (SED256 and FTD256 share layout).

Reads the diagnostic atomics straight from RAM to answer: why is this board
silent? Key fields:
  noreg_boots >= 5            -> parked by the anti-brick guard (no more reboots)
  first_register_complete=0   -> never registered this boot
  reached_network=0           -> never even resolved/contacted the server
  thread_role 0/1             -> radio dead / detached (L2 problem)
  thread_role 2+              -> mesh fine, problem is upper layers

Usage: python tools/autopsy_v071.py <MAC>
"""
import pathlib, re, subprocess, sys

REPO = pathlib.Path(__file__).resolve().parent.parent
OPENOCD = REPO / "tools/openocd-esp32/openocd-esp32/bin/openocd.exe"
SCRIPTS = REPO / "tools/openocd-esp32/openocd-esp32/share/openocd/scripts"

SYMS = {
    "thread_role":        0x4083c020,
    "reached_network":    0x4083c034,
    "first_reg_complete": 0x4083c038,
    "in_recovery":        0x4083c040,
    "noreg_boots":        0x4083c048,
    "watchdog_count":     0x4083c050,
    "last_error_code":    0x4083c058,
    "recover_count":      0x4083c060,
    "reg_success":        0x4083c064,
    "reg_attempts":       0x4083c068,
    "last_reset_reason":  0x4080d8ec,
    "lwm2m_connected":    0x4083c3e1,
}
ROLE = {0: "DISABLED", 1: "DETACHED", 2: "CHILD", 3: "ROUTER", 4: "LEADER"}
RR = {1: "POR", 2: "EXT", 4: "BRN", 8: "SW", 16: "WDOG"}


def main():
    mac = sys.argv[1].upper()
    ofs = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    args = [str(OPENOCD), "-s", str(SCRIPTS),
            "-c", f"adapter serial {mac}",
            "-c", f"gdb port {23000+ofs}", "-c", f"telnet port {23100+ofs}",
            "-c", f"tcl port {23200+ofs}",
            "-f", "board/esp32c6-builtin.cfg",
            "-c", "init", "-c", "halt"]
    for a in SYMS.values():
        args += ["-c", f"mdw 0x{a:08x} 1"]
    args += ["-c", "resume", "-c", "shutdown"]
    r = subprocess.run(args, capture_output=True, text=True, timeout=40)
    text = r.stdout + r.stderr
    if "Target halted" not in text:
        print(f"{mac}: JTAG_FAIL")
        print(text[-400:])
        return 2
    out = {}
    for name, addr in SYMS.items():
        m = re.search(rf"0x{addr:08x}:\s+([0-9a-f]+)", text)
        if m:
            v = int(m.group(1), 16)
            if name == "last_error_code" and v >= 0x80000000:
                v -= 0x100000000
            if name == "lwm2m_connected":
                v &= 0xFF
            out[name] = v
    role = ROLE.get(out.get("thread_role", -1) & 0xFF, "?")
    rr = RR.get(out.get("last_reset_reason", 0), str(out.get("last_reset_reason")))
    print(f"=== {mac} autopsy ===")
    print(f"  thread_role        = {role}")
    print(f"  reached_network    = {bool(out.get('reached_network'))}")
    print(f"  first_reg_complete = {bool(out.get('first_reg_complete'))}")
    print(f"  lwm2m_connected    = {bool(out.get('lwm2m_connected'))}")
    print(f"  noreg_boots        = {out.get('noreg_boots')}  (>=5 = PARKED, no more auto-reboots)")
    print(f"  reg attempts/ok    = {out.get('reg_attempts')}/{out.get('reg_success')}")
    print(f"  recover_count      = {out.get('recover_count')}  in_recovery={bool(out.get('in_recovery'))}")
    print(f"  watchdog_count     = {out.get('watchdog_count')}")
    print(f"  last_error_code    = {out.get('last_error_code')}")
    print(f"  last_reset_reason  = {rr}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
