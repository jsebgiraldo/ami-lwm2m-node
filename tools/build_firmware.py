#!/usr/bin/env python3
"""Build the AMI LwM2M Node firmware exactly once per (variant, mesh) combination.

Variants (Thread role — overlays/<variant>.conf):
  med  Minimal End Device (default fleet role) — MTD compile-time, smaller binary,
       cannot be promoted to Router. ~580-620 KB.
  ftd  Full Thread Device (router-eligible) — for the few nodes you want to act
       as routers in the mesh. ~700 KB.

Mesh targets (which OTBR / Thread network — overlays/<mesh>.conf):
  r1000  UNAL-R1000 on Seeed R1000 (192.168.1.175, channel 21) — production default.
  pi4    UNAL-Thread on Pi4 EKH01 (192.168.1.111, channel 25) — legacy.

Each combination produces its own artifact at
`build_<variant>_<mesh>/zephyr/zephyr.bin` (e.g. build_med_r1000, build_ftd_pi4).
Subsequent flashes reuse the binary without recompiling.

Usage:
    python tools/build_firmware.py                              # med + r1000 (defaults)
    python tools/build_firmware.py --variant ftd                # ftd + r1000
    python tools/build_firmware.py --mesh pi4                   # med + pi4 (legacy)
    python tools/build_firmware.py --variant all --mesh r1000   # both variants for R1000
    python tools/build_firmware.py --variant all --mesh all     # all 4 combinations
    python tools/build_firmware.py --pristine                   # force clean rebuild
"""
from __future__ import annotations

import argparse
import subprocess
import sys

import fleet_common as fc

fc.bootstrap_venv()


def build_one(env: fc.ToolEnv, variant: str, mesh: str, pristine: bool, board: str) -> int:
    variant_overlay = fc.overlay_path(variant)
    mesh_overlay = fc.mesh_overlay_path(mesh)

    for o in (variant_overlay, mesh_overlay):
        if not o.exists():
            print(f"[build] missing overlay: {o}")
            return 2

    bdir = fc.build_dir(env, variant, mesh)
    west = env.venv_scripts / "west.exe"
    overlay_arg = fc.composed_overlay_arg(variant, mesh)

    cmd = [str(west), "build", "--build-dir", str(bdir)]
    if pristine:
        cmd += ["-p", "always"]
    cmd += ["-b", board, str(fc.REPO_ROOT),
            "--", f"-DEXTRA_CONF_FILE={overlay_arg}"]

    print(f"\n[build] variant={variant}  mesh={mesh}  build_dir={bdir.name}")
    print(f"[build] overlays={overlay_arg}")
    print(f"[build] {' '.join(cmd)}\n")
    res = subprocess.run(cmd, cwd=str(env.west_workspace), env=env.env_for_subprocess())
    if res.returncode != 0:
        print(f"\n[build] variant={variant} mesh={mesh} FAILED")
        return res.returncode

    bin_path = fc.firmware_bin(env, variant, mesh)
    if not bin_path.exists():
        print(f"[build] missing artifact: {bin_path}")
        return 2

    sha = fc.firmware_hash(env, variant, mesh)
    size_kb = bin_path.stat().st_size // 1024
    print(f"[build] variant={variant} mesh={mesh} OK   {bin_path.name}  size={size_kb} KiB  sha={sha}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--variant", default=fc.DEFAULT_VARIANT,
                    choices=[*fc.VARIANTS, "all"],
                    help=f"Thread role variant. Default: {fc.DEFAULT_VARIANT}")
    ap.add_argument("--mesh", default=fc.DEFAULT_MESH,
                    choices=[*fc.MESH_TARGETS, "all"],
                    help=f"Target Thread mesh. Default: {fc.DEFAULT_MESH}")
    ap.add_argument("--board", default=fc.DEFAULT_BOARD)
    ap.add_argument("--pristine", action="store_true", help="Force pristine (-p always)")
    args = ap.parse_args()

    env = fc.detect_env(verbose=True)
    variants = list(fc.VARIANTS) if args.variant == "all" else [args.variant]
    meshes = list(fc.MESH_TARGETS) if args.mesh == "all" else [args.mesh]

    failures = []
    for m in meshes:
        for v in variants:
            rc = build_one(env, v, m, args.pristine, args.board)
            if rc != 0:
                failures.append((v, m, rc))
    if failures:
        print("\n[build] Failed combinations:")
        for v, m, rc in failures:
            print(f"  - variant={v} mesh={m} rc={rc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
