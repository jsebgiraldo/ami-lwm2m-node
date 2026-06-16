#!/usr/bin/env python3
"""Stream the firmware LOG over SEGGER-RTT via OpenOCD JTAG — non-intrusive.

Resolves the _SEGGER_RTT control-block address from the RTT build's ELF, starts
OpenOCD with an RTT server (no halt), and tees the live log stream to stdout and
logs/rtt_<board>.log. Catches the exact reboot-reason line ("REBOOT (<why>)")
the firmware emits right before sys_reboot — which names the watchdog/path that
is rebooting the board at ~700 s.

Usage: python tools/rtt_stream.py [board=1494] [seconds=900]
"""
import os
import pathlib
import re
import socket
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
OPENOCD = REPO / "tools" / "openocd-esp32" / "openocd-esp32" / "bin" / "openocd.exe"
SCRIPTS = REPO / "tools" / "openocd-esp32" / "openocd-esp32" / "share" / "openocd" / "scripts"
ZP = pathlib.Path.home() / "Documents" / "ESP32" / "zephyrproject"
ELF = ZP / "build_resprobe_rtt" / "ami-lwm2m-node" / "zephyr" / "zephyr.elf"
NM = pathlib.Path.home() / "zephyr-sdk-0.17.0" / "riscv64-zephyr-elf" / "bin" / "riscv64-zephyr-elf-gcc-nm.exe"

NODES = {"1494": "10:51:db:1c:14:94", "f7b4": "10:51:db:1b:f7:b4", "fbb8": "10:51:db:1b:fb:b8"}
RTT_PORT = 19021


def rtt_addr():
    out = subprocess.run([str(NM), str(ELF)], capture_output=True, text=True).stdout
    for line in out.splitlines():
        p = line.split()
        if len(p) == 3 and p[2] == "_SEGGER_RTT":
            return int(p[0], 16)
    raise SystemExit("could not find _SEGGER_RTT in ELF")


def main():
    board = sys.argv[1] if len(sys.argv) > 1 else "1494"
    secs = int(sys.argv[2]) if len(sys.argv) > 2 else 900
    mac = NODES[board].upper()
    addr = rtt_addr()
    print(f"[rtt] board={board} mac={mac} _SEGGER_RTT=0x{addr:08x} dur={secs}s", flush=True)
    logp = REPO / "logs" / f"rtt_{board}.log"
    logp.parent.mkdir(exist_ok=True)

    oocd = [str(OPENOCD), "-s", str(SCRIPTS),
            "-c", f"adapter serial {mac}",
            "-c", "gdb port 13344", "-c", "telnet port 14455", "-c", "tcl port 16680",
            "-f", "board/esp32c6-builtin.cfg",
            "-c", "init",
            "-c", f"rtt setup 0x{addr:08x} 0x800 \"SEGGER RTT\"",
            "-c", "rtt start",
            "-c", f"rtt server start {RTT_PORT} 0"]
    oocd = [c.replace("\\", "/") for c in oocd]
    oo_log = open(REPO / "logs" / f"rtt_{board}.openocd.log", "w")
    proc = subprocess.Popen(oocd, stdout=oo_log, stderr=subprocess.STDOUT)
    time.sleep(4)  # let OpenOCD attach + start the rtt server

    deadline = time.time() + secs
    with open(logp, "w", encoding="utf-8", errors="replace") as f:
        while time.time() < deadline:
            try:
                s = socket.create_connection(("127.0.0.1", RTT_PORT), timeout=5)
            except Exception as e:
                print(f"[rtt] connect retry ({e})", flush=True); time.sleep(2); continue
            s.settimeout(2.0)
            buf = b""
            while time.time() < deadline:
                try:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        txt = line.decode("utf-8", "replace").rstrip("\r")
                        ts = time.strftime("%H:%M:%S")
                        out = f"{ts} {txt}"
                        print(out, flush=True)
                        f.write(out + "\n"); f.flush()
                except socket.timeout:
                    continue
                except Exception:
                    break
            try: s.close()
            except Exception: pass
    try:
        proc.terminate()
    except Exception:
        pass
    print("[rtt] done", flush=True)


if __name__ == "__main__":
    main()
