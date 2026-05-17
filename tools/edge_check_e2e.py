"""Read-only E2E health check via SSH against the R1000 edge.

Confirms that:
  1. The edge alias workaround for the old hardcoded mleid (bf5a:2554)
     is still active (defensive — should NOT be needed by v0.6.0 nodes)
  2. The OTBR's current mleid + OMR
  3. ot-ctl history rx shows traffic from our bench node going to the
     CURRENT OMR (not the old mleid)
  4. TB Edge Leshan log shows New registration for our endpoint
  5. SRP server is publishing _lwm2m._udp service correctly

Read-only. Does NOT mutate edge state.
"""
from __future__ import annotations
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fleet_common as fc
fc.bootstrap_venv()

import paramiko  # noqa: E402

EDGE_HOST = "192.168.8.176"
EDGE_USER = "root"
EDGE_PASS = "root"
ENDPOINT = "ami-esp32c6-1494"

CMDS = [
    ("ip -6 addr show wpan0 | head -30",
     "wpan0 IPv6 addrs (looking for old alias bf5a:2554 + current mleid)"),
    ("ot-ctl ipaddr mleid 2>&1",
     "OTBR current mleid (the address SRP is published from)"),
    ("ot-ctl srp server host 2>&1 | head -20",
     "SRP server hosts published"),
    ("ot-ctl srp server service 2>&1 | head -20",
     "SRP server services (looking for _lwm2m._udp)"),
    ("ot-ctl history rx 30 2>&1 | grep -E '5683|:53\\b' | head -40",
     "ot-ctl rx history filtered for CoAP+DNS traffic"),
    ("docker exec tb-edge-v2 grep -E 'New registration|update.*registration' /var/log/tb-edge/tb-edge.log 2>/dev/null | tail -10",
     "TB Edge Leshan registration events"),
    ("docker logs --tail 30 tb-edge-v2 2>&1 | grep -iE 'register|ami-esp32c6-1494' | tail -10",
     "TB Edge container logs (registration events)"),
]


def main():
    print(f"[edge-check] connecting to {EDGE_USER}@{EDGE_HOST}...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(EDGE_HOST, username=EDGE_USER, password=EDGE_PASS,
                       timeout=10, allow_agent=False, look_for_keys=False)
    except Exception as e:
        print(f"[edge-check] SSH connect failed: {e}")
        return 1
    print(f"[edge-check] connected. Running {len(CMDS)} read-only checks.\n")

    for cmd, desc in CMDS:
        print(f"\n=== {desc} ===")
        print(f"$ {cmd}")
        stdin, stdout, stderr = client.exec_command(cmd, timeout=15)
        try:
            out = stdout.read().decode("utf-8", "replace").rstrip()
            err = stderr.read().decode("utf-8", "replace").rstrip()
        except Exception as e:
            out, err = "", str(e)
        if out:
            print(out)
        if err and err.strip() != "Done":
            print(f"[stderr] {err}")
    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
