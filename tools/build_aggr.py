#!/usr/bin/env python3
"""Build the AGGRESSIVE TEST firmware -> build_aggr  (v0.7.6-aggr).

Same fat-production base as build_prod PLUS overlays/aggr.conf (faster liveness/
recovery, USB-drain cut, pmin 15s, CoAP block 1024). aggr.conf is listed LAST so
its block-size override (1024) wins over resprobe_lwm2m.conf (64).

Priority of this config: node never STUCK; fewer reboots = bonus. TEST on a
subset before fleet-wide.

Flash a subset:
    python tools/flash_aggr.py 10:51:DB:.. 10:51:DB:..
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
    fc.REPO_ROOT / "overlays" / "aggr.conf",          # LAST: block-size override wins
]
overlay_arg = ";".join(str(p) for p in OVERLAYS)
bdir = env.west_workspace / "build_aggr"
west = env.venv_scripts / "west.exe"
board = "xiao_esp32c6/esp32c6/hpcore"

cmd = [str(west), "build", "--build-dir", str(bdir), "-p", "always",
       "--sysbuild", "-b", board, str(fc.REPO_ROOT),
       "--", f"-DEXTRA_CONF_FILE={overlay_arg}"]
print(f"[build] build_aggr (v0.7.6-aggr)  overlays={overlay_arg}\n", flush=True)
r = subprocess.run(cmd, cwd=str(env.west_workspace), env=env.env_for_subprocess())
app = bdir / "ami-lwm2m-node" / "zephyr" / "zephyr.signed.bin"
mcub = bdir / "mcuboot" / "zephyr" / "zephyr.bin"
ok = (r.returncode == 0 and app.exists() and mcub.exists())
print(f"\n[build] build_aggr: {'OK' if ok else 'FAIL'} (app={app.exists()} mcuboot={mcub.exists()} rc={r.returncode})")
sys.exit(0 if ok else 1)
