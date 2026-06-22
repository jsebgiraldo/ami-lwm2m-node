#!/usr/bin/env python3
"""SSH into the Pi4 OTBR and correlate mesh-level state with the stuck boards.

Answers the open question from the 2026-06-19 audit: are the app-layer-silent
boards (registered, REG_UPDATE OK, but ZERO telemetry delivered) victims of
OTBR INBOUND address-resolution collapse, or board-side stuck sessions?

Dumps the authoritative mesh tables from ot-ctl and highlights:
  * presence/role/RSSI/timeout of each STUCK board in router/child/neighbor table
  * eidcache size + how many entries are cached 'retry/0' (the resolution-
    collapse signature — Edge can't deliver inbound to those EIDs)
  * SRP server has the TB Edge service advertised (else boards can't find Edge)
  * active dataset (confirms channel/netkey for the sniffer)

Usage:
    python tools/otbr_correlate.py --host 192.168.8.XYZ --user pi --password raspberry
    python tools/otbr_correlate.py --host root@192.168.8.111         # key auth
"""
from __future__ import annotations
import argparse
import sys

import fleet_common as fc

fc.bootstrap_venv()
import paramiko  # noqa: E402  (from the Zephyr venv)

# Stuck boards from the 2026-06-19 fleet_audit (label -> base MAC).
STUCK = {
    "Lab7":  "10:51:db:1c:14:98",
    "Lab10": "10:51:db:1b:f8:4c",
    "Lab17": "10:51:db:1c:15:58",   # recovered to ka90 — expect HEALTHY now
    "Lab21": "10:51:db:1b:f7:88",
    "Lab30": "58:e6:c5:18:c6:0c",
}

OT = "ot-ctl"
CMDS = [
    "state", "version",
    "channel", "panid", "extpanid", "networkkey",
    "router table", "child table", "neighbor table",
    "eidcache",
    "counters mac",
    "srp server state", "srp server host", "srp server service",
    "dataset active",
]


def eui64_from_eui48(mac48: str) -> str:
    """Thread ext address = EUI-64: insert ff:fe, flip U/L bit of first octet."""
    b = [int(x, 16) for x in mac48.split(":")]
    b[0] ^= 0x02
    e = b[:3] + [0xFF, 0xFE] + b[3:]
    return "".join(f"{x:02x}" for x in e)


def run(cli: paramiko.SSHClient, cmd: str) -> str:
    _in, out, err = cli.exec_command(cmd, timeout=20)
    o = out.read().decode("utf-8", "replace")
    e = err.read().decode("utf-8", "replace")
    return (o + ("\n[stderr] " + e if e.strip() else "")).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="host or user@host")
    ap.add_argument("--user", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--port", type=int, default=22)
    ap.add_argument("--sudo", action="store_true",
                    help="prefix ot-ctl with sudo")
    args = ap.parse_args()

    user = args.user
    host = args.host
    if "@" in host and not user:
        user, host = host.split("@", 1)

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"[otbr] ssh {user}@{host}:{args.port}")
    cli.connect(host, port=args.port, username=user, password=args.password,
                timeout=15, allow_agent=True, look_for_keys=True)

    prefix = "sudo " if args.sudo else ""
    raw = {}
    for c in CMDS:
        raw[c] = run(cli, f"{prefix}{OT} {c}")

    # ---- dump everything ----
    for c in CMDS:
        print(f"\n===== {OT} {c} =====")
        print(raw[c])

    # ---- correlate stuck boards ----
    print("\n" + "=" * 64)
    print("STUCK-BOARD CORRELATION")
    print("=" * 64)
    tables = (raw.get("child table", "") + "\n" + raw.get("router table", "")
              + "\n" + raw.get("neighbor table", "")).lower()
    cache = raw.get("eidcache", "")
    for lab, mac in STUCK.items():
        eui = eui64_from_eui48(mac)
        present = eui in tables
        # find any cache line referencing this board's last 4 hex of EUI
        tail = eui[-4:]
        cache_hits = [ln for ln in cache.splitlines() if tail in ln.lower()]
        print(f"  {lab:6} mac={mac}  eui64={eui}")
        print(f"         in router/child/neighbor table: {'YES' if present else 'NO (not a neighbor)'}")
        if cache_hits:
            for h in cache_hits:
                print(f"         eidcache: {h.strip()}")

    # ---- eidcache health ----
    lines = [l for l in cache.splitlines() if l.strip() and "cache" not in l.lower()]
    retry0 = sum(1 for l in lines if "retry" in l.lower() or "/0" in l)
    print("\n" + "=" * 64)
    print(f"EIDCACHE HEALTH: {len(lines)} entries; ~{retry0} look stale/retry "
          f"(collapse signature if this is large, e.g. >150)")
    edge_ok = "edge" in raw.get("srp server service", "").lower() or \
              "_meshcop" in raw.get("srp server service", "").lower() or \
              raw.get("srp server service", "").strip() not in ("", "Done")
    print(f"SRP server: state='{raw.get('srp server state','').strip()}'  "
          f"services_advertised={'YES' if edge_ok else 'NONE — boards cannot find TB Edge!'}")
    cli.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
