#!/usr/bin/env python3
"""Multi-port USB-Serial-JTAG console capture + stack/overflow analyzer.

For the EXTREME-AUDIT round: flash the 16-node sample with build_audit
(THREAD_ANALYZER_AUTO every 30s + stack sentinel + HW stack guard), then run
this to open every connected ESP32-C6 console at once, log each to its own
file, and continuously parse the THREAD_ANALYZER output to surface:

  * the per-thread stack HIGH-WATER mark (max usage% ever seen) per board
  * any thread above --warn-pct (default 80%) — the Bug-#5 early-warning
  * any stack-overflow / sentinel / fault / panic / assert line, immediately

Console is the native USB-Serial-JTAG CDC at 115200. Excludes the other-
project board (COM68 / MAC ...ad64) by default — NEVER touched.

Usage:
    python tools/audit_console_capture.py                 # all boards, forever
    python tools/audit_console_capture.py --duration 1800 # 30 min then summary
    python tools/audit_console_capture.py --ports COM23,COM26 --warn-pct 75
"""
from __future__ import annotations
import argparse
import datetime as dt
import os
import re
import subprocess
import sys
import threading
import time

import fleet_common as fc

fc.bootstrap_venv()
import serial  # noqa: E402  (pyserial, from the Zephyr venv)

EXCLUDE_PATHHASH = ("1187AD8F",)  # COM68 / ad64 — other project, do not touch
LOGDIR = "logs/audit_console"

# THREAD_ANALYZER (USE_PRINTK) line, e.g.:
#  coap_keepalive     : STACK: unused 3000 usage 1096 / 4096 (26 %); CPU: 1 %
STACK_RE = re.compile(
    r"^\s*(?P<name>.+?)\s*:\s*STACK:\s*unused\s+(?P<unused>\d+)\s+usage\s+"
    r"(?P<usage>\d+)\s*/\s*(?P<size>\d+)\s*\((?P<pct>\d+)\s*%\)"
    r"(?:;\s*CPU:\s*(?P<cpu>\d+)\s*%)?")
ALERT_RE = re.compile(
    r"overflow|sentinel|STACK CHECK|FATAL|FAULT|panic|ASSERT|Halting|"
    r"stack guard|USAGE FAULT|delivery-stall|HW watchdog", re.IGNORECASE)


def detect_ports() -> list[str]:
    ps = ("Get-PnpDevice -Class Ports -Status OK | "
          "Where-Object {$_.InstanceId -match 'VID_303A&PID_1001'} | "
          "ForEach-Object { if ($_.FriendlyName -match '\\((COM\\d+)\\)') "
          "{ \"$($matches[1]) $($_.InstanceId)\" } }")
    out = subprocess.check_output(["powershell", "-NoProfile", "-Command", ps],
                                  text=True, timeout=15)
    ports = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        com = line.split()[0]
        if any(h in line for h in EXCLUDE_PATHHASH):
            print(f"  [skip] {com} (excluded — other project)")
            continue
        ports.append(com)
    return sorted(set(ports), key=lambda c: int(c[3:]))


