#!/usr/bin/env python3
"""Archive the ELF (and its config) for a firmware version, so a later panic can
be resolved.

WHY: v0.7.18 exposes the faulting PC as Object 33000 RIDs 40/41 (mepc, ra). Those
are raw addresses — turning them into a source line needs addr2line against THE
EXACT ELF that was running. docs/PENDIENTES.md 2.2 makes archiving per-version
artefacts a hard requirement; without it the addresses are noise.

  python tools/archive_build.py --build-dir build_med_lab
  python tools/archive_build.py --list
  python tools/archive_build.py --resolve 0x42012345 0x42011abc --version 0.7.19-ami

Archive layout:  build_archive/<version>/{zephyr.elf,zephyr.bin,.config,build_info.txt}
"""
from __future__ import annotations

import argparse
import hashlib
import pathlib
import re
import shutil
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
ARCHIVE = REPO / "build_archive"
WS = pathlib.Path("C:/Users/jsgir/Documents/ESP32/zephyrproject")


def fw_version() -> str:
    m = re.search(r'#define\s+CLIENT_FIRMWARE_VER\s+"([^"]+)"',
                  (REPO / "src" / "main.c").read_text(encoding="utf-8", errors="replace"))
    return m.group(1) if m else "unknown"


def sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def find_toolchain_addr2line() -> str | None:
    for sdk in sorted(pathlib.Path.home().glob("zephyr-sdk-*"), reverse=True):
        for c in sdk.glob("riscv64-zephyr-elf/bin/riscv64-zephyr-elf-addr2line*"):
            return str(c)
    return None


def do_archive(build_dir: str) -> int:
    bd = WS / build_dir
    elf = bd / "zephyr" / "zephyr.elf"
    if not elf.exists():
        print(f"no ELF at {elf}")
        return 1
    ver = fw_version()
    dst = ARCHIVE / ver
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("zephyr.elf", "zephyr.bin", ".config"):
        src = bd / "zephyr" / name
        if src.exists():
            shutil.copy2(src, dst / name)
    info = (f"version    : {ver}\n"
            f"archived   : {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"build_dir  : {bd}\n"
            f"elf sha256 : {sha(elf)}\n"
            f"bin sha256 : {sha(bd / 'zephyr' / 'zephyr.bin')}\n")
    (dst / "build_info.txt").write_text(info, encoding="utf-8")
    print(info)
    print(f"archived -> {dst}")
    return 0


def do_list() -> int:
    if not ARCHIVE.exists():
        print("no archive yet")
        return 0
    for d in sorted(ARCHIVE.iterdir()):
        info = d / "build_info.txt"
        print(f"  {d.name:16} {info.read_text(encoding='utf-8').splitlines()[3] if info.exists() else ''}")
    return 0


def do_resolve(addrs: list[str], version: str | None) -> int:
    ver = version or fw_version()
    elf = ARCHIVE / ver / "zephyr.elf"
    if not elf.exists():
        print(f"no archived ELF for {ver} — run: python tools/archive_build.py --build-dir <dir>")
        print("Resolving against a DIFFERENT build's ELF produces plausible but WRONG lines.")
        return 1
    a2l = find_toolchain_addr2line()
    if not a2l:
        print("addr2line not found under ~/zephyr-sdk-*")
        return 1
    cmd = [a2l, "-f", "-p", "-C", "-e", str(elf), *addrs]
    print(f"# ELF: {elf}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr.strip())
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-dir", help="west build dir under the zephyr workspace")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--resolve", nargs="+", metavar="ADDR",
                    help="addresses (mepc/ra from RIDs 40/41) to resolve")
    ap.add_argument("--version", help="archived version to resolve against")
    args = ap.parse_args()

    if args.list:
        return do_list()
    if args.resolve:
        return do_resolve(args.resolve, args.version)
    if args.build_dir:
        return do_archive(args.build_dir)
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
