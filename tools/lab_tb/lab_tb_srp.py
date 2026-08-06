#!/usr/bin/env python3
"""lab_tb_srp.py — make the LAB bench's LwM2M server DISCOVERABLE by the
AMI node, by registering it into the OTBR's Thread SRP server.

WHY THIS EXISTS
---------------
The AMI firmware has NO static server address. `CONFIG_AMI_LWM2M_SERVER_IPV6_*`
was gutted in v0.6.65 (prj.conf:385-395, Kconfig:280-296 — the symbols survive
but the code ignores them). src/lwm2m_discover.c is the ONLY way the node ever
learns where ThingsBoard lives, and it pins three strings as a protocol
contract, not as config:

    SRV_INSTANCE_LABEL  "ThingsBoard-Edge"
    SRV_TYPE_DOMAIN     "_lwm2m._udp.default.service.arpa."
    HOST_FQDN           "thingsboard-edge.default.service.arpa."

Strategy 1 = otDnsClientResolveService() → takes BOTH address and port from the
SRV record.  Strategy 2 = otDnsClientResolveAddress(HOST_FQDN) with the port
HARDCODED to 5683 (lwm2m_discover.c:22,148).  If both fail
CONFIG_AMI_LWM2M_DNS_SD_RETRY_MAX (=10) times the node sys_reboot()s.  So on a
bench with no published record the node CANNOT register, no matter how healthy
ThingsBoard is — which is exactly the failure this bench was manufacturing.

On the production Pi4 a persistent `otbr-srp` service put that record in the
SRP server (docs/DYNAMIC_DISCOVERY.md, tools/edge_health.py:94-100 asserts it,
hint: "publish via avahi-publish-host or SRP client").  Nothing in this repo
actually creates it.  This tool does.

WHICH MECHANISM AND WHY
-----------------------
The TB server runs on the WSL host, which is not a Thread node — so it cannot
run an SRP client of its own.  Three options exist:

  (a) avahi publishes _lwm2m._udp on the infra link + OTBR's *Discovery Proxy*
      bridges mDNS → Thread DNS-SD.  Architecturally the "right" answer for an
      infra-side service, but on THIS bench it depends on otbr-agent having
      been built with OTBR_DNSSD_DISCOVERY_PROXY=ON *and* on mDNS working over
      WSL2's NAT'd eth0, and it makes the advertised AAAA hard to control.
      Kept as the documented fallback (`install-avahi`), not the default.

  (b) *** RECOMMENDED, what this tool does by default ***  The OTBR's own
      OpenThread instance runs an SRP **client** as well as the server.  We
      point that client at the local SRP server and register
      host `thingsboard-edge` → the wpan0 **OMR** address, plus service
      `ThingsBoard-Edge._lwm2m._udp` on port 5683.  This is truthful (the OMR
      address really is an address of the machine running ThingsBoard), it
      lands the record in `ot-ctl srp server service` — the exact place
      production has it and edge_health.py checks — and it depends on no
      optional build flag but OT_SRP_CLIENT, which we probe.

  (c) There is NO ot-ctl command that injects a record straight into the SRP
      server registry; the registry is only writable by SRP updates.  (b) *is*
      the ot-ctl mechanism for a local service.

Pinning ONE address matters.  lwm2m_discover.c:86-91 takes whatever AAAA the
SRV additional-section carries, with no ability to choose; :113-131 prefers an
off-mesh-local address and warns "only mesh-local address available (may break
src/dst symmetry on BR)".  Registering ONLY the OMR address makes both
strategies agree and reproduces the 2026-06-04 production fix by construction.

USAGE (orchestrator runs these; this tool never touches a COM port)
-------------------------------------------------------------------
    # from Windows — everything is tunnelled through WSL:
    python tools/lab_tb/lab_tb_srp.py probe        # what does this otbr-agent support?
    python tools/lab_tb/lab_tb_srp.py address      # which IPv6 will be advertised?
    python tools/lab_tb/lab_tb_srp.py publish      # register + verify  <-- the gate
    python tools/lab_tb/lab_tb_srp.py verify       # re-check only (exit 1 if broken)
    python tools/lab_tb/lab_tb_srp.py install      # make it survive otbr-agent restarts
    python tools/lab_tb/lab_tb_srp.py remove       # unregister

    # from inside WSL the default --exec already works:
    python3 tools/lab_tb/lab_tb_srp.py publish --exec "ot-ctl"

    # docker-hosted OTBR instead of native systemd:
    python tools/lab_tb/lab_tb_srp.py publish \
        --exec "wsl -d Ubuntu-24.04 docker exec otbr ot-ctl"

Stdlib only.  Degrades gracefully: every failure path logs to stderr and
returns a non-zero exit code without a traceback, so it is safe to call from a
bring-up script.  --json emits a machine-readable summary on stdout.
"""
from __future__ import annotations