class BoardCapture(threading.Thread):
    def __init__(self, com: str, baud: int, stop: threading.Event):
        super().__init__(daemon=True)
        self.com = com
        self.baud = baud
        self.stop = stop
        self.hi = {}          # thread name -> max usage%
        self.cpu = {}         # thread name -> last CPU%
        self.alerts = []      # (ts, line)
        self.lines = 0
        self.err = None

    def run(self):
        path = os.path.join(LOGDIR, f"{self.com}.log")
        try:
            ser = serial.Serial(self.com, self.baud, timeout=1)
        except Exception as e:
            self.err = str(e)
            print(f"  [{self.com}] OPEN FAIL: {e}")
            return
        with open(path, "w", encoding="utf-8", errors="replace") as f:
            f.write(f"# capture start {dt.datetime.now().isoformat()} {self.com}\n")
            while not self.stop.is_set():
                try:
                    raw = ser.readline()
                except Exception as e:
                    self.err = str(e)
                    break
                if not raw:
                    continue
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                self.lines += 1
                f.write(line + "\n")
                f.flush()
                m = STACK_RE.match(line)
                if m:
                    name = m.group("name").strip()
                    pct = int(m.group("pct"))
                    self.hi[name] = max(self.hi.get(name, 0), pct)
                    if m.group("cpu") is not None:
                        self.cpu[name] = int(m.group("cpu"))
                elif ALERT_RE.search(line):
                    ts = dt.datetime.now().strftime("%H:%M:%S")
                    self.alerts.append((ts, line))
                    print(f"  !! [{self.com} {ts}] {line.strip()}")
        try:
            ser.close()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ports", default=None, help="comma list; default auto")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--duration", type=int, default=0, help="seconds; 0=forever")
    ap.add_argument("--warn-pct", type=int, default=80)
    ap.add_argument("--summary-every", type=int, default=60)
    args = ap.parse_args()

    os.makedirs(LOGDIR, exist_ok=True)
    ports = args.ports.split(",") if args.ports else detect_ports()
    if not ports:
        print("No ESP32-C6 console ports detected.")
        return 1
    print(f"[audit-console] capturing {len(ports)} boards: {', '.join(ports)}")
    print(f"  logs -> {LOGDIR}/<COM>.log   warn>={args.warn_pct}%   "
          f"{'forever' if not args.duration else str(args.duration)+'s'}")

    stop = threading.Event()
    caps = [BoardCapture(c, args.baud, stop) for c in ports]
    for c in caps:
        c.start()

    t0 = time.time()
    try:
        while not args.duration or (time.time() - t0) < args.duration:
            time.sleep(args.summary_every)
            print(f"\n===== STACK HIGH-WATER @ +{int(time.time()-t0)}s "
                  f"({dt.datetime.now():%H:%M:%S}) =====")
            worst = []
            for c in caps:
                for name, pct in c.hi.items():
                    worst.append((pct, c.com, name, c.cpu.get(name)))
            worst.sort(reverse=True)
            shown = 0
            for pct, com, name, cpu in worst:
                flag = "  <-- WARN" if pct >= args.warn_pct else ""
                if pct >= args.warn_pct or shown < 12:
                    cpus = f" cpu={cpu}%" if cpu is not None else ""
                    print(f"  {pct:3d}%  {com:6} {name:18}{cpus}{flag}")
                    shown += 1
            live = sum(1 for c in caps if c.lines > 0 and not c.err)
            alerts = sum(len(c.alerts) for c in caps)
            print(f"  live={live}/{len(caps)} boards   alerts={alerts}")
    except KeyboardInterrupt:
        print("\n[audit-console] stopping...")
    finally:
        stop.set()
        time.sleep(1.5)

    # ── final report ──
    print("\n" + "=" * 60)
    print("FINAL AUDIT SUMMARY")
    print("=" * 60)
    allhi = {}
    for c in caps:
        for name, pct in c.hi.items():
            allhi.setdefault(name, []).append(pct)
    print("Per-thread stack high-water across the fleet (max / boards seen):")
    for name in sorted(allhi, key=lambda n: -max(allhi[n])):
        v = allhi[name]
        print(f"  {max(v):3d}% max  {sum(v)//len(v):3d}% avg  n={len(v):2d}  {name}")
    print("\nAlerts:")
    any_alert = False
    for c in caps:
        for ts, line in c.alerts:
            print(f"  [{c.com} {ts}] {line.strip()}")
            any_alert = True
    if not any_alert:
        print("  none — no overflow/fault/sentinel/watchdog lines seen")
    dead = [c.com for c in caps if c.err or c.lines == 0]
    if dead:
        print(f"\nNo-data boards (USB wedge or silent): {', '.join(dead)}")
    print(f"\nlogs in {LOGDIR}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
