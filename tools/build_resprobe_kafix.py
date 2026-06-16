#!/usr/bin/env python3
"""Rebuild the `resprobe` variant (FTD + resource_probe + resprobe_lwm2m, NO mesh
overlay → default TLV creds) with the v0.7.3-kafix keepalive fix.

This mirrors the exact EXTRA_CONF_FILE that produced the firmware currently on
board 1494 (read from build_resprobe/CMakeCache.txt), so the ONLY change between
the looping firmware and this artifact is the one-line keepalive logic fix.
Artifact: <west_ws>/build_resprobe/zephyr/zephyr.bin
"""
from __future__ import annotations
import subprocess
import sys
import fleet_common as fc

fc.bootstrap_venv()
env = fc.detect_env(verbose=True)

OVERLAYS = [
    fc.REPO_ROOT / "overlays" / "ftd.conf",
    fc.REPO_ROOT / "overlays" / "resource_probe.conf",
    fc.REPO_ROOT / "overlays" / "resprobe_lwm2m.conf",
]
overlay_arg = ";".join(str(p) for p in OVERLAYS)
bdir = env.west_workspace / "build_resprobe"
west = env.venv_scripts / "west.exe"
board = "xiao_esp32c6/esp32c6/hpcore"

cmd = [str(west), "build", "--build-dir", str(bdir), "-p", "always",
       "--sysbuild", "-b", board,
       str(fc.REPO_ROOT), "--", f"-DEXTRA_CONF_FILE={overlay_arg}"]
print(f"[build] resprobe+kafix -> {bdir.name}")
print(f"[build] overlays={overlay_arg}")
print(f"[build] {' '.join(cmd)}\n", flush=True)
res = subprocess.run(cmd, cwd=str(env.west_workspace), env=env.env_for_subprocess())
if res.returncode != 0:
    print(f"\n[build] FAILED rc={res.returncode}")
    sys.exit(res.returncode)
binp = bdir / "zephyr" / "zephyr.bin"
print(f"\n[build] OK  {binp}  exists={binp.exists()}  "
      f"size={binp.stat().st_size//1024 if binp.exists() else '-'} KiB")