import argparse
import ipaddress
import json as _json
import os
import re
import shlex
import subprocess
import sys
import time

LAB_TB_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(LAB_TB_DIR))

# ── The protocol contract (src/lwm2m_discover.c:22-26) ───────────────────────
# Treat these as a versioned constant shared with the firmware, NOT as knobs.
# lab_tb_common.py is the single source of truth for the whole lab_tb package,
# so borrow from it when it imports — but keep working literals here too: that
# module pulls in requests/paramiko, and this publisher must stay runnable
# under a bare interpreter during bring-up, before any venv exists.
DEF_INSTANCE = "ThingsBoard-Edge"
DEF_SERVICE = "_lwm2m._udp"
DEF_HOST_LABEL = "thingsboard-edge"
DEF_DOMAIN = "default.service.arpa."
# lwm2m_discover.c:22 LWM2M_DEFAULT_PORT — Strategy 2 hardcodes this, so the TB
# LwM2M transport MUST bind 5683 (see docs/LAB_LWM2M_DISCOVERY.md port section).
DEF_PORT = 5683
DEF_DISTRO = "Ubuntu-24.04"

if LAB_TB_DIR not in sys.path:
    sys.path.insert(0, LAB_TB_DIR)
try:
    import lab_tb_common as _L  # noqa: E402
    DEF_INSTANCE = _L.SRV_INSTANCE_LABEL
    DEF_HOST_LABEL, DEF_DOMAIN = _L.HOST_FQDN.split(".", 1)
    DEF_PORT = _L.LWM2M_PORT
    DEF_DISTRO = _L.DEFAULT_DISTRO
except Exception:  # missing deps, or run standalone from a copy — fine
    _L = None

# Files installed into WSL for persistence.
SH_SRC = os.path.join(LAB_TB_DIR, "srp_publish_lwm2m.sh")
SH_DST = "/usr/local/sbin/srp_publish_lwm2m.sh"
UNIT_SRC = os.path.join(LAB_TB_DIR, "otbr-srp-lwm2m.service")
UNIT_DST = "/etc/systemd/system/otbr-srp-lwm2m.service"
UNIT_NAME = "otbr-srp-lwm2m.service"
AVAHI_SRC = os.path.join(LAB_TB_DIR, "avahi-lwm2m.service")
AVAHI_DST = "/etc/avahi/services/lwm2m-thingsboard.service"
AVAHI_HOSTS = "/etc/avahi/hosts"

ERR_RE = re.compile(r"^Error\s+\d+\s*:", re.I)

_VERBOSE = False


def log(msg: str) -> None:
    print(f"[srp-publish] {msg}", file=sys.stderr, flush=True)


def dbg(msg: str) -> None:
    if _VERBOSE:
        log(msg)


class OtError(RuntimeError):
    """ot-ctl could not be run at all (transport failure, not a CLI error)."""


# ─────────────────────────────────────────────────────────────────────────────
# ot-ctl transport
# ─────────────────────────────────────────────────────────────────────────────
def default_exec_prefix() -> str:
    """`ot-ctl` inside WSL, tunnelled from Windows when needed.

    The default user in the distro cannot open /run/openthread-wpan0.sock, so
    `-u root` is not optional here.
    """
    if sys.platform == "win32":
        return f"wsl -d {DEF_DISTRO} -u root -- ot-ctl"
    return "ot-ctl"


def default_shell_prefix() -> str:
    """Prefix that runs an arbitrary command (not ot-ctl) on the OTBR host."""
    if sys.platform == "win32":
        return f"wsl -d {DEF_DISTRO} -u root --"
    return ""


class OtCtl:
    """Thin, forgiving wrapper around one-shot `ot-ctl <args>` invocations."""

    def __init__(self, exec_prefix: str, timeout: int = 20):
        self.argv0 = shlex.split(exec_prefix)
        if not self.argv0:
            raise OtError("empty --exec prefix")
        self.timeout = timeout

    def raw(self, *args: str) -> tuple[int, str]:
        cmd = self.argv0 + list(args)
        dbg("run: " + " ".join(shlex.quote(c) for c in cmd))
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=self.timeout)
        except FileNotFoundError as e:
            raise OtError(
                f"command not found ({e.filename!r}). From Windows use "
                f'--exec "wsl -d Ubuntu-24.04 -u root -- ot-ctl"'
            )
        except subprocess.TimeoutExpired:
            raise OtError(
                f"ot-ctl timed out after {self.timeout}s running {' '.join(args)!r} "
                "- is otbr-agent up? (systemctl status otbr-agent)"
            )
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode, out.replace("\r\n", "\n").replace("\r", "\n")

    def lines(self, *args: str) -> list[str]:
        """Run and return payload lines: no 'Done', no blanks, errors stripped."""
        _, out = self.raw(*args)
        res = []
        for ln in out.split("\n"):
            s = ln.strip()
            if not s or s == "Done":
                continue
            res.append(s)
        return res

    def ok(self, *args: str) -> tuple[bool, str]:
        """True when ot-ctl printed 'Done' and no 'Error N:' line."""
        _, out = self.raw(*args)
        has_err = any(ERR_RE.match(l.strip()) for l in out.split("\n"))
        done = any(l.strip() == "Done" for l in out.split("\n"))
        return (done and not has_err), out.strip()

    def one(self, *args: str) -> str:
        ls = self.lines(*args)
        return ls[0] if ls else ""


