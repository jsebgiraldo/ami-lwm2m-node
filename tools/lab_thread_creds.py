#!/usr/bin/env python3
"""lab_thread_creds.py — pull the LAB OTBR's active Thread dataset and emit the
exact code/config the ESP32-C6 AMI firmware needs to join THIS bench network.

WHY THIS EXISTS
---------------
The AMI node firmware does NOT read Thread credentials from NVS or from a
commissioner. It compiles a *hardcoded* operational-dataset TLV blob straight
into the image — see src/main.c :: apply_otbr_dataset(), the `otbr_tlvs[]`
arrays guarded by CONFIG_AMI_MESH_PI4 / CONFIG_AMI_MESH_R1000. To put a node on
a *new* network (our local benchtop OTBR) you must regenerate that byte array
from the OTBR's live dataset and rebuild.

This tool automates exactly that: it runs `ot-ctl dataset active -x` on the LAB
OTBR (a docker container in WSL2 by default — configurable), parses the TLVs so
you can eyeball channel / panid / key, and prints THREE paste-ready artifacts:

  1. the C `otbr_tlvs[]` block for a new `#elif defined(CONFIG_AMI_MESH_LAB)`
     branch in src/main.c :: apply_otbr_dataset()   (or to overwrite an existing
     branch, e.g. the r1000 slot, for a quick bench build);
  2. the cosmetic Kconfig-value overlay lines (matches overlays/pi4.conf /
     overlays/r1000.conf) — channel / panid / name / xpanid / networkkey;
  3. a ready-to-write overlays/lab.conf (use --write-overlay).

It is a lab sibling of tools/get_otbr_dataset.py, but STDLIB-ONLY (no paramiko —
the lab OTBR is reached by `docker exec`, not SSH) and non-interactive.

DATASET SOURCE (configurable — default is docker exec)
------------------------------------------------------
  # default: docker exec otbr ot-ctl dataset active -x   (run from inside WSL)
  python3 tools/lab_thread_creds.py

  # from Windows PowerShell, tunnel the same command through WSL:
  python tools\\lab_thread_creds.py --exec "wsl -d Ubuntu-24.04 docker exec otbr ot-ctl"

  # different container name:
  python3 tools/lab_thread_creds.py --container my-otbr

  # paste a hex blob you already captured (offline / no docker on this host):
  python3 tools/lab_thread_creds.py --hex 0e08000000000001000...19

  # read the hex from a file:
  python3 tools/lab_thread_creds.py --from-file dataset.hex

  # also drop overlays/lab.conf next to pi4.conf / r1000.conf:
  python3 tools/lab_thread_creds.py --write-overlay

Robust to a dead source: on any failure it logs to stderr and exits non-zero
without a traceback, so it is safe to call from a bring-up script.
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys

# TLV type registry (Thread operational dataset) — mirrors tools/get_otbr_dataset.py.
TLV_NAMES = {
    0x00: "Channel",
    0x01: "PAN ID",
    0x02: "Extended PAN ID",
    0x03: "Network Name",
    0x04: "PSKc",
    0x05: "Network Key",
    0x07: "Mesh-Local Prefix",
    0x0C: "Security Policy",
    0x0E: "Active Timestamp",
    0x35: "Channel Mask",
}

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def log(msg: str) -> None:
    print(f"[lab-creds] {msg}", file=sys.stderr)


# ── dataset acquisition ──────────────────────────────────────────────────────
def fetch_hex_via_exec(exec_prefix: str) -> str:
    """Run '<exec_prefix> dataset active -x' and return the concatenated hex.

    exec_prefix is a shell-ish command such as 'docker exec otbr ot-ctl' or
    'wsl -d Ubuntu-24.04 docker exec otbr ot-ctl'. We append the ot-ctl args.
    """
    cmd = shlex.split(exec_prefix) + ["dataset", "active", "-x"]
    log("running: " + " ".join(shlex.quote(c) for c in cmd))
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=20
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            f"command not found ({e.filename!r}). On Windows use "
            f"--exec \"wsl -d Ubuntu-24.04 docker exec otbr ot-ctl\"."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("ot-ctl timed out — is the OTBR container up? "
                           "(docker ps) and is Thread started? (ot-ctl state)")
    if out.returncode != 0:
        raise RuntimeError(
            f"exit {out.returncode}: {out.stderr.strip() or out.stdout.strip()}"
        )
    return clean_hex(out.stdout)


def clean_hex(raw: str) -> str:
    """Strip 'Done', whitespace, newlines from an ot-ctl hex dump."""
    parts = [
        ln.strip()
        for ln in raw.replace("\r", "\n").split("\n")
        if ln.strip() and ln.strip().lower() != "done"
    ]
    h = "".join(parts).replace(" ", "").replace(":", "")
    return h.lower()


# ── TLV parsing ──────────────────────────────────────────────────────────────
def parse_tlvs(data: bytes) -> dict:
    """Walk the TLV blob → dict of decoded, human-facing fields."""
    fields: dict = {}
    i = 0
    n = len(data)
    while i + 2 <= n:
        t = data[i]
        ln = data[i + 1]
        val = data[i + 2 : i + 2 + ln]
        if len(val) != ln:
            log(f"WARN truncated TLV type 0x{t:02x} (need {ln}, got {len(val)})")
            break
        if t == 0x00 and ln >= 3:  # Channel: page(1) + channel(2, big-endian)
            fields["channel"] = (val[1] << 8) | val[2]
        elif t == 0x01 and ln == 2:  # PAN ID
            fields["panid"] = (val[0] << 8) | val[1]
        elif t == 0x02 and ln == 8:  # Extended PAN ID
            fields["xpanid"] = ":".join(f"{b:02x}" for b in val)
        elif t == 0x03:  # Network Name
            fields["name"] = val.decode("utf-8", errors="replace")
        elif t == 0x05 and ln == 16:  # Network Key
            fields["networkkey"] = ":".join(f"{b:02x}" for b in val)
        elif t == 0x07 and ln == 8:  # Mesh-Local Prefix
            fields["meshlocal"] = (
                ":".join(f"{val[j]:02x}{val[j+1]:02x}" for j in range(0, 8, 2))
                + "::/64"
            )
        i += 2 + ln
    return fields


# ── emitters ─────────────────────────────────────────────────────────────────
def emit_c_block(data: bytes, fields: dict) -> str:
    """Emit the src/main.c apply_otbr_dataset() otbr_tlvs[] block (10/line)."""
    lines = []
    lines.append("#elif defined(CONFIG_AMI_MESH_LAB)")
    lines.append("\tstatic const uint8_t otbr_tlvs[] = {")
    lines.append("\t\t/* LAB benchtop OTBR (SONOFF ZBDongle-E RCP, WSL2 docker) - "
                 f"Ch{fields.get('channel','?')}, "
                 f"PAN 0x{fields.get('panid',0):04x}")
    if "meshlocal" in fields:
        lines.append(f"\t\t * Mesh-local: {fields['meshlocal']}")
    lines.append("\t\t * Exported via: docker exec otbr ot-ctl dataset active -x")
    lines.append("\t\t * (regenerate with tools/lab_thread_creds.py). */")
    for j in range(0, len(data), 10):
        chunk = data[j : j + 10]
        hexb = ", ".join(f"0x{b:02x}" for b in chunk)
        lines.append(f"\t\t{hexb},")
    lines.append("\t};")
    name = fields.get("name", "LAB")
    lines.append(f'\tconst char *mesh_label = "LAB bench ({name}, '
                 f'Ch{fields.get("channel","?")})";')
    ml = fields.get("meshlocal", "unknown")
    lines.append(f'\tconst char *mesh_local_str = "{ml}";')
    return "\n".join(lines)


def emit_overlay(fields: dict) -> str:
    """Emit overlays/lab.conf (cosmetic Kconfig, mirrors pi4.conf/r1000.conf)."""
    ch = fields.get("channel", 25)
    pan = fields.get("panid", 0)
    name = fields.get("name", "LAB")
    xpan = fields.get("xpanid", "")
    key = fields.get("networkkey", "")
    return "\n".join([
        "# Mesh target: LAB benchtop OTBR on THIS Windows PC.",
        "#   RCP  = SONOFF ZBDongle-E (EFR32MG21) running ot-rcp",
        "#   OTBR = openthread/otbr docker container inside WSL2",
        "#",
        "# Generated by tools/lab_thread_creds.py from the OTBR active dataset.",
        "# The REAL dataset is the TLV blob in src/main.c (apply_otbr_dataset,",
        "# CONFIG_AMI_MESH_LAB branch); the values below are cosmetic - they only",
        "# feed the startup banner \"Network: Thread Ch%d PAN 0x%04X\".",
        "#",
        "# Build:",
        "#   python tools/build_firmware.py --variant med --mesh lab",
        "# or:",
        '#   west build ... -- -DEXTRA_CONF_FILE="overlays/med.conf;overlays/lab.conf"',
        "",
        "CONFIG_AMI_MESH_LAB=y",
        "",
        "# Cosmetic Kconfig values (must match the compiled-in TLV blob).",
        f"CONFIG_OPENTHREAD_CHANNEL={ch}",
        f"CONFIG_OPENTHREAD_PANID={pan}",
        f'CONFIG_OPENTHREAD_NETWORK_NAME="{name}"',
        f'CONFIG_OPENTHREAD_XPANID="{xpan}"',
        f'CONFIG_OPENTHREAD_NETWORKKEY="{key}"',
        "",
    ])


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Pull the LAB OTBR active dataset and emit the firmware "
                    "code/config to join it (C otbr_tlvs[] + overlays/lab.conf).")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--hex", metavar="HEX",
                     help="use this dataset hex directly (skip docker/ot-ctl)")
    src.add_argument("--from-file", metavar="PATH",
                     help="read the dataset hex from a file")
    ap.add_argument("--exec", dest="exec_prefix", default=None,
                    help="command that runs ot-ctl on the OTBR "
                         "(default: 'docker exec <container> ot-ctl'). "
                         "Windows example: "
                         "\"wsl -d Ubuntu-24.04 docker exec otbr ot-ctl\"")
    ap.add_argument("--container", default="otbr",
                    help="OTBR docker container name (default: otbr); "
                         "ignored if --exec is given")
    ap.add_argument("--write-overlay", action="store_true",
                    help="write overlays/lab.conf (does NOT touch src/main.c)")
    args = ap.parse_args()

    # 1) acquire the hex
    try:
        if args.hex:
            hexstr = clean_hex(args.hex)
        elif args.from_file:
            with open(args.from_file, "r", encoding="utf-8") as f:
                hexstr = clean_hex(f.read())
        else:
            exec_prefix = args.exec_prefix or f"docker exec {args.container} ot-ctl"
            hexstr = fetch_hex_via_exec(exec_prefix)
    except Exception as e:  # dead source → log + non-zero, no traceback
        log(f"could not obtain dataset: {e}")
        return 2

    if not hexstr or len(hexstr) % 2 != 0:
        log(f"invalid hex ({len(hexstr)} chars) — got: {hexstr[:60]!r}...")
        return 2
    try:
        data = bytes.fromhex(hexstr)
    except ValueError as e:
        log(f"not valid hex: {e}")
        return 2

    fields = parse_tlvs(data)

    # 2) human summary → stderr (keeps stdout paste-clean)
    log(f"dataset = {len(data)} bytes")
    log(f"  channel     : {fields.get('channel')}")
    log(f"  panid       : 0x{fields.get('panid', 0):04x}")
    log(f"  name        : {fields.get('name')}")
    log(f"  xpanid      : {fields.get('xpanid')}")
    log(f"  mesh-local  : {fields.get('meshlocal')}")
    log(f"  networkkey  : {fields.get('networkkey')}")

    # 3) emit artifacts → stdout
    print("=" * 72)
    print("STEP A -- paste into src/main.c :: apply_otbr_dataset()")
    print("         (add this branch to the #if/#elif chain, above the #else)")
    print("=" * 72)
    print(emit_c_block(data, fields))
    print()
    print("=" * 72)
    print("STEP B -- overlays/lab.conf (cosmetic Kconfig)")
    print("=" * 72)
    overlay = emit_overlay(fields)
    print(overlay)

    print("=" * 72)
    print("STEP C -- add to the Kconfig `choice AMI_MESH` (Kconfig, ~line 571):")
    print("=" * 72)
    print("config AMI_MESH_LAB")
    print('\tbool "LAB benchtop OTBR (WSL2 docker + ZBDongle-E RCP)"')
    print("\thelp")
    print("\t  Local benchtop test network. Dataset regenerated by")
    print("\t  tools/lab_thread_creds.py. NOT for production nodes.")

    if args.write_overlay:
        dst = os.path.join(REPO_ROOT, "overlays", "lab.conf")
        try:
            with open(dst, "w", encoding="utf-8", newline="\n") as f:
                f.write(overlay)
            log(f"wrote {dst}")
        except OSError as e:
            log(f"could not write overlay: {e}")
            return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
