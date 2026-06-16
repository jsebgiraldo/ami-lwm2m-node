"""JTAG long-soak monitor for Lab 26 — reads diag counters every tick.

Usage: python tools/lab26_soak.py [ticks] [interval_s]
"""
import pathlib, re, subprocess, sys, time

REPO = pathlib.Path(__file__).resolve().parent.parent
OPENOCD = REPO / "tools" / "openocd-esp32" / "openocd-esp32" / "bin" / "openocd.exe"
SCRIPTS = REPO / "tools" / "openocd-esp32" / "openocd-esp32" / "share" / "openocd" / "scripts"
import sys as _s
SERIAL = _s.argv[3] if len(_s.argv) > 3 else "10:51:DB:1B:F6:E4"

# Symbol addresses (v0.6.65 ELF, .dram0.bss layout)
SYMS = [
    ("uptime",  0x40845cdc),
    ("role",    0x40845c44),
    ("regS",    0x40845c80),
    ("regA",    0x40845c84),
    ("emit",    0x40845c88),
    ("err_c",   0x40845c74),
    ("recov",   0x40845c7c),
    ("wdog",    0x40845c6c),
    ("noreg",   0x40845c64),
]


def read_once():
    args = [
        str(OPENOCD),
        "-s", str(SCRIPTS),
        "-c", f"adapter serial {SERIAL}",
        "-c", "gdb port 13333",
        "-c", "telnet port 14444",
        "-c", "tcl port 16669",
        "-f", "board/esp32c6-builtin.cfg",
        "-c", "init",
        "-c", "halt",
    ]
    for _, addr in SYMS:
        args += ["-c", f"mdw 0x{addr:08x} 1"]
    args += ["-c", "resume", "-c", "shutdown"]

    r = subprocess.run(args, capture_output=True, text=True, timeout=25)
    text = r.stdout + r.stderr
    out = {}
    for name, addr in SYMS:
        m = re.search(rf"0x{addr:08x}: ([0-9a-f]+)", text)
        if m:
            val = int(m.group(1), 16)
            out[name] = val if val < 0x80000000 else val - 0x100000000
    return out


def main():
    ticks = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    interval = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    role_name = {0: "OFF", 1: "Det", 2: "Chld", 3: "Rtr", 4: "Ldr"}
    print(f"=== Lab 26 soak: {ticks} ticks @ {interval}s ===")
    print(f"{'tick':<5}{'up_s':<7}{'role':<6}{'regS/A':<10}{'emit':<7}{'err':<7}{'recov':<6}{'wdog':<5}{'noreg':<6}")
    for i in range(1, ticks + 1):
        try:
            r = read_once()
            print(
                f"{i:<5}{r.get('uptime','?'):<7}"
                f"{role_name.get(r.get('role'), '?'):<6}"
                f"{r.get('regS','?')}/{r.get('regA','?'):<7}"
                f"{r.get('emit','?'):<7}"
                f"{r.get('err_c','?'):<7}"
                f"{r.get('recov','?'):<6}"
                f"{r.get('wdog','?'):<5}"
                f"{r.get('noreg','?'):<6}",
                flush=True,
            )
        except subprocess.TimeoutExpired:
            print(f"{i:<5} TIMEOUT", flush=True)
        except Exception as e:
            print(f"{i:<5} ERR: {e}", flush=True)
        if i < ticks:
            time.sleep(interval)


if __name__ == "__main__":
    main()