# ─────────────────────────────────────────────────────────────────────────────
# Address selection — mirror of the firmware's own preference logic
# ─────────────────────────────────────────────────────────────────────────────
def _as_ip6(tok: str):
    try:
        return ipaddress.IPv6Address(tok.strip())
    except ValueError:
        return None


def omr_prefix(ot: OtCtl):
    """`br omrprefix` -> IPv6Network, preferring the Favored over the Local one."""
    fav = loc = None
    for ln in ot.lines("br", "omrprefix"):
        if ERR_RE.match(ln):
            return None
        parts = ln.split()
        if len(parts) < 2:
            continue
        if parts[0].lower().startswith("favored"):
            fav = parts[1]
        elif parts[0].lower().startswith("local"):
            loc = parts[1]
    for cand in (fav, loc):
        if not cand:
            continue
        try:
            return ipaddress.IPv6Network(cand, strict=False)
        except ValueError:
            continue
    return None


def mesh_local_prefix(ot: OtCtl):
    """/64 of the mesh-local EID - everything sharing it (RLOC/ALOC/ML-EID) is
    off-limits as an advertised address (lwm2m_discover.c:43-57)."""
    a = _as_ip6(ot.one("ipaddr", "mleid"))
    if a is None:
        return None
    return ipaddress.IPv6Network(f"{a}/64", strict=False)


def pick_address(ot: OtCtl, explicit: str | None = None) -> ipaddress.IPv6Address:
    """Choose the IPv6 address to advertise for thingsboard-edge.

    Preference order, deliberately identical to the node's own:
      1. an address inside the BR's OMR prefix  (what we want - routable,
         and Linux picks it as the reply source for traffic from the mesh)
      2. any global address that is neither link-local nor mesh-local
      3. hard failure — advertising a mesh-local address is the 2026-06-04
         src/dst-mismatch bug, so we refuse rather than publish a trap.
    """
    if explicit:
        a = _as_ip6(explicit)
        if a is None:
            raise OtError(f"--addr {explicit!r} is not a valid IPv6 address")
        return a

    addrs = [a for a in (_as_ip6(l) for l in ot.lines("ipaddr")) if a is not None]
    if not addrs:
        raise OtError("`ot-ctl ipaddr` returned no addresses - is Thread up? "
                      "(ot-ctl state must be leader/router/child)")

    omr = omr_prefix(ot)
    mlp = mesh_local_prefix(ot)
    dbg(f"ipaddr={[str(a) for a in addrs]} omr={omr} mesh-local={mlp}")

    if omr is not None:
        for a in addrs:
            if a in omr:
                return a

    for a in addrs:
        if a.is_link_local or a.is_multicast or a.is_loopback:
            continue
        if mlp is not None and a in mlp:
            continue
        return a

    raise OtError(
        "no off-mesh-local (OMR) address on wpan0 - the border router is not "
        "publishing an OMR prefix. Fix that first: `ot-ctl br state` must be "
        "'running' and `ot-ctl br omrprefix` must show an fd..::/64. "
        "Publishing a mesh-local address here reproduces the 2026-06-04 "
        "src/dst-mismatch outage; refusing."
    )


# ─────────────────────────────────────────────────────────────────────────────
# SRP server registry parsing
# ─────────────────────────────────────────────────────────────────────────────
# A field line is `key: value` where the key is a bare identifier. A record
# NAME line is an FQDN — it contains dots and no colon — so the two can never
# be confused, not even by `host: thingsboard-edge.default.service.arpa.`
# (which a naive "does it end in service.arpa.?" test gets wrong).
FIELD_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*)$")
IP6_TOKEN_RE = re.compile(r"\b[0-9A-Fa-f:]{2,}:[0-9A-Fa-f:]+\b")


