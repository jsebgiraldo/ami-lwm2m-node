#!/usr/bin/env python3
"""JTAG-examine the 3 SuperMini after the v0.7.3-kafix flash.

Halts each core over OpenOCD, reads the diagnostic globals straight from RAM
(addresses resolved from build_resprobe ELF via nm), resumes. No serial needed.
The decisive fix metric is keepalive_consec_fail: under the bug it climbed
0->1->2->3 (then reboot); with kafix it must stay 0 while emit_count climbs.

Usage: python tools/jtag_examine_kafix.py
"""
import pathlib
import re
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
OPENOCD = REPO / "tools" / "openocd-esp32" / "openocd-esp32" / "bin" / "openocd.exe"
SCRIPTS = REPO / "tools" / "openocd-esp32" / "openocd-esp32" / "share" / "openocd" / "scripts"

NODES = {
    "1494": "10:51:db:1c:14:94",
    "f7b4": "10:51:db:1b:f7:b4",
    "fbb8": "10:51:db:1b:fb:b8",
}

# symbol -> RAM address (riscv64-zephyr-elf-nm on build_resprobe/.../zephyr.elf)
SYMS = [
    ("reset_reason",   0x4083cce0),
    ("total_resets",   0x4083cc6c),
    ("recover_count",  0x4083cc90),
    ("watchdog_count", 0x4083cc80),
    ("noreg_boots",    0x4083cc78),
    ("reached_net",    0x4083cc64),
    ("ka_emit",        0x4083cd70),
    ("ka_consec_fail", 0x4083cd6c),
    ("last_emit_up",   0x4083cc9c),
]


def examine(mac):
    mac = mac.upper()
    cmds = [str(OPENOCD), "-s", str(SCRIPTS),
            "-c", f"adapter serial {mac}",
            "-c", "gdb port 13335", "-c", "telnet port 14446", "-c", "tcl port 16671",
            "-f", "board/esp32c6-builtin.cfg",
            "-c", "init", "-c", "halt"]
    for _, addr in SYMS:
        cmds += ["-c", f"mdw 0x{addr:08x}"]
    cmds += ["-c", "resume", "-c", "shutdown"]
    cmds = [c.replace("\\", "/") for c in cmds]
    try:
        r = subprocess.run(cmds, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    text = r.stdout + r.stderr
    # mdw lines look like: "0x4083cd6c: 00000002"
    vals = {}
    for name, addr in SYMS:
        m = re.search(rf"0x{addr:08x}:\s*([0-9a-fA-F]{{8}})", text)
        vals[name] = int(m.group(1), 16) if m else None
    if all(v is None for v in vals.values()):
        tail = "\n".join(text.splitlines()[-6:])
        return None, f"no reads\n{tail}"
    return vals, "ok"


def main():
    print("JTAG examine (halt/read/resume) post v0.7.3-kafix flash\n")
    for name, mac in NODES.items():
        vals, note = examine(mac)
        if vals is None:
            print(f"  {name}: FAIL ({note})")
            continue
        rr = vals.get("reset_reason")
        print(f"  {name}: rr={rr} TR={vals['total_resets']} recov={vals['recover_count']} "
              f"wdog={vals['watchdog_count']} noreg={vals['noreg_boots']} "
              f"reached_net={vals['reached_net']} | ka_emit={vals['ka_emit']} "
              f"ka_consec_fail={vals['ka_consec_fail']} last_emit_up={vals['last_emit_up']}s",
              flush=True)
    print("\nFix OK if ka_consec_fail stays 0 (vs climbing to 3) and TR stops growing.")


if __name__ == "__main__":
    main()
