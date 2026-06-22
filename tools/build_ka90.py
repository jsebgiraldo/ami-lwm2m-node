#!/usr/bin/env python3
"""Build the A/B #1 TEST-ARM firmware -> build_ka90  (v0.7.8-ka90).

Identical to build_prod (canonical fat-production) PLUS overlays/keepalive90.conf
which drops CONFIG_AMI_COAP_KEEPALIVE_PERIOD_S 300 -> 90 to shorten the ~10-min
MESH session-drop outage to ~3 min. Reports fw_version 0.7.8-ka90 so the test arm
is trivially separable from the build_prod (v0.7.7-omr, 300s) control in TB.

Flash a SUBSET (include 2-3 of the flickering 7: Labs 7,10,17,21,22,29,30) via
flash_one.py / flash_erase_parallel.py; keep the rest on build_prod as control.
Metric (tools/flicker_baseline.py): outage duration test-arm vs control.
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
    fc.REPO_ROOT / "overlays" / "keepalive90.conf",   # LAST: keepalive override
]
overlay_arg = ";".join(str(p) for p in OVERLAYS)
bdir = env.west_workspace / "build_ka90"
west = env.venv_scripts / "west.exe"
board = "xiao_esp32c6/esp32c6/hpcore"

cmd = [str(west), "build", "--build-dir", str(bdir), "-p", "always",
       "--sysbuild", "-b", board, str(fc.REPO_ROOT),
       "--", f"-DEXTRA_CONF_FILE={overlay_arg}"]
print(f"[build] build_ka90 (v0.7.8-ka90, keepalive=90s)  overlays={overlay_arg}\n", flush=True)
r = subprocess.run(cmd, cwd=str(env.west_workspace), env=env.env_for_subprocess())
app = bdir / "ami-lwm2m-node" / "zephyr" / "zephyr.signed.bin"
mcub = bdir / "mcuboot" / "zephyr" / "zephyr.bin"
ok = (r.returncode == 0 and app.exists() and mcub.exists())
print(f"\n[build] build_ka90: {'OK' if ok else 'FAIL'} (app={app.exists()} mcuboot={mcub.exists()} rc={r.returncode})")
sys.exit(0 if ok else 1)