def parse_srp_records(lines: list[str]) -> list[tuple[str, dict]]:
    """`srp server host|service` output -> [(name, {field: value})].

    Record blocks look like:
        ThingsBoard-Edge._lwm2m._udp.default.service.arpa.
            deleted: false
            port: 5683
            host: thingsboard-edge.default.service.arpa.
            addresses: [fdaf:e549:1751:1:1199:8c2b:a32e:38ee]
    ot-ctl indents the fields, but lines() has already stripped indentation, so
    FIELD_RE does the discrimination instead.
    """
    recs: list[tuple[str, dict]] = []
    cur_name, cur = None, {}
    for ln in lines:
        if ERR_RE.match(ln):
            continue
        m = FIELD_RE.match(ln)
        if m and cur_name is not None:
            cur[m.group(1).strip().lower()] = m.group(2).strip()
        elif m is None:
            if cur_name is not None:
                recs.append((cur_name, cur))
            cur_name, cur = ln.strip(), {}
    if cur_name is not None:
        recs.append((cur_name, cur))
    return recs


def extract_ip6(text: str) -> list[ipaddress.IPv6Address]:
    """Pull every IPv6 literal out of free-form ot-ctl text.

    OpenThread prints addresses UNCOMPRESSED (fdde:ad00:beef:0:0:ff:fe00:fc00)
    while Python's ipaddress canonicalises with '::'. Comparing objects instead
    of strings is the only way these two ever agree.
    """
    out = []
    for tok in IP6_TOKEN_RE.findall(text):
        tok = tok.strip("[](), ")
        # The word boundary happily swallows the delimiting colon of a label
        # such as "HostAddress:fdaf:..." — drop a single stray edge colon, but
        # never break a legitimate "::" run.
        if tok.startswith(":") and not tok.startswith("::"):
            tok = tok[1:]
        if tok.endswith(":") and not tok.endswith("::"):
            tok = tok[:-1]
        a = _as_ip6(tok)
        if a is not None and a not in out:
            out.append(a)
    return out


def rec_addresses(rec: dict) -> list[ipaddress.IPv6Address]:
    return extract_ip6(rec.get("addresses", ""))


# ─────────────────────────────────────────────────────────────────────────────
# Capability probe
# ─────────────────────────────────────────────────────────────────────────────
def probe(ot: OtCtl) -> dict:
    """Report what this otbr-agent build can actually do. No side effects."""
    info: dict = {}
    info["thread_state"] = ot.one("state") or "?"
    info["rloc16"] = ot.one("rloc16")
    srv = ot.one("srp", "server", "state")
    info["srp_server_state"] = srv or "?"

    cli = ot.one("srp", "client", "state")
    info["srp_client_cli"] = not bool(ERR_RE.match(cli or "Error 35:"))
    info["srp_client_state"] = cli
    info["srp_client_autostart"] = ot.one("srp", "client", "autostart")
    info["srp_client_host_state"] = ot.one("srp", "client", "host", "state")

    dns = ot.lines("dns", "config")
    info["dns_client_cli"] = bool(dns) and not ERR_RE.match(dns[0])
    info["dns_config"] = dns

    try:
        p = omr_prefix(ot)
        info["omr_prefix"] = str(p) if p else None
    except OtError:
        info["omr_prefix"] = None
    info["br_state"] = ot.one("br", "state")
    return info


# ─────────────────────────────────────────────────────────────────────────────
# Publish / remove
# ─────────────────────────────────────────────────────────────────────────────
def ensure_srp_server(ot: OtCtl, wait_s: int = 15) -> str:
    state = ot.one("srp", "server", "state")
    if "running" in state.lower():
        return state
    log(f"SRP server state={state or '?'} - enabling")
    ok, out = ot.ok("srp", "server", "enable")
    if not ok and "already" not in out.lower():
        dbg(f"srp server enable -> {out}")
    deadline = time.time() + wait_s
    while time.time() < deadline:
        state = ot.one("srp", "server", "state")
        if "running" in state.lower():
            return state
        time.sleep(1)
    return state


def find_record(ot: OtCtl, subcmd: tuple[str, ...], fqdn: str) -> dict | None:
    for name, rec in parse_srp_records(ot.lines(*subcmd)):
        if name.lower() == fqdn.lower():
            return rec
    return None


def already_published(ot: OtCtl, cfg: dict, addr) -> bool:
    """Idempotency gate: is the exact record we want already in the registry?

    Port + deleted come from the service record, the address from the host
    record - the service block does not always echo the host's addresses, and
    getting that wrong would make every re-check tear down and re-register,
    blinking the record the node depends on.
    """
    svc_fqdn = f"{cfg['instance']}.{cfg['service']}.{cfg['domain']}"
    host_fqdn = f"{cfg['host_label']}.{cfg['domain']}"

    svc = find_record(ot, ("srp", "server", "service"), svc_fqdn)
    if svc is None or svc.get("deleted", "").lower() != "false":
        return False
    if svc.get("port") != str(cfg["port"]):
        return False

    host = find_record(ot, ("srp", "server", "host"), host_fqdn)
    if host is None or host.get("deleted", "").lower() != "false":
        return False
    return addr in rec_addresses(host)


