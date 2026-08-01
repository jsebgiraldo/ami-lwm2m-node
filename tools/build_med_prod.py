#!/usr/bin/env python3
"""Build the MED (Minimal End Device) fat-production fleet firmware -> build_med_prod.

Identical to build_prod.py (sysbuild + resprobe_lwm2m + prod_fat, MCUboot, same
board and baked-in mesh dataset) but swaps the FTD role overlay for MED
(overlays/med.conf: CONFIG_OPENTHREAD_MTD=y / FTD=n). MED nodes attach as
non-routing children -> keeps the Thread partition under the ~32-router / ~60-node
practical ceiling so the fleet stays stable at scale. Keep ~5-10% of nodes on the
FTD build_prod firmware to act as routers.

Artifact: build_med_prod/ami-lwm2m-node/zephyr/zephyr.signed.bin (+ mcuboot)
Flash with:
    python tools/flash_fleet_seq.py --coms COMxx,... --build-dir build_med_prod
"""
from __future__ import annotations
import subprocess, sys
import fleet_common as fc

fc.bootstrap_venv()
env = fc.detect_env(verbose=True)

OVERLAYS = [
    fc.REPO_ROOT / "overlays" / "med.conf",
    fc.REPO_ROOT / "overlays" / "resprobe_lwm2m.conf",
    fc.REPO_ROOT / "overlays" / "prod_fat.conf",
]
overlay_arg = ";".join(str(p) for p in OVERLAYS)
bdir = env.west_workspace / "build_med_prod"
west = env.venv_scripts / "west.exe"
board = "xiao_esp32c6/esp32c6/hpcore"

cmd = [str(west), "build", "--build-dir", str(bdir), "-p", "always",
       "--sysbuild", "-b", board, str(fc.REPO_ROOT),
       "--", f"-DEXTRA_CONF_FILE={overlay_arg}"]
print(f"[build] build_med_prod (MED fat-production)  overlays={overlay_arg}\n", flush=True)
r = subprocess.run(cmd, cwd=str(env.west_workspace), env=env.env_for_subprocess())
app = bdir / "ami-lwm2m-node" / "zephyr" / "zephyr.signed.bin"
mcub = bdir / "mcuboot" / "zephyr" / "zephyr.bin"
ok = (r.returncode == 0 and app.exists() and mcub.exists())
print(f"\n[build] build_med_prod: {'OK' if ok else 'FAIL'} (app={app.exists()} mcuboot={mcub.exists()} rc={r.returncode})")
sys.exit(0 if ok else 1)
