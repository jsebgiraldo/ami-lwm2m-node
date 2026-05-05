"""Read-only diagnostic for a specific node from the edge side.

Usage:
    python tools/edge_check_node.py <endpoint>     e.g. ami-esp32c6-14c8

Checks:
  1. Is the device known to TB Edge? Active state?
  2. Does ot-ctl child table show it as a Thread child?
  3. ot-ctl history rx — is the node sending CoAP toward the edge?
  4. ot-ctl history rx — is it sending DNS queries (DNS-SD lookup)?
  5. SRP server clients — is the node publishing itself?
  6. TB Edge Leshan log — any registration / failure for this endpoint?

Read-only.
"""
from __future__ import annotations
import sys, pathlib, re
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fleet_common as fc
fc.bootstrap_venv()

import paramiko  # noqa: E402

EDGE_HOST = "192.168.1.175"
EDGE_USER = "root"
EDGE_PASS = "root"


def short_mac_from_endpoint(ep: str) -> str:
    """ami-esp32c6-14c8 → 14c8."""
    m = re.match(r"ami-esp32c6-([0-9a-f]+)", ep, re.I)
    return m.group(1).lower() if m else ep


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <endpoint>")
        return 2
    endpoint = sys.argv[1]
    short = short_mac_from_endpoint(endpoint)

    cmds = [
        (f"ot-ctl child table 2>&1",
         "OT child table (looking for any child)"),
        (f"ot-ctl neighbor table 2>&1 | head -30",
         "OT neighbor table (extended addresses of attached nodes)"),
        (f"ot-ctl srp server host 2>&1",
         "SRP clients registered (looking for endpoint or short MAC)"),
        (f"ot-ctl history rx 60 2>&1 | tail -80",
         "Recent OT rx history (last 60 entries)"),
        (f"ot-ctl history tx 60 2>&1 | tail -80",
         "Recent OT tx history (last 60 entries)"),
        (f"ot-ctl ipmaddr 2>&1 | head -10",
         "OT multicast subscriptions"),
        (f"docker exec tb-edge-postgres psql -tAU postgres thingsboard_edge -c "
         f"\"SELECT name, additional_info FROM device WHERE name LIKE '%{short}%' OR name = '{endpoint}';\" 2>&1",
         "TB Edge postgres: device record"),
        (f"docker exec tb-edge-v2 grep -i '{endpoint}\\|{short}' /var/log/tb-edge/tb-edge.log 2>/dev/null | tail -20",
         "TB Edge Leshan log (any line mentioning this node)"),
        (f"docker logs --tail 200 tb-edge-v2 2>&1 | grep -iE '{endpoint}|{short}|registration.*fail' | tail -20",
         "TB Edge container logs (registration/failures)"),
        (f"ot-ctl counters mac 2>&1 | head -30",
         "OT MAC counters (TX/RX errors, retries)"),
    ]

    print(f"[edge-check] target endpoint: {endpoint} (short={short})")
    print(f"[edge-check] connecting to {EDGE_USER}@{EDGE_HOST}...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(EDGE_HOST, username=EDGE_USER, password=EDGE_PASS,
                       timeout=10, allow_agent=False, look_for_keys=False)
    except Exception as e:
        print(f"[edge-check] SSH connect failed: {e}")
        return 1
    print(f"[edge-check] connected. Running {len(cmds)} read-only checks.\n")

    for cmd, desc in cmds:
        print(f"\n=== {desc} ===")
        print(f"$ {cmd}")
        try:
            stdin, stdout, stderr = client.exec_command(cmd, timeout=20)
            out = stdout.read().decode("utf-8", "replace").rstrip()
            err = stderr.read().decode("utf-8", "replace").rstrip()
        except Exception as e:
            out, err = "", str(e)
        if out:
            print(out)
        if err and err.strip() not in ("Done", ""):
            print(f"[stderr] {err}")
    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