def publish(ot: OtCtl, cfg: dict, addr, wait_s: int) -> bool:
    """Register host + service through the OTBR's own SRP client (loopback)."""
    host_fqdn = f"{cfg['host_label']}.{cfg['domain']}"

    if already_published(ot, cfg, addr):
        log(f"already published: {cfg['instance']}.{cfg['service']} "
            f"-> [{addr}]:{cfg['port']} (no-op)")
        return True

    # Clean slate. `stop` + `host clear` wipes the CLIENT's view without
    # touching the persisted ECDSA key (OT keeps it in settings), so the
    # re-registration re-uses the same KEY and the SRP server accepts it as an
    # update of the same name instead of a name conflict. Errors are expected
    # on a first run — ignore them.
    ot.ok("srp", "client", "stop")
    ot.ok("srp", "client", "service", "clear")
    ot.ok("srp", "client", "host", "clear")

    steps = [
        ("host name", ("srp", "client", "host", "name", cfg["host_label"])),
        ("host address", ("srp", "client", "host", "address", str(addr))),
        ("service add", ("srp", "client", "service", "add",
                         cfg["instance"], cfg["service"], str(cfg["port"]))),
    ]
    for label, args in steps:
        ok, out = ot.ok(*args)
        if not ok:
            log(f"FAIL srp client {label}: {out.strip() or 'no output'}")
            if "InvalidCommand" in out:
                log("     this otbr-agent was built WITHOUT OT_SRP_CLIENT=ON - "
                    "use the avahi fallback: "
                    "python tools/lab_tb/lab_tb_srp.py install-avahi")
            return False

    if cfg.get("server"):
        host, _, port = cfg["server"].rpartition(":")
        host = host.strip("[]") or cfg["server"]
        ok, out = ot.ok("srp", "client", "start", host, port or "53")
    else:
        ok, out = ot.ok("srp", "client", "autostart", "enable")
    if not ok:
        log(f"FAIL srp client start/autostart: {out.strip()}")
        return False

    deadline = time.time() + wait_s
    state = ""
    while time.time() < deadline:
        state = ot.one("srp", "client", "host", "state")
        if state.lower().startswith("registered"):
            log(f"registered: {host_fqdn} -> [{addr}] "
                f"+ {cfg['instance']}.{cfg['service']} port {cfg['port']}")
            return True
        time.sleep(1)

    log(f"TIMEOUT after {wait_s}s: srp client host state = {state or '?'} "
        "(expected 'Registered')")
    log("     server the client picked: " + (ot.one("srp", "client", "server") or "?"))
    log("     if it never leaves 'ToAdd', autostart did not find the local SRP "
        "server; retry with --server '[<mleid>]:<port>' "
        "(read the port from `ot-ctl netdata show` Services)")
    return False


def remove(ot: OtCtl) -> bool:
    """Unregister: release the name AND its key lease so a later re-publish
    with a different key is not rejected as a name conflict."""
    ok1, o1 = ot.ok("srp", "client", "host", "remove", "1", "1")
    dbg(f"host remove -> {o1.strip()}")
    deadline = time.time() + 15
    while time.time() < deadline:
        st = ot.one("srp", "client", "host", "state")
        if st.lower().startswith("removed") or not st:
            break
        time.sleep(1)
    ot.ok("srp", "client", "service", "clear")
    ot.ok("srp", "client", "host", "clear")
    ot.ok("srp", "client", "stop")
    log("unregistered ThingsBoard-Edge from the SRP server")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Verification — the part that PROVES a Thread node can resolve it
