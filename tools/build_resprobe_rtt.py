#!/usr/bin/env python3
"""Build resprobe+kafix+RTT into build_resprobe_rtt (sysbuild, signed app).

Same instrumented resprobe build as build_resprobe, PLUS overlays/rtt.conf so
the firmware's LOG stream goes to a SEGGER-RTT RAM buffer readable live over
OpenOCD JTAG without halting. Lets us watch every tick and catch the exact
reboot path on the serial-less SuperMini.
Artifact: build_resprobe_rtt/ami-lwm2m-node/zephyr/zephyr.signed.bin
"""
from __future__ import annotations
import subprocess, sys
import fleet_common as fc

fc.bootstrap_venv()
env = fc.detect_env(verbose=True)

OVERLAYS = [
    fc.REPO_ROOT / "overlays" / "ftd.conf",
    fc.REPO_ROOT / "overlays" / "resource_probe.conf",
    fc.REPO_ROOT / "overlays" / "resprobe_lwm2m.conf",
    fc.REPO_ROOT / "overlays" / "rtt.conf",
]
overlay_arg = ";".join(str(p) for p in OVERLAYS)
bdir = env.west_workspace / "build_resprobe_rtt"
west = env.venv_scripts / "west.exe"
board = "xiao_esp32c6/esp32c6/hpcore"

cmd = [str(west), "build", "--build-dir", str(bdir), "-p", "always",
       "--sysbuild", "-b", board,
       str(fc.REPO_ROOT), "--", f"-DEXTRA_CONF_FILE={overlay_arg}"]
print(f"[build] resprobe+kafix+RTT -> {bdir.name}")
print(f"[build] overlays={overlay_arg}\n", flush=True)
res = subprocess.run(cmd, cwd=str(env.west_workspace), env=env.env_for_subprocess())
if res.returncode != 0:
    print(f"\n[build] FAILED rc={res.returncode}")
    sys.exit(res.returncode)
binp = bdir / "ami-lwm2m-node" / "zephyr" / "zephyr.signed.bin"
print(f"\n[build] OK  signed={binp}  exists={binp.exists()}")
