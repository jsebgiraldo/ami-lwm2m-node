#!/usr/bin/env python3
"""Correlated current capture with the Nordic PPK2 — 100 kHz, and it owns the power.

Supersedes tools/lab_burst_capture.py (FNB-C2, ~100 Hz). Two things change and
both matter:

  * RESOLUTION. The FNB samples at ~100 Hz, so a radio TX burst lasting a few
    hundred microseconds is averaged away; its 262 mA "peak" was a floor. The
    PPK2 samples at ~100 kHz and sees the transient.
  * CONTROL. In source-meter mode the PPK2 SUPPLIES the board, so this script can
    power-cycle it in software (a real POR, verified: "Reset cause: POR=1") and
    capture from the first microsecond of boot. No hands, no USB.

WIRING (the board's own USB must be UNPLUGGED — two supplies on one 3V3 node
otherwise, and with USB gone the ESP32-C6's USB-Serial-JTAG never enumerates,
which is what used to wedge Windows):

    PPK2 VOUT ──► XIAO 3V3      PPK2 GND ──► XIAO GND
    FTDI  RX  ◄── XIAO D6 (UART0 TX, console+shell)   FTDI GND ──► XIAO GND

  python tools/lab_ppk2_capture.py --seconds 60          # power-cycle + capture
  python tools/lab_ppk2_capture.py --seconds 60 --no-cycle   # capture as-is

Outputs tools/lab_ppk2_<ts>.csv (per-sample mA + firmware phase) and .log, then
prints current per phase. NOTE the PPK2 measures the 3V3 RAIL; the FNB measured
the 5 V USB bus. Do not compare the two numbers without converting to power.
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import re
import sys
import threading
import time
from lab_paths import captures_dir

HERE = pathlib.Path(__file__).resolve().parent

PHASE_MARKERS = [
    ("Booting Zephyr", "boot-rom"),
    ("=== AMI LwM2M Node", "app-init"),
    ("boot-burst limiter", "burst-throttle"),
    ("Thread started", "thread-start"),
    ("Thread attached", "attached"),
    ("DNS-SD lookup attempt", "dns-sd"),
    ("DNS-SD resolved", "pre-register"),
    ("Boot watchdog disarmed", "registered"),
]
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--ppk", default="COM9")
    ap.add_argument("--console", default="COM6")
    ap.add_argument("--mv", type=int, default=3300, help="rail voltage in mV")
    ap.add_argument("--no-cycle", action="store_true",
                    help="do not power-cycle; capture the running board")
    ap.add_argument("--off-ms", type=int, default=1500,
                    help="how long to hold the rail down when power-cycling")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    import serial
    from ppk2_api.ppk2_api import PPK2_API

    stamp = time.strftime("%Y%m%d_%H%M%S")
    csv_path = captures_dir() / f"lab_ppk2_{stamp}.csv"
    log_path = captures_dir() / f"lab_ppk2_{stamp}.log"

    ppk = PPK2_API(args.ppk)
    ppk.get_modifiers()
    ppk.use_source_meter()
    ppk.set_source_voltage(args.mv)

    events: list[tuple[float, str]] = []
    stop = threading.Event()

    def reader():
        try:
            s = serial.Serial(args.console, 115200, timeout=0.3)
        except Exception as e:
            print(f"[warn] console {args.console}: {e} — capturing current only")
            return
        buf = ""
        while not stop.is_set():
            try:
                buf += ANSI.sub("", s.read(4096).decode("utf-8", "replace"))
            except Exception:
                break
            while "\n" in buf:
                ln, buf = buf.split("\n", 1)
                if ln.strip():
                    events.append((time.time(), ln.rstrip("\r")))
        s.close()

    th = threading.Thread(target=reader, daemon=True)
    th.start()

    if not args.no_cycle:
        print(f"power-cycling: rail down {args.off_ms} ms ...")
        ppk.toggle_DUT_power("OFF")
        time.sleep(args.off_ms / 1000.0)
    t0 = time.time()
    ppk.toggle_DUT_power("ON")
    ppk.start_measuring()
    print(f"capturing {args.seconds}s at {args.mv} mV ...")

    samples: list[tuple[float, float]] = []   # (t_rel, mA)
    last = t0
    while time.time() - t0 < args.seconds:
        raw = ppk.get_data()
        if raw != b"":
            vals, _ = ppk.get_samples(raw)
            now = time.time()
            # PPK2 returns a block of evenly spaced samples; spread them across
            # the wall-clock window they arrived in so they line up with console
            # timestamps taken on the same clock.
            n = len(vals)
            if n:
                dt = (now - last) / n
                for i, v in enumerate(vals):
                    samples.append((last + i * dt - t0, v / 1000.0))
                last = now
        time.sleep(0.005)
    ppk.stop_measuring()
    stop.set()
    th.join(timeout=3)
    print("capture done — rail LEFT ON")

    log_path.write_text("\n".join(l for _, l in events), encoding="utf-8")

    phases: list[tuple[float, str]] = []
    for ts, line in events:
        for marker, name in PHASE_MARKERS:
            if marker in line:
                if not phases or phases[-1][1] != name:
                    phases.append((ts - t0, name))
                break

    def phase_at(t: float) -> str:
        cur = "pre-power"
        for pt, name in phases:
            if t >= pt:
                cur = name
            else:
                break
        return cur

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["t_rel_s", "i_ma", "phase"])
        for t, ma in samples:
            w.writerow([round(t, 6), round(ma, 4), phase_at(t)])

    print("\n=== PHASES (console) ===")
    for pt, name in phases:
        print(f"  t+{pt:7.2f}s  {name}")

    print(f"\n=== CURRENT BY PHASE (mA @ {args.mv} mV rail) ===")
    by: dict[str, list[float]] = {}
    for t, ma in samples:
        by.setdefault(phase_at(t), []).append(ma)
    for name in ["pre-power"] + [n for _, n in PHASE_MARKERS]:
        v = by.get(name)
        if not v:
            continue
        s = sorted(v)
        n = len(s)
        print(f"  {name:15} n={n:8}  median={s[n//2]:7.1f}  p99={s[int(n*0.99)]:7.1f}"
              f"  max={s[-1]:7.1f}")

    allv = sorted(ma for _, ma in samples)
    if allv:
        n = len(allv)
        peak = allv[-1]
        idle = allv[n // 2]
        print(f"\n=== HEADLINE ({n:,} samples @ ~{n/args.seconds:.0f}/s) ===")
        print(f"  idle (median) : {idle:.1f} mA")
        print(f"  p99.9         : {allv[int(n*0.999)]:.1f} mA")
        print(f"  PEAK          : {peak:.1f} mA   ({peak*args.mv/1000:.0f} mW)")
    print(f"\n  {csv_path.name}\n  {log_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
