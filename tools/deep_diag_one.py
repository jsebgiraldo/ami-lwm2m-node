"""Single-board JTAG diag with parameterizable ports for parallel calls.

Usage: python tools/deep_diag_one.py <MAC> <port_offset>
  port_offset 0..99 → gdb=13334+ofs telnet=14445+ofs tcl=16670+ofs
"""
import pathlib, re, subprocess, sys

REPO = pathlib.Path(__file__).resolve().parent.parent
OPENOCD = REPO / "tools/openocd-esp32/openocd-esp32/bin/openocd.exe"
SCRIPTS = REPO / "tools/openocd-esp32/openocd-esp32/share/openocd/scripts"

# v0.6.73 ELF addresses (RAM layout shifted again due to new atomic:
# conn_monitor_last_tick_s + new work item).
SYMS = {
    "uptime_s": 0x4083c22c, "thread_role": 0x4083c170,
    "ka_emit_count": 0x4083c290, "ka_consec_fail": 0x4083c28c,
    "reg_success": 0x4083c1b4, "reg_attempts": 0x4083c1b8,
    "first_register_done": 0x4083c188, "reached_network": 0x4083c184,
    "last_error_code": 0x4083c1a8, "last_error_uptime": 0x4083c1a4,
    "in_recovery": 0x4083c190, "recovering_flag": 0x4083c17c,
    "diag_recover_cnt": 0x4083c1b0, "restart_success": 0x4083c1ac,
    "noreg_boots": 0x4083c198, "watchdog_count": 0x4083c1a0,
    "total_resets": 0x4083c18c, "last_reset_reason": 0x4080da34,
    "last_emit_uptime": 0x4083c1bc, "lwm2m_connected": 0x4083c535,
    "boot_burst": 0x4083c194, "detached_total_s": 0x4083c168,
    "conn_mon_last_tick": 0x4083c164,
}
ROLE = {0:"OFF",1:"Det",2:"Chld",3:"Rtr",4:"Ldr"}
RR = {0:"POR",1:"EXT",2:"BRN",3:"SW_LP",8:"SW",16:"WDOG"}

def main():
    mac = sys.argv[1].upper()
    ofs = int(sys.argv[2])
    args = [str(OPENOCD), "-s", str(SCRIPTS),
            "-c", f"adapter serial {mac}",
            "-c", f"gdb port {13334+ofs}",
            "-c", f"telnet port {14445+ofs}",
            "-c", f"tcl port {16670+ofs}",
            "-f", "board/esp32c6-builtin.cfg",
            "-c", "init", "-c", "halt"]
    for addr in SYMS.values():
        args += ["-c", f"mdw 0x{addr:08x} 1"]
    args += ["-c", "resume", "-c", "shutdown"]
    r = subprocess.run(args, capture_output=True, text=True, timeout=30)
    text = r.stdout + r.stderr
    if "Target halted" not in text:
        print(f"{mac} JTAG_FAIL")
        return 2
    out = {}
    for name, addr in SYMS.items():
        m = re.search(rf"0x{addr:08x}:\s+([0-9a-f]+)", text)
        if m:
            v = int(m.group(1), 16)
            if name == "last_error_code" and v >= 0x80000000: v -= 0x100000000
            elif name == "last_reset_reason" and v >= 0x80000000: v -= 0x100000000
            elif name == "lwm2m_connected": v &= 0xff
            out[name] = v
    fmt = (
        f"up={out.get('uptime_s')}s role={ROLE.get(out.get('thread_role',0)&0xff,'?')} "
        f"reg={out.get('reg_success')}/{out.get('reg_attempts')} "
        f"first={bool(out.get('first_register_done'))} netok={bool(out.get('reached_network'))} "
        f"conn={bool(out.get('lwm2m_connected'))} "
        f"ka={out.get('ka_emit_count')}/fail{out.get('ka_consec_fail')} "
        f"lastEmit={out.get('last_emit_uptime')}s "
        f"err={out.get('last_error_code')}@{out.get('last_error_uptime')}s "
        f"recov={out.get('diag_recover_cnt')} "
        f"inRec={bool(out.get('in_recovery'))} "
        f"wdog={out.get('watchdog_count')} "
        f"noreg={out.get('noreg_boots')} "
        f"TR={out.get('total_resets')} rr={RR.get(out.get('last_reset_reason',0)&0xff,'?')} "
        f"burst={out.get('boot_burst')} detTot={out.get('detached_total_s')}s "
        f"cmTick={out.get('conn_mon_last_tick')}s"
    )
    print(f"{mac} {fmt}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
