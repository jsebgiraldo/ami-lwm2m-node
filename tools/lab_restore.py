#!/usr/bin/env python3
"""Bring the bench back up after a host reboot / dongle drop / WSL restart.

Three things do NOT survive and bite in the same order every time:

  1. usbipd. A plain `usbipd attach` dies with the process that issued it, and
     when the RCP disappears otbr-agent exits 5 and the whole mesh goes with it.
     Use --auto-attach and keep it running.
  2. Docker Desktop re-hijacks /var/run/docker.sock inside the distro, so
     containers silently run in ITS VM — a different network namespace with no
     wpan0, which makes ThingsBoard unreachable from the mesh even though it
     logs a healthy startup.
  3. otbr-agent comes back with `srp server` DISABLED, and without the SRP
     service the node's DNS-SD fails with err=-2 and it never registers.

  python tools/lab_restore.py            # check + fix what is down
  python tools/lab_restore.py --check    # report only, change nothing
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time

DISTRO = "Ubuntu-24.04"
BUSID = "3-9"          # SONOFF ZBDongle-E (10c4:ea60)


def wsl(cmd: str, t: int = 60) -> str:
    r = subprocess.run(["wsl.exe", "-d", DISTRO, "-u", "root", "--", "bash", "-lc", cmd],
                       capture_output=True, text=True, timeout=t)
    return (r.stdout + r.stderr).strip()


def ps(cmd: str, t: int = 60) -> str:
    r = subprocess.run(["powershell.exe", "-NoProfile", "-Command", cmd],
                       capture_output=True, text=True, timeout=t)
    return (r.stdout + r.stderr).strip()


def ok(msg):
    print(f"  [ OK ] {msg}")


def fix(msg):
    print(f"  [FIX ] {msg}")


def bad(msg):
    print(f"  [FAIL] {msg}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="report only")
    args = ap.parse_args()
    ro = args.check
    problems = 0

    print("== 1. RCP passed through to WSL ==")
    if "/dev/ttyUSB0" in wsl("ls /dev/ttyUSB0 2>&1"):
        ok("/dev/ttyUSB0 present")
    elif ro:
        bad("no /dev/ttyUSB0 — run without --check, or: usbipd attach --wsl --busid "
            f"{BUSID} --auto-attach"); problems += 1
    else:
        fix("attaching the dongle (keep the --auto-attach process alive!)")
        subprocess.Popen(["powershell.exe", "-NoProfile", "-Command",
                          f"usbipd attach --wsl --busid {BUSID} --auto-attach"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(12):
            time.sleep(3)
            if "/dev/ttyUSB0" in wsl("ls /dev/ttyUSB0 2>&1"):
                ok("/dev/ttyUSB0 appeared")
                break
        else:
            bad("dongle never appeared — check `usbipd list` and the cable"); problems += 1

    print("== 2. docker engine is the distro's, not Docker Desktop ==")
    who = wsl("timeout 10 docker info --format '{{.Name}}' 2>&1 | head -1")
    if "docker-desktop" in who:
        if ro:
            bad("Docker Desktop hijacked the socket — containers have no wpan0"); problems += 1
        else:
            fix("reclaiming the native socket (systemctl restart docker.socket docker.service)")
            wsl("systemctl restart docker.socket docker.service", t=120)
            time.sleep(6)
            who = wsl("timeout 10 docker info --format '{{.Name}}' 2>&1 | head -1")
            (ok if "docker-desktop" not in who else bad)(f"engine now: {who}")
    else:
        ok(f"engine: {who}")

    print("== 3. OTBR ==")
    st = wsl("ot-ctl state 2>&1 | head -1")
    if st not in ("leader", "router", "child"):
        if ro:
            bad(f"otbr state: {st}"); problems += 1
        else:
            fix("restarting otbr-agent")
            wsl("systemctl restart otbr-agent", t=90)
            for _ in range(18):
                time.sleep(5)
                st = wsl("ot-ctl state 2>&1 | head -1")
                if st in ("leader", "router", "child"):
                    break
    (ok if st in ("leader", "router", "child") else bad)(f"otbr state: {st}")

    print("== 4. SRP server (node DNS-SD depends on it) ==")
    srp = wsl("ot-ctl srp server state 2>&1 | head -1")
    if srp != "running":
        if ro:
            bad(f"srp server: {srp}"); problems += 1
        else:
            fix("ot-ctl srp server enable")
            wsl("ot-ctl srp server enable")
            time.sleep(3)
            srp = wsl("ot-ctl srp server state 2>&1 | head -1")
    (ok if srp == "running" else bad)(f"srp server: {srp}")

    print("== 5. _lwm2m._udp service advertised ==")
    svc = wsl("ot-ctl srp server service 2>&1")
    if "_lwm2m._udp" in svc and "deleted: false" in svc:
        port = re.search(r"port:\s*(\d+)", svc)
        ok(f"advertised (port {port.group(1) if port else '?'})")
    elif ro:
        bad("not advertised — python tools/lab_tb/lab_tb_srp.py publish"); problems += 1
    else:
        fix("publishing (must run from Windows: the venv has the deps)")
        out = ps("cd C:/Users/jsgir/Documents/UNAL/Unal-Flash-tool/firmware/ami-lwm2m-node; "
                 "& C:/Users/jsgir/Documents/ESP32/.venv/Scripts/python.exe "
                 "tools/lab_tb/lab_tb_srp.py publish", t=180)
        svc = wsl("ot-ctl srp server service 2>&1")
        # NOTE: lab_tb_srp's own DNS verification reports a false negative here
        # (its ot-ctl query from the OTBR itself fails while the node resolves
        # fine) — trust the srp server table, not that exit code.
        (ok if "_lwm2m._udp" in svc else bad)("published" if "_lwm2m._udp" in svc
                                              else f"publish failed: {out[-200:]}")

    print("== 6. ThingsBoard ==")
    tb = wsl("docker ps --format '{{.Names}} {{.Status}}' 2>/dev/null | grep tb-lab | head -1")
    if "tb-lab" in tb:
        ok(tb)
    elif ro:
        bad("tb-lab not running — tools/lab_tb/lab_tb.ps1 -Action up"); problems += 1
    else:
        fix("starting the bench ThingsBoard")
        wsl("cd /mnt/c/Users/jsgir/Documents/UNAL/Unal-Flash-tool/firmware/ami-lwm2m-node/"
            "tools/lab_tb && bash lab_tb_up.sh up", t=600)
        tb = wsl("docker ps --format '{{.Names}} {{.Status}}' 2>/dev/null | grep tb-lab | head -1")
        (ok if "tb-lab" in tb else bad)(tb or "tb-lab still down")

    print(f"\n{'CHECK' if ro else 'RESTORE'} done"
          + (f" — {problems} problem(s)" if problems else ""))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
