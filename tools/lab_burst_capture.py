#!/usr/bin/env python3
"""Correlated LwM2M-burst power capture — the measurement this project has been
chasing for months ("USB cliff": does the REGISTER + observe push draw a current
spike big enough to trip a USB host?).

Runs the FNB-C2 logger and the node's UART0 console AT THE SAME TIME, reboots the
node, and lines the two up on one clock so every current sample can be attributed
to a firmware phase (boot -> Thread attach -> DNS-SD -> REGISTER -> observe push).

  python tools/lab_burst_capture.py [--seconds 150] [--console COM6]

Outputs (tools/):
  lab_burst_<ts>.csv    per-sample current with the phase it belongs to
  lab_burst_<ts>.log    raw console
and prints a per-phase current summary (idle / peak / p95) plus the headline
number: the peak current during the REGISTER burst vs the idle floor.
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import re
import subprocess
import sys
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Console markers -> phase name. Ordered; each marker opens a phase that runs
# until the next marker fires.
PHASE_MARKERS = [
    ("=== AMI LwM2M Node", "boot"),
    ("Thread attached", "thread-attached"),
    ("DNS-SD lookup attempt", "dns-sd"),
    ("DNS-SD resolved", "pre-register"),
    ("REGISTER jitter", "register-jitter"),
    ("Boot watchdog disarmed", "registered"),
    ("Object 33000", "telemetry"),
]

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=150)
    ap.add_argument("--console", default="COM6")
    ap.add_argument("--no-reboot", action="store_true",
                    help="capture without rebooting (for steady-state bursts)")
    args = ap.parse_args()

    # Line-buffer stdout: this script runs for minutes and is usually launched
    # from a non-TTY (task runner / redirect), where full buffering would hide
    # every progress line until exit.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    import serial  # noqa: E402  (pyserial, present in the Zephyr venv)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    csv_path = HERE / f"lab_burst_{stamp}.csv"
    log_path = HERE / f"lab_burst_{stamp}.log"
    fnb_csv = HERE / f"lab_burst_fnb_{stamp}.csv"

    # 1) FNB logger in the background, writing its own per-sample CSV.
    fnb = subprocess.Popen(
        [sys.executable, str(HERE / "fnb_power_logger.py"),
         "--duration", str(args.seconds + 10), "--interval", "1",
         "--csv", str(fnb_csv)],
        cwd=str(HERE.parent), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    time.sleep(4)  # let it enumerate and start sampling

    # 2) Console reader thread, timestamping every line on the SAME clock.
    events: list[tuple[float, str]] = []
    raw: list[str] = []
    stop = threading.Event()

    def reader():
        try:
            s = serial.Serial(args.console, 115200, timeout=0.3)
        except Exception as e:  # console is optional — power data still useful
            print(f"[warn] console {args.console}: {e}")
            return
        if not args.no_reboot:
            s.reset_input_buffer()
            s.write(b"kernel reboot cold\r\n")
            s.flush()
        buf = ""
        while not stop.is_set():
            try:
                buf += ANSI.sub("", s.read(4096).decode("utf-8", "replace"))
            except Exception:
                break
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.rstrip("\r")
                if line.strip():
                    events.append((time.time(), line))
                    raw.append(line)
        s.close()

    t = threading.Thread(target=reader, daemon=True)
    t0 = time.time()
    t.start()
    print(f"capturing {args.seconds}s (FNB + console{'' if args.no_reboot else ', node rebooted'}) ...")
    while time.time() - t0 < args.seconds:
        time.sleep(1)
    stop.set()
    t.join(timeout=5)
    try:
        fnb.wait(timeout=20)
    except Exception:
        fnb.terminate()

    log_path.write_text("\n".join(raw), encoding="utf-8")

    # 3) Build the phase timeline from console events.
    phases: list[tuple[float, str]] = []
    for ts, line in events:
        for marker, name in PHASE_MARKERS:
            if marker in line:
                if not phases or phases[-1][1] != name:
                    phases.append((ts, name))
                break

    def phase_at(ts: float) -> str:
        cur = "pre-boot"
        for pts, name in phases:
            if ts >= pts:
                cur = name
            else:
                break
        return cur

    # 4) Join the FNB samples to phases.
    rows = []
    try:
        for r in csv.DictReader(open(fnb_csv, encoding="utf-8")):
            try:
                rel = float(r["t_rel_s"])
                ma = float(r["i_ma"])
            except Exception:
                continue
            # fnb_power_logger starts ~4 s before t0
            rows.append((t0 - 4 + rel, ma, r.get("v_bus", "")))
    except FileNotFoundError:
        print("[warn] no FNB csv — was the meter connected?")

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["t_rel_s", "i_ma", "v_bus", "phase"])
        for ts, ma, v in rows:
            w.writerow([round(ts - t0, 3), ma, v, phase_at(ts)])

    # 5) Report.
    print("\n=== PHASE TIMELINE (console) ===")
    for pts, name in phases:
        print(f"  t+{pts - t0:6.1f}s  {name}")

    # The FNB-C2 emits occasional CRC-valid but garbage samples (observed
    # 1290.x / 2097 / 8388 mA against a 55 mA floor). They are decode artifacts,
    # not current: a real ESP32-C6 cannot draw >1 A. Treat anything above
    # GLITCH_X * the global median as a decode glitch, count them, and report
    # robust statistics so the headline number is defensible.
    GLITCH_X = 8.0
    all_ma = sorted(m for _, m, _ in rows)
    gmed = all_ma[len(all_ma) // 2] if all_ma else 0.0
    glitch_cut = max(gmed * GLITCH_X, 400.0)
    n_glitch = sum(1 for m in all_ma if m > glitch_cut)

    def stats(v: list[float]) -> tuple[float, float, float, int]:
        clean = sorted(x for x in v if x <= glitch_cut)
        if not clean:
            return (0.0, 0.0, 0.0, len(v))
        return (clean[len(clean) // 2],
                clean[min(int(len(clean) * 0.99), len(clean) - 1)],
                clean[-1],
                len(v) - len(clean))

    print("\n=== CURRENT BY PHASE (mA, decode glitches excluded) ===")
    by: dict[str, list[float]] = {}
    for ts, ma, _ in rows:
        by.setdefault(phase_at(ts), []).append(ma)
    order = ["pre-boot"] + [n for _, n in PHASE_MARKERS]
    idle = None
    peak_clean = 0.0
    for name in order:
        v = by.get(name)
        if not v:
            continue
        med, p99, mx, ng = stats(v)
        if name == "pre-boot":
            idle = med
        peak_clean = max(peak_clean, mx)
        print(f"  {name:16} n={len(v):5}  median={med:7.1f}  p99={p99:7.1f}  max={mx:7.1f}"
              + (f"   [{ng} glitch]" if ng else ""))

    print("\n=== HEADLINE ===")
    if idle:
        print(f"  idle floor        : {idle:.1f} mA")
    print(f"  peak (clean)      : {peak_clean:.1f} mA")
    if idle:
        print(f"  burst above idle  : +{peak_clean - idle:.1f} mA")
    print(f"  USB-2.0 host cap  : 500 mA  ->  headroom {500 - peak_clean:.0f} mA")
    print(f"  decode glitches   : {n_glitch}/{len(all_ma)} samples > {glitch_cut:.0f} mA (excluded)")
    print(f"\n  per-sample data: {csv_path.name}\n  console       : {log_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
