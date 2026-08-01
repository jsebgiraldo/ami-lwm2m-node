#!/usr/bin/env python3
"""Build a 40 MHz-flash variant of the fat-production firmware -> build_40m.

Same config as build_prod but with CONFIG_ESPTOOLPY_FLASHFREQ_40M applied to
BOTH the app and the mcuboot image, to test/fix the 80 MHz boot-loop on
marginal flash chips (MCUboot hangs reading flash at 80 MHz -> watchdog loop).
Kept in a separate build dir so build_prod (the 80 MHz fleet build) is untouched.
"""
from __future__ import annotations
import subprocess, sys
import fleet_common as fc

fc.bootstrap_venv()
env = fc.detect_env(verbose=True)

FLASH40M = fc.REPO_ROOT / "overlays" / "flash40m.conf"
OVERLAYS = [
    fc.REPO_ROOT / "overlays" / "ftd.conf",
    fc.REPO_ROOT / "overlays" / "resprobe_lwm2m.conf",
    fc.REPO_ROOT / "overlays" / "prod_fat.conf",
    FLASH40M,
]
overlay_arg = ";".join(str(p) for p in OVERLAYS)
bdir = env.west_workspace / "build_40m"
west = env.venv_scripts / "west.exe"
board = "xiao_esp32c6/esp32c6/hpcore"

cmd = [str(west), "build", "--build-dir", str(bdir), "-p", "always",
       "--sysbuild", "-b", board, str(fc.REPO_ROOT),
       "--", f"-DEXTRA_CONF_FILE={overlay_arg}",
       f"-Dmcuboot_EXTRA_CONF_FILE={FLASH40M}"]
print(f"[build] build_40m (40 MHz flash test)  app+mcuboot overlay={FLASH40M}\n", flush=True)
r = subprocess.run(cmd, cwd=str(env.west_workspace), env=env.env_for_subprocess())
app = bdir / "ami-lwm2m-node" / "zephyr" / "zephyr.signed.bin"
mcub = bdir / "mcuboot" / "zephyr" / "zephyr.bin"
ok = (r.returncode == 0 and app.exists() and mcub.exists())
print(f"\n[build] build_40m: {'OK' if ok else 'FAIL'} (app={app.exists()} mcuboot={mcub.exists()} rc={r.returncode})")
sys.exit(0 if ok else 1)
