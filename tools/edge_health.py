#!/usr/bin/env python3
"""Pre-flight check for the Edge before starting a fleet deployment.

Verifies (over SSH + REST) that the OTBR + TB Edge are ready for nodes:
  1. TB Edge container running and LwM2M port listening
  2. AMI_LwM2M_Node profile exists in TB Edge
  3. OTBR SRP server running with thingsboard-edge host registered
  4. Hotplug script /etc/hotplug.d/iface/99-wpan0-undeprecate installed
  5. wpan0 mesh-local EID is currently NOT marked deprecated

Exits non-zero if any check fails. Use as gate before batch_onboard.
"""
from __future__ import annotations

import argparse
import sys

import fleet_common as fc

fc.bootstrap_venv()

OK = "[ OK ]"
NO = "[FAIL]"
WR = "[WARN]"


def ssh_run(client, cmd: str, timeout: int = 10) -> str:
    _, o, _ = client.exec_command(cmd, timeout=timeout)
    return o.read().decode(errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mesh", default=fc.DEFAULT_MESH, choices=fc.MESH_TARGETS,
                    help=f"Which Edge to check. Default: {fc.DEFAULT_MESH}")
    ap.add_argument("--host", default=None, help="Override host (else derived from --mesh)")
    ap.add_argument("--port", type=int, default=None, help="Override port (else derived from --mesh)")
    ap.add_argument("--user", default=fc.EDGE_TENANT_USER)
    ap.add_argument("--password", default=fc.EDGE_TENANT_PASS)
    ap.add_argument("--ssh-user", default="root")
    ap.add_argument("--ssh-pass", default="root")
    ap.add_argument("--profile", default=fc.EDGE_PROFILE)
    args = ap.parse_args()

    mesh_host, mesh_port = fc.edge_for_mesh(args.mesh)
    if args.host is None:
        args.host = mesh_host
    if args.port is None:
        args.port = mesh_port

    failures = 0
    warnings = 0

    # ---- 1. SSH to OTBR ------------------------------------------------
    try:
        import paramiko
    except ImportError:
        print(f"{NO} paramiko not installed (pip install paramiko)")
        return 2

    print(f"\nEdge target: {args.host}\n")

    try:
        c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(args.host, username=args.ssh_user, password=args.ssh_pass, timeout=5)
    except Exception as e:
        print(f"{NO} SSH to {args.host}: {e}")
        return 2

    # ---- 2. TB Edge container + LwM2M port ----------------------------
    docker_ps = ssh_run(c, "docker ps --format '{{.Names}} {{.Status}}' | grep -E '^tb-edge-v2 ' || true")
    if docker_ps.strip().startswith("tb-edge-v2") and "Up" in docker_ps:
        print(f"{OK} tb-edge container: {docker_ps.strip()}")
    else:
        print(f"{NO} tb-edge-v2 container not Up")
        failures += 1

    # check listener: 5683 = 0x1633
    listening = ssh_run(c, "awk '$2 ~ /:1633$/ {print $2}' /proc/net/udp6")
    if listening.strip():
        print(f"{OK} LwM2M listener on [::]:5683")
    else:
        print(f"{NO} no UDP listener on port 5683")
        failures += 1

    # ---- 3. SRP server + thingsboard-edge host ------------------------
    srp_state = ssh_run(c, "ot-ctl srp server state").strip()
    if "running" in srp_state:
        print(f"{OK} SRP server: running")
    else:
        print(f"{NO} SRP server: {srp_state or 'not running'}")
        failures += 1

    srp_host = ssh_run(c, "ot-ctl srp server host")
    if "thingsboard-edge.default.service.arpa." in srp_host and "deleted: false" in srp_host:
        print(f"{OK} SRP host thingsboard-edge.default.service.arpa. registered")
    else:
        print(f"{NO} SRP host thingsboard-edge.* missing or deleted")
        print("     (publish via avahi-publish-host or SRP client)")
        failures += 1

    # ---- 4. Hotplug script --------------------------------------------
    hp_path = "/etc/hotplug.d/iface/99-wpan0-undeprecate"
    hp = ssh_run(c, f"test -x {hp_path} && echo OK || echo MISSING").strip()
    if hp == "OK":
        print(f"{OK} hotplug {hp_path} present")
    else:
        print(f"{NO} hotplug script missing at {hp_path}")
        print("     run: scp tools/otbr/wpan0_undeprecate.sh root@HOST:/etc/hotplug.d/iface/99-wpan0-undeprecate")
        failures += 1

    # ---- 5. mesh-local EID NOT deprecated -----------------------------
    eid = ssh_run(c, "ot-ctl ipaddr mleid").splitlines()[0].strip()
    if eid:
        flags = ssh_run(c, f"ip -6 addr show wpan0 | grep -F '{eid}'")
        if "deprecated" in flags:
            print(f"{NO} mesh-local EID {eid} is DEPRECATED on wpan0 (run hotplug or manual fix)")
            failures += 1
        else:
            print(f"{OK} mesh-local EID {eid} preferred (not deprecated)")

    c.close()

    # ---- 6. TB Edge REST: profile exists ------------------------------
    try:
        from provision_node import TBClient
        tb = TBClient(args.host, args.port, args.user, args.password)
        tb.login()
        pid = tb.get_profile_id(args.profile)
        if pid:
            print(f"{OK} device profile '{args.profile}' exists ({pid[:8]}...)")
        else:
            print(f"{NO} device profile '{args.profile}' not found in TB Edge")
            failures += 1
    except Exception as e:
        print(f"{NO} TB Edge REST: {e}")
        failures += 1

    # ---- summary ------------------------------------------------------
    print()
    if failures == 0:
        print(f"All checks passed (warnings={warnings}). Edge is ready for fleet deployment.")
        return 0
    print(f"FAILED: {failures} check(s). Fix before starting batch deployment.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
