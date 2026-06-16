#!/usr/bin/env python3
"""Rebuild the production minimal firmware (block 64) WITH Fix A baked in.

Fix A (BUG #2) lives in prj.conf (CONFIG_LWM2M_UPDATE_PERIOD=300), so a pristine
rebuild of the existing production build dirs picks it up automatically. Same
EXTRA_CONF_FILE each dir used before (read from their CMakeCache), so the only
change vs the deployed fleet firmware is the watchdog/liveness fixes + RID 37.

Builds both variants for the 16/16 SED/FTD split:
  build_minimal      = SED : overlays/sed_aggressive.conf + overlays/brn_fix_minimal.conf
  build_minimal_ftd  = FTD : overlays/ftd.conf           + overlays/brn_fix_minimal.conf
Both: MINIMAL AMI, mesh R1000, CoAP block 64.

Then flash with:  python tools/bulk_flash_minimal.py --alternate
"""
from __future__ import annotations
import subprocess, sys
import fleet_common as fc

fc.bootstrap_venv()
env = fc.detect_env(verbose=True)
west = env.venv_scripts / "west.exe"
board = "xiao_esp32c6/esp32c6/hpcore"

VARIANTS = {
    "build_minimal":     ["overlays/sed_aggressive.conf", "overlays/brn_fix_minimal.conf"],
    "build_minimal_ftd": ["overlays/ftd.conf",            "overlays/brn_fix_minimal.conf"],
}

rc_all = 0
for bdir_name, ovl in VARIANTS.items():
    overlay_arg = ";".join(str(fc.REPO_ROOT / o) for o in ovl)
    bdir = env.west_workspace / bdir_name
    cmd = [str(west), "build", "--build-dir", str(bdir), "-p", "always",
           "--sysbuild", "-b", board, str(fc.REPO_ROOT),
           "--", f"-DEXTRA_CONF_FILE={overlay_arg}"]
    print(f"\n[build] {bdir_name}  overlays={overlay_arg}", flush=True)
    r = subprocess.run(cmd, cwd=str(env.west_workspace), env=env.env_for_subprocess())
    binp = bdir / "ami-lwm2m-node" / "zephyr" / "zephyr.signed.bin"
    mcub = bdir / "mcuboot" / "zephyr" / "zephyr.bin"
    ok = (r.returncode == 0 and binp.exists() and mcub.exists())
    print(f"[build] {bdir_name}: {'OK' if ok else 'FAIL'} "
          f"(app={binp.exists()} mcuboot={mcub.exists()} rc={r.returncode})", flush=True)
    if not ok:
        rc_all = 1
sys.exit(rc_all)