# ─────────────────────────────────────────────────────────────────────────────
def verify(ot: OtCtl, cfg: dict, addr=None) -> tuple[bool, dict]:
    """Four checks, escalating from 'the record exists' to 'the exact API the
    firmware calls returns the right answer'.

      1/4 SRP server registry holds the host   (what edge_health.py checks)
      2/4 SRP server registry holds the service, right port + address
      3/4 otDnsClientResolveService via `dns service` == lwm2m_discover.c
          Strategy 1
      4/4 otDnsClientResolveAddress via `dns resolve` == Strategy 2
    3 and 4 run the BR's own DNS client against the BR's DNS-SD server, i.e.
    the same query the node emits, minus the radio hop.
    """
    res: dict = {"checks": []}
    ok_all = True
    host_fqdn = f"{cfg['host_label']}.{cfg['domain']}"
    svc_fqdn = f"{cfg['service']}.{cfg['domain']}"
    inst_fqdn = f"{cfg['instance']}.{svc_fqdn}"

    def note(name, ok, detail):
        nonlocal ok_all
        res["checks"].append({"check": name, "ok": bool(ok), "detail": detail})
        ok_all = ok_all and bool(ok)
        print(f"{'[ OK ]' if ok else '[FAIL]'} {name}: {detail}")

    # 1/4 host record
    found = find_record(ot, ("srp", "server", "host"), host_fqdn)
    if found is None:
        note("srp server host", False, f"{host_fqdn} absent from the registry")
    elif found.get("deleted", "").lower() != "false":
        note("srp server host", False, f"{host_fqdn} present but deleted:true")
    else:
        ips = rec_addresses(found)
        good = (addr is None) or (addr in ips)
        note("srp server host", good,
             f"{host_fqdn} -> {[str(i) for i in ips] or 'no addresses'}")

    # 2/4 service record
    found = find_record(ot, ("srp", "server", "service"), inst_fqdn)
    if found is None:
        note("srp server service", False, f"{inst_fqdn} absent from the registry")
    else:
        port_ok = found.get("port") == str(cfg["port"])
        del_ok = found.get("deleted", "").lower() == "false"
        note("srp server service", port_ok and del_ok,
             f"{inst_fqdn} port={found.get('port')} "
             f"deleted={found.get('deleted')} host={found.get('host')} "
             f"addresses={found.get('addresses')}")

    # 3/4 + 4/4 need a DNS server to query. Prefer the BR's own DNS-SD server;
    # try the implicit default first, then explicit <addr>:53 fallbacks.
    servers: list[tuple[str, ...]] = [()]
    for cand in (addr, _as_ip6(ot.one("ipaddr", "mleid"))):
        if cand is not None:
            servers.append((str(cand), "53"))

    def try_dns(args: tuple[str, ...], validate) -> tuple[bool, str]:
        last = ""
        for srv in servers:
            out = ot.lines(*args, *srv)
            blob = " | ".join(out)
            via = f"   [via {srv[0]}]" if srv else "   [via default DNS config]"
            if blob:
                last = blob + via
            if not out or any(ERR_RE.match(l) for l in out):
                continue
            if validate(blob):
                return True, blob + via
        return False, last or "no response from any DNS server"

    def svc_ok(blob: str) -> bool:
        # `dns service` prints "Port:5683, Priority:0, Weight:0, TTL:7200"
        if not re.search(rf"Port:\s*{cfg['port']}\b", blob):
            return False
        # The AAAA must ride along in the additional section: Strategy 1 takes
        # info.mHostAddress verbatim (lwm2m_discover.c:90) and has no second
        # chance to look it up.
        return addr is None or addr in extract_ip6(blob)

    def host_ok(blob: str) -> bool:
        ips = extract_ip6(blob)
        return bool(ips) and (addr is None or addr in ips)

    ok3, d3 = try_dns(("dns", "service", cfg["instance"], svc_fqdn), svc_ok)
    note("dns service (firmware Strategy 1)", ok3, d3)

    ok4, d4 = try_dns(("dns", "resolve", host_fqdn), host_ok)
    note("dns resolve (firmware Strategy 2)", ok4, d4)

    res["ok"] = ok_all
    return ok_all, res


# ─────────────────────────────────────────────────────────────────────────────
# Persistence — push the shell publisher + systemd unit into WSL
# ─────────────────────────────────────────────────────────────────────────────
class Shell:
    """Runs arbitrary commands on the OTBR host (WSL) as root."""

    def __init__(self, prefix: str, timeout: int = 60):
        self.argv0 = shlex.split(prefix) if prefix.strip() else []
        self.timeout = timeout

    def run(self, args: list[str], stdin: bytes | None = None) -> tuple[int, str]:
        cmd = self.argv0 + args
        dbg("shell: " + " ".join(shlex.quote(c) for c in cmd))
        try:
            p = subprocess.run(cmd, input=stdin, capture_output=True,
                               timeout=self.timeout)
        except FileNotFoundError as e:
            raise OtError(f"command not found ({e.filename!r})")
        except subprocess.TimeoutExpired:
            raise OtError(f"timed out running {args!r}")
        out = (p.stdout or b"").decode(errors="replace") + \
              (p.stderr or b"").decode(errors="replace")
        return p.returncode, out.replace("\r\n", "\n")

    def sh(self, script: str) -> tuple[int, str]:
        return self.run(["sh", "-c", script])

    def put(self, local_path: str, remote_path: str, mode: str = "0644") -> None:
        """Copy a repo file to the OTBR host, forcing LF endings.

        Windows checkouts happily hand you CRLF; a shell script or a systemd
        unit with CRLF fails in ways that read like a logic bug
        ("/bin/sh^M: bad interpreter"). Normalising here kills that whole class.
        """
        with open(local_path, "rb") as f:
            data = f.read().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        rc, out = self.run(["tee", remote_path], stdin=data)
        if rc != 0:
            raise OtError(f"could not write {remote_path}: {out.strip()}")
        self.run(["chmod", mode, remote_path])
        log(f"installed {remote_path} ({len(data)} bytes, mode {mode})")


