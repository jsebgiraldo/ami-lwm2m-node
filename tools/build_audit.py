#!/usr/bin/env python3
"""Build the EXTREME-AUDIT firmware -> build_audit  (v0.7.9-dlv-audit).

Same as build_ka90 (prod overlays + keepalive90 + the v0.7.9 delivery-gate
that lives in src/) PLUS overlays/audit.conf which adds THREAD_ANALYZER_AUTO,
per-thread CPU stats, stack sentinel + HW stack guard, and INIT_STACKS.

Purpose: flash a 16-node bench sample, capture USB-Serial-JTAG console with
tools/audit_console_capture.py, and find the threads closest to their stack
ceiling / any overflow trip / the per-thread CPU hot-spots — while at the SAME
time validating the new delivery-liveness gate on real hardware.

Reports fw_version 0.7.9-dlv (set in src/main.c). The audit build is feature-
identical to prod at the mesh/LwM2M layer; only observability is added.
"""
from __future__ import annotations
import subprocess, sys
import fleet_common as fc

fc.bootstrap_venv()
env = fc.detect_env(verbose=True)

OVERLAYS = [
    fc.REPO_ROOT / "overlays" / "ftd.conf",
    fc.REPO_ROOT / "overlays" / "resprobe_lwm2m.conf",
    fc.REPO_ROOT / "overlays" / "prod_fat.conf",
    fc.REPO_ROOT / "overlays" / "keepalive90.conf",
    fc.REPO_ROOT / "overlays" / "audit.conf",   # LAST: instrumentation
]
overlay_arg = ";".join(str(p) for p in OVERLAYS)
bdir = env.west_workspace / "build_audit"
west = env.venv_scripts / "west.exe"
board = "xiao_esp32c6/esp32c6/hpcore"

cmd = [str(west), "build", "--build-dir", str(bdir), "-p", "always",
       "--sysbuild", "-b", board, str(fc.REPO_ROOT),
       "--", f"-DEXTRA_CONF_FILE={overlay_arg}"]
print(f"[build] build_audit (v0.7.9-dlv-audit, THREAD_ANALYZER+stackguard)\n"
      f"        overlays={overlay_arg}\n", flush=True)
r = subprocess.run(cmd, cwd=str(env.west_workspace), env=env.env_for_subprocess())
app = bdir / "ami-lwm2m-node" / "zephyr" / "zephyr.signed.bin"
mcub = bdir / "mcuboot" / "zephyr" / "zephyr.bin"
ok = (r.returncode == 0 and app.exists() and mcub.exists())
print(f"\n[build] build_audit: {'OK' if ok else 'FAIL'} "
      f"(app={app.exists()} mcuboot={mcub.exists()} rc={r.returncode})")
sys.exit(0 if ok else 1)
