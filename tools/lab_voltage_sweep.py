#!/usr/bin/env python3
"""Find the voltage at which the node actually dies — the number nobody has.

docs/PENDIENTES.md §1 asks it directly: "si VDD baja de 3.0 V en la ventana de
60 s desde el power-on, es alimentación; si se mantiene sobre 3.1 V y el nodo
muere igual, es firmware/radio". Nobody could answer because it needs a
programmable supply. The PPK2 is one.

Two modes, and they answer different questions:

  --mode sag   The node is running; walk the rail DOWN in steps and watch when
               it stops. Models a supply that droops under load — the classic
               "the hub sagged" story.

  --mode boot  At each voltage, power-cycle and see whether it comes up at all.
               This is the one that matters for the mass power-on scenario:
               PPK2 measurements put the power-on inrush at ~247 mA on the 5 V
               bus, the largest event in the whole lifecycle, so N nodes
               energising together sag the rail exactly when each of them needs
               its biggest gulp. "Will it boot at 4.2 V?" is the real question.

Liveness is judged from the UART0 console (the node talks even when it cannot
join the mesh), with the current draw as a second opinion: a booted ESP32-C6
pulls tens of mA, a dead one pulls ~0.

  python tools/lab_voltage_sweep.py --mode boot --from-mv 5000 --to-mv 2600 --step-mv 200
  python tools/lab_voltage_sweep.py --mode sag  --from-mv 5000 --to-mv 2600 --step-mv 100

WIRING: PPK2 VOUT -> the pin you are sweeping (VSYS/5V or 3V3), GND<->GND, the
board's own USB UNPLUGGED, FTDI on D6/GND for the console.
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import re
import statistics as st
import sys
import threading
import time
from lab_paths import captures_dir

HERE = pathlib.Path(__file__).resolve().parent
ANSI = re.compile(r"\x1b\[[0-9;]*m")

ALIVE_MARKERS = ("Booting Zephyr", "AMI LwM2M Node", "thread_analyzer",
                 "ami_lwm2m", "boot-burst", "Thread")


class Console:
    """Background console reader; tells us what the node said since last check."""

    def __init__(self, port: str):
        self.lines: list[tuple[float, str]] = []
        self._stop = threading.Event()
        self._port = port
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        import serial
        try:
            s = serial.Serial(self._port, 115200, timeout=0.3)
        except Exception as e:
            print(f"[warn] console {self._port}: {e}")
            return
        buf = ""
        while not self._stop.is_set():
            try:
                buf += ANSI.sub("", s.read(4096).decode("utf-8", "replace"))
            except Exception:
                break
            while "\n" in buf:
                ln, buf = buf.split("\n", 1)
                if ln.strip():
                    self.lines.append((time.time(), ln.rstrip("\r")))
        s.close()

    def since(self, t: float) -> list[str]:
        return [l for ts, l in self.lines if ts >= t]

    def stop(self):
        self._stop.set()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("boot", "sag"), default="boot")
    ap.add_argument("--from-mv", type=int, default=5000)
    ap.add_argument("--to-mv", type=int, default=2600)
    ap.add_argument("--step-mv", type=int, default=200)
    ap.add_argument("--dwell", type=float, default=25.0,
                    help="seconds to observe at each voltage")
    ap.add_argument("--ppk", default="COM9")
    ap.add_argument("--console", default="COM6")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    from ppk2_api.ppk2_api import PPK2_API

    if args.from_mv > 5000 or args.to_mv < 800:
        print("PPK2 source range is 800-5000 mV")
        return 1

    ppk = PPK2_API(args.ppk)
    ppk.get_modifiers()
    ppk.use_source_meter()

    con = Console(args.console)
    out = captures_dir() / f"lab_vsweep_{args.mode}_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(
            ["mv", "alive", "booted", "attached", "i_median_ma", "i_peak_ma",
             "console_lines", "note"])

    print(f"mode={args.mode}  {args.from_mv} -> {args.to_mv} mV  step {args.step_mv}  "
          f"dwell {args.dwell:.0f}s")
    print("(a booted C6 draws tens of mA; ~0 mA means the rail is up but the chip is not)\n")

    last_alive = None
    first_dead = None
    steps = list(range(args.from_mv, args.to_mv - 1, -abs(args.step_mv)))

    for mv in steps:
        ppk.set_source_voltage(mv)
        if args.mode == "boot":
            ppk.toggle_DUT_power("OFF")
            time.sleep(1.2)
            mark = time.time()
            ppk.toggle_DUT_power("ON")
        else:
            ppk.toggle_DUT_power("ON")     # idempotent; sag mode never cuts power
            mark = time.time()

        ppk.start_measuring()
        samples: list[float] = []
        t0 = time.time()
        while time.time() - t0 < args.dwell:
            raw = ppk.get_data()
            if raw != b"":
                vals, _ = ppk.get_samples(raw)
                samples.extend(v / 1000.0 for v in vals)
            time.sleep(0.01)
        ppk.stop_measuring()

        said = con.since(mark)
        booted = any("Booting Zephyr" in l for l in said)
        attached = any("Thread attached" in l for l in said)
        alive = bool(said) and any(any(m in l for m in ALIVE_MARKERS) for l in said)
        med = st.median(samples) if samples else 0.0
        peak = max(samples) if samples else 0.0
        # A rail that is up but drawing almost nothing means the SoC is not running.
        if med < 5.0:
            alive = False

        note = ""
        if alive:
            last_alive = mv
        elif first_dead is None:
            first_dead = mv
            note = "FIRST FAILURE"

        flag = "ok " if alive else "DEAD"
        print(f"  {mv:5} mV  {flag}  boot={'Y' if booted else '-'} "
              f"attach={'Y' if attached else '-'}  I med={med:6.1f} peak={peak:6.1f} mA  "
              f"lines={len(said):3}{'   <-- ' + note if note else ''}")
        with out.open("a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([mv, int(alive), int(booted), int(attached),
                                    round(med, 2), round(peak, 2), len(said), note])
        if first_dead is not None and mv <= first_dead - 2 * abs(args.step_mv):
            print("  (two steps past the first failure — stopping)")
            break

    con.stop()
    print("\n" + "=" * 58)
    if last_alive is not None:
        print(f"  lowest voltage still ALIVE : {last_alive} mV")
    if first_dead is not None:
        print(f"  first voltage that FAILED  : {first_dead} mV")
        if last_alive:
            print(f"  -> the cliff sits between {first_dead} and {last_alive} mV")
    else:
        print(f"  never failed down to {args.to_mv} mV")
    print(f"  data: {out.name}")
    print("=" * 58)
    print("\nRestoring 5000 mV and powering the node back up.")
    ppk.set_source_voltage(5000)
    ppk.toggle_DUT_power("ON")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