def action_install(sh: Shell, cfg: dict) -> int:
    """SRP path persistence: publisher script + systemd unit, enabled now.

    Without this the registration is RAM-only: it dies with otbr-agent and the
    node silently loses its server on the next restart. This is the bench
    equivalent of the Pi4's `otbr-srp` UCI service.
    """
    for src in (SH_SRC, UNIT_SRC):
        if not os.path.exists(src):
            log(f"missing repo file {src}")
            return 2
    sh.put(SH_SRC, SH_DST, "0755")
    sh.put(UNIT_SRC, UNIT_DST, "0644")
    rc, out = sh.sh(
        "systemctl daemon-reload && "
        f"systemctl enable {UNIT_NAME} && "
        f"systemctl restart {UNIT_NAME} && "
        f"sleep 2 && systemctl is-active {UNIT_NAME}"
    )
    print(out.strip())
    if rc != 0:
        log(f"systemctl failed (rc={rc}). Check: "
            f"journalctl -u {UNIT_NAME} -n 50 --no-pager")
        return 1
    log(f"{UNIT_NAME} enabled and active - the record now survives "
        "otbr-agent restarts and Thread re-attach")
    return 0


def action_uninstall(sh: Shell) -> int:
    rc, out = sh.sh(
        f"systemctl disable --now {UNIT_NAME} 2>/dev/null; "
        f"rm -f {UNIT_DST} {SH_DST}; systemctl daemon-reload; echo removed"
    )
    print(out.strip())
    return 0 if rc == 0 else 1


