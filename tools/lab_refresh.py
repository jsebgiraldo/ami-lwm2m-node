#!/usr/bin/env python3
"""One self-contained lab-dashboard refresh cycle:

  1. capture fresh FNB-C2 power (bounded run so nothing is left orphaned),
  2. run the e2e monitor INSIDE WSL (native ot-ctl + the OMR route lives there),
  3. splice the fresh snapshot into tools/lab_dashboard_live.html.

Run this each cycle with the venv python, THEN (re)publish
tools/lab_dashboard_live.html as the Artifact to keep the same URL. Designed to
be driven on an interval (/loop): every step is self-healing — a dead source
degrades to null in the snapshot, never a crash.

  C:/Users/jsgir/Documents/ESP32/.venv/Scripts/python.exe tools/lab_refresh.py
"""
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
REPO_WSL = "/mnt/c/Users/jsgir/Documents/UNAL/Unal-Flash-tool/firmware/ami-lwm2m-node"
MONITOR_S = 36
FNB_S = MONITOR_S + 18          # cover the monitor window + margin, then exit clean
# Suppress child console windows so an unattended Task Scheduler run stays silent
# (no window flashing every cycle). No-op on non-Windows.
CF = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 1) FNB power logger (Windows HID), bounded so it exits on its own.
fnb = subprocess.Popen(
    [sys.executable, str(HERE / "fnb_power_logger.py"),
     "--duration", str(FNB_S), "--interval", "2"],
    cwd=str(REPO), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=CF)
time.sleep(5)                    # let it enumerate + write one sample

# 2) e2e monitor in WSL — writes tools/lab_e2e_snapshot.json.
mon = subprocess.run(
    ["wsl.exe", "-d", "Ubuntu-24.04", "-u", "root", "--", "bash", "-lc",
     f"cd {REPO_WSL} && python3 tools/lab_e2e_monitor.py "
     f"--otbr-mode native --probe-mode local --interval 12 --duration {MONITOR_S}"],
    capture_output=True, text=True, creationflags=CF)
for line in mon.stdout.strip().splitlines()[-2:]:
    print(line)

# 3) splice the fresh snapshot into the publishable HTML.
subprocess.run([sys.executable, str(HERE / "lab_dashboard_publish.py")], check=True,
               creationflags=CF)

try:
    fnb.wait(timeout=FNB_S)
except Exception:
    fnb.terminate()
print("refresh cycle done -> (re)publish tools/lab_dashboard_live.html")
