#!/usr/bin/env python3
"""Build the CANONICAL fat-production fleet firmware -> build_prod.

Config (v0.7.5-prod): FTD + Object 33000 (RID 37) + CoAP block 64 + mesh R1000
+ Fix A (CONFIG_LWM2M_UPDATE_PERIOD=300, from prj.conf) + heap stats for RID 36.
= the validated 3-board config minus the serial-only debug instrumentation.

Overlays: ftd.conf + resprobe_lwm2m.conf + prod_fat.conf
Artifact: build_prod/ami-lwm2m-node/zephyr/zephyr.signed.bin  (+ mcuboot)

Flash the fleet (all FTD) with:
    python tools/bulk_flash_minimal.py --sed-build build_prod --ftd-build build_prod
(both slots = build_prod -> every connected board gets the same fat build.)
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
]
overlay_arg = ";".join(str(p) for p in OVERLAYS)
bdir = env.west_workspace / "build_prod"
west = env.venv_scripts / "west.exe"
board = "xiao_esp32c6/esp32c6/hpcore"

cmd = [str(west), "build", "--build-dir", str(bdir), "-p", "always",
       "--sysbuild", "-b", board, str(fc.REPO_ROOT),
       "--", f"-DEXTRA_CONF_FILE={overlay_arg}"]
print(f"[build] build_prod (fat-production)  overlays={overlay_arg}\n", flush=True)
r = subprocess.run(cmd, cwd=str(env.west_workspace), env=env.env_for_subprocess())
app = bdir / "ami-lwm2m-node" / "zephyr" / "zephyr.signed.bin"
mcub = bdir / "mcuboot" / "zephyr" / "zephyr.bin"
ok = (r.returncode == 0 and app.exists() and mcub.exists())
print(f"\n[build] build_prod: {'OK' if ok else 'FAIL'} (app={app.exists()} mcuboot={mcub.exists()} rc={r.returncode})")
sys.exit(0 if ok else 1)