def action_install_avahi(sh: Shell, cfg: dict, addr) -> int:
    """FALLBACK path: publish over mDNS and let the OTBR Discovery Proxy bridge
    it into Thread DNS-SD. Only for an otbr-agent built without OT_SRP_CLIENT.

    NEVER run this alongside the SRP path: the OTBR's Advertising Proxy already
    mirrors the SRP registration to mDNS, so a second avahi copy of the same
    instance name collides and avahi renames it 'ThingsBoard-Edge #2' - which
    no longer matches SRV_INSTANCE_LABEL and breaks discovery.
    """
    if not os.path.exists(AVAHI_SRC):
        log(f"missing repo file {AVAHI_SRC}")
        return 2
    rc, out = sh.sh("command -v avahi-daemon >/dev/null && echo yes || echo no")
    if "yes" not in out:
        log("avahi-daemon is not installed. Install it first: "
            "sudo apt-get update && sudo apt-get install -y avahi-daemon avahi-utils")
        return 2

    sh.put(AVAHI_SRC, AVAHI_DST, "0644")

    host_local = f"{cfg['host_label']}.local"
    # /etc/avahi/hosts is the only way to make avahi publish an AAAA it does
    # not own — and the wpan0 OMR address is exactly that from avahi's view.
    script = (
        f"touch {AVAHI_HOSTS}; "
        f"sed -i '/[[:space:]]{cfg['host_label']}\\.local$/d' {AVAHI_HOSTS}; "
        f"printf '%s %s\\n' '{addr}' '{host_local}' >> {AVAHI_HOSTS}; "
        "systemctl restart avahi-daemon && sleep 2 && "
        "systemctl is-active avahi-daemon"
    )
    rc, out = sh.sh(script)
    print(out.strip())
    if rc != 0:
        log("avahi-daemon restart failed")
        return 1
    log(f"avahi now publishes {cfg['instance']}._lwm2m._udp.local port "
        f"{cfg['port']} -> {host_local} -> {addr}")
    log("NOTE: this only reaches the node if otbr-agent was built with "
        "OTBR_DNSSD_DISCOVERY_PROXY=ON. Prove it with: "
        "python tools/lab_tb/lab_tb_srp.py verify")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    global _VERBOSE
    ap = argparse.ArgumentParser(
        description="Publish the bench LwM2M server into the OTBR's Thread "
                    "DNS-SD so the AMI node's SRP discovery resolves.")
    ap.add_argument("action", nargs="?", default="publish",
                    choices=("publish", "verify", "remove", "address", "probe",
                             "install", "uninstall", "install-avahi"),
                    help="what to do (default: publish)")
    ap.add_argument("--exec", dest="exec_prefix", default=None,
                    help="command that runs ot-ctl on the OTBR "
                         f"(default: {default_exec_prefix()!r})")
    ap.add_argument("--shell", dest="shell_prefix", default=None,
                    help="command prefix that runs a shell command on the OTBR "
                         f"host (default: {default_shell_prefix()!r})")
    ap.add_argument("--instance", default=DEF_INSTANCE,
                    help="SRV instance label - MUST match SRV_INSTANCE_LABEL "
                         "in src/lwm2m_discover.c:24")
    ap.add_argument("--service", default=DEF_SERVICE,
                    help="service type (default %(default)s)")
    ap.add_argument("--host-label", default=DEF_HOST_LABEL,
                    help="SRP host label - MUST match HOST_FQDN in "
                         "src/lwm2m_discover.c:26")
    ap.add_argument("--domain", default=DEF_DOMAIN,
                    help="DNS-SD domain (default %(default)s)")
    ap.add_argument("--port", type=int, default=DEF_PORT,
                    help="LwM2M/CoAP port TB binds (default %(default)s)")
    ap.add_argument("--addr", default=None,
                    help="advertise this IPv6 instead of auto-picking the "
                         "wpan0 OMR address")
    ap.add_argument("--server", default=None, metavar="[ADDR]:PORT",
                    help="explicit SRP server for `srp client start` instead "
                         "of autostart (escape hatch)")
    ap.add_argument("--wait", type=int, default=45,
                    help="seconds to wait for host state Registered")
    ap.add_argument("--json", action="store_true", help="machine-readable stdout")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    _VERBOSE = args.verbose

    if args.port != DEF_PORT:
        log(f"WARNING: --port {args.port} != {DEF_PORT}. Strategy 2 of "
            "src/lwm2m_discover.c hardcodes 5683, so a node that falls back to "
            "the host-AAAA path will talk to the wrong port.")

    cfg = {
        "instance": args.instance,
        "service": args.service,
        "host_label": args.host_label,
        "domain": args.domain if args.domain.endswith(".") else args.domain + ".",
        "port": args.port,
        "server": args.server,
    }

    exec_prefix = args.exec_prefix or default_exec_prefix()
    shell_prefix = args.shell_prefix if args.shell_prefix is not None \
        else default_shell_prefix()

    try:
        ot = OtCtl(exec_prefix)

        if args.action == "probe":
            info = probe(ot)
            if args.json:
                print(_json.dumps(info, indent=2))
            else:
                for k, v in info.items():
                    print(f"  {k:24s} {v}")
                if not info["srp_client_cli"]:
                    print("\n  -> No `srp client` CLI: this otbr-agent lacks "
                          "OT_SRP_CLIENT=ON.\n     Use the fallback: "
                          "python tools/lab_tb/lab_tb_srp.py install-avahi")
            return 0

        if args.action == "uninstall":
            return action_uninstall(Shell(shell_prefix))

        if args.action == "remove":
            remove(ot)
            return 0

        if args.action == "install":
            # Deliberately before pick_address: the installed daemon waits for
            # Thread attach on its own, so the unit can be laid down before the
            # mesh is even up.
            return action_install(Shell(shell_prefix), cfg)

        addr = pick_address(ot, args.addr)

        if args.action == "address":
            if args.json:
                print(_json.dumps({"address": str(addr)}))
            else:
                print(addr)
            return 0

        if args.action == "install-avahi":
            return action_install_avahi(Shell(shell_prefix), cfg, addr)

        if args.action == "publish":
            state = ot.one("state")
            if state.lower() not in ("leader", "router", "child"):
                log(f"Thread state is {state!r} - the OTBR must be attached "
                    "before anything can be registered")
                return 2
            srp_state = ensure_srp_server(ot)
            if "running" not in srp_state.lower():
                log(f"SRP server did not reach 'running' (state={srp_state!r})")
                return 2
            log(f"advertising [{addr}]:{cfg['port']} as "
                f"{cfg['instance']}.{cfg['service']}.{cfg['domain']}")
            if not publish(ot, cfg, addr, args.wait):
                return 1

        ok, res = verify(ot, cfg, addr)
        res["address"] = str(addr)
        res["port"] = cfg["port"]
        res["instance_fqdn"] = f"{cfg['instance']}.{cfg['service']}.{cfg['domain']}"
        res["host_fqdn"] = f"{cfg['host_label']}.{cfg['domain']}"
        if args.json:
            print(_json.dumps(res, indent=2))
        if not ok:
            log("verification FAILED - the node will not be able to discover "
                "the server. See docs/LAB_LWM2M_DISCOVERY.md troubleshooting.")
            return 1
        log("discovery chain is healthy; power-cycle the node and watch for "
            '"DNS-SD service resolved: coap://[...]:5683"')
        return 0

    except OtError as e:
        log(str(e))
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
