"""Shared plumbing for the BENCHTOP ThingsBoard stack (tools/lab_tb/).

WHY THIS EXISTS
---------------
The lab bench (Windows 11 + WSL2 Ubuntu + native otbr-agent + docker inside
WSL) has no TB Edge on the LAN, so every fleet tool that resolves its server
through `fleet_common.edge_for_mesh()` points at an unreachable Pi. Worse, a
bench with NO LwM2M server is not a smaller version of production - it is a
DIFFERENT system: the node never gets a server-ACKed REGISTER, so
overlays/lab.conf has to disable the boot-register deadline and stretch the
HW-watchdog grace, and every registration/observe/RPC/OTA code path stays
unexercised. That bench manufactures failures (and hides real ones).

This module is the small amount of bench-specific glue that the production
tools legitimately do not have:

  * finding the bench TB (WSL2 NAT means localhost:8080 usually forwards into
    the distro, but not always - we probe localhost, 127.0.0.1 and the WSL
    eth0 IP),
  * waiting for a cold TB to finish booting (tb-postgres takes 60-180 s) and
    telling "still starting" apart from "demo data was never loaded, so
    tenant@thingsboard.org does not exist",
  * running `ot-ctl` / `docker` inside WSL from a Windows-side Python,
  * the DNS-SD names the firmware pins as a protocol contract.

Everything else (profile shape, observe/pmax map, model XMLs, NoSec
credentials) is IMPORTED from the production tools so the bench cannot drift:
    tools/tb_edge_provision.py        - profile body + device + credentials
    tools/tb_edge_upload_models.py    - model XML generators + upload
    tools/tb_edge_monitoring_setup.py - OBSERVE_ADDITIONS (pmin/pmax superset)

Not an entry point: use lab_tb_provision.py / lab_tb_check.py.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

# tools/lab_tb/lab_tb_common.py -> tools/ must be importable for fleet_common
TOOLS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = TOOLS_DIR.parent
MODELS_DIR = REPO_ROOT / "models"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import fleet_common as fc  # noqa: E402

fc.bootstrap_venv()

import requests  # noqa: E402

import tb_edge_provision as prov  # noqa: E402  (profile + device + creds)

# ── Bench identity ─────────────────────────────────────────────────────
# Derived, never hardcoded: the firmware builds the endpoint from the last
# two bytes of the link address (src/main.c build_endpoint_name()).
BENCH_MAC = "98:a3:16:61:3b:b0"
BENCH_ENDPOINT = fc.mac_to_endpoint(BENCH_MAC)      # ami-esp32c6-3bb0
PROFILE_NAME = fc.EDGE_PROFILE                      # AMI_LwM2M_Node

DEFAULT_TB_HOST = "localhost"
DEFAULT_TB_PORT = 8080
DEFAULT_DISTRO = "Ubuntu-24.04"

# ── DNS-SD contract (MUST match src/lwm2m_discover.c:22-26 verbatim) ───
# The node has NO static server IP since v0.6.65: it resolves the server via
# otDnsClientResolveService() against the OTBR's SRP server. These three
# strings are a versioned protocol constant, not configuration.
LWM2M_PORT = 5683
SRV_INSTANCE_LABEL = "ThingsBoard-Edge"
SRV_TYPE_DOMAIN = "_lwm2m._udp.default.service.arpa."
HOST_FQDN = "thingsboard-edge.default.service.arpa."
SRV_FQDN = f"{SRV_INSTANCE_LABEL}._lwm2m._udp.default.service.arpa."

OK, NO, WR, SK = "[ OK ]", "[FAIL]", "[WARN]", "[SKIP]"


# ── WSL / docker bridge ────────────────────────────────────────────────
def _decode(raw: bytes) -> str:
    """wsl.exe emits its OWN errors as UTF-16LE while the child process's
    output passes through as raw UTF-8. Detect and normalise."""
    if not raw:
        return ""
    if raw[:2] in (b"\xff\xfe",) or (len(raw) > 4 and raw[1] == 0 and raw[3] == 0):
        try:
            return raw.decode("utf-16-le", errors="replace").replace("\x00", "")
        except Exception:
            pass
    return raw.decode("utf-8", errors="replace")


def wsl_run(args: list[str], distro: str = DEFAULT_DISTRO, root: bool = True,
            timeout: int = 25) -> tuple[int, str]:
    """Run argv inside the WSL distro; return (returncode, stdout+stderr).

    Degrades gracefully: when this script is itself running inside Linux
    (e.g. someone runs it from the WSL side) the command is executed
    directly instead of being wrapped in wsl.exe.
    """
    if sys.platform == "win32":
        cmd = ["wsl.exe", "-d", distro] + (["-u", "root"] if root else []) + ["--"] + args
    else:
        cmd = args
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "wsl.exe not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s: {' '.join(args)}"
    return r.returncode, (_decode(r.stdout) + _decode(r.stderr)).strip()


_OT_CTL_PREFIX: list[str] | None = None


def ot_ctl(args: list[str], distro: str = DEFAULT_DISTRO,
           timeout: int = 20) -> tuple[int, str]:
    """`ot-ctl <args>` inside WSL.

    The bench migrated from a dockerised OTBR to a native systemd otbr-agent,
    so try the plain binary first and fall back to `docker exec otbr ot-ctl`
    for older bring-ups (docs/LAB_OTBR_BRINGUP.md). The working prefix is
    cached for the rest of the process.
    """
    global _OT_CTL_PREFIX
    prefixes = ([_OT_CTL_PREFIX] if _OT_CTL_PREFIX else
                [["ot-ctl"], ["docker", "exec", "otbr", "ot-ctl"]])
    last = (127, "ot-ctl not reachable")
    for pref in prefixes:
        rc, out = wsl_run(pref + args, distro=distro, timeout=timeout)
        if rc == 0 and "not found" not in out.lower():
            _OT_CTL_PREFIX = pref
            return rc, out
        last = (rc, out)
    return last


def tb_container(distro: str = DEFAULT_DISTRO) -> str | None:
    """Name of the running ThingsBoard container, detected by image name so we
    do not depend on whatever the compose stack called it."""
    rc, out = wsl_run(["docker", "ps", "--format", "{{.Names}}\t{{.Image}}"],
                      distro=distro)
    if rc != 0:
        return None
    for line in out.splitlines():
        if "\t" not in line:
            continue
        name, image = line.split("\t", 1)
        if "thingsboard" in image.lower() or "tb-" in image.lower():
            return name.strip()
    return None


def docker_logs(name: str, tail: int = 3000,
                distro: str = DEFAULT_DISTRO) -> str:
    rc, out = wsl_run(["docker", "logs", "--tail", str(tail), name],
                      distro=distro, timeout=60)
    return out if rc == 0 else ""


def udp_ports(distro: str = DEFAULT_DISTRO) -> set[int]:
    """UDP ports bound in the WSL network namespace (which is the one docker
    host-networking containers share). Parsed from /proc/net/udp6 + udp so we
    do not depend on iproute2 being installed."""
    ports: set[int] = set()
    rc, out = wsl_run(["sh", "-c", "cat /proc/net/udp6 /proc/net/udp"],
                      distro=distro)
    if rc != 0:
        return ports
    for line in out.splitlines():
        f = line.split()
        if len(f) < 2 or ":" not in f[1]:
            continue
        try:
            ports.add(int(f[1].rsplit(":", 1)[1], 16))
        except ValueError:
            continue
    return ports


def wsl_ip(distro: str = DEFAULT_DISTRO) -> str | None:
    rc, out = wsl_run(["hostname", "-I"], distro=distro, root=False, timeout=15)
    if rc != 0:
        return None
    for tok in out.split():
        if tok.count(".") == 3:
            return tok
    return None


# ── SRP / OpenThread parsing ───────────────────────────────────────────
def srp_blocks(text: str) -> list[tuple[str, str]]:
    """Split `ot-ctl srp server host|service` output into (header, body) pairs.

    Records are printed as an unindented FQDN followed by indented detail
    lines, e.g.

        ThingsBoard-Edge._lwm2m._udp.default.service.arpa.
            deleted: false, subtypes: (null), port:5683, weight:0, ...
            addresses: [fdaf:e549:1751:1:1199:8c2b:a32e:38ee]
    """
    blocks: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        if not line.strip() or line.strip() == "Done":
            continue
        if line[:1].isspace():
            if blocks:
                blocks[-1][1].append(line.strip())
        else:
            blocks.append((line.strip(), []))
    return [(h, " ".join(b)) for h, b in blocks]


def addrs_in(text: str) -> list[str]:
    """Every IPv6 literal inside `addresses: [a, b]` style text."""
    out: list[str] = []
    for chunk in re.findall(r"\[([^\]]+)\]", text):
        for tok in chunk.replace(",", " ").split():
            try:
                ipaddress.IPv6Address(tok)
            except ValueError:
                continue
            out.append(tok)
    return out


def mesh_local_prefix(distro: str = DEFAULT_DISTRO):
    """The Thread mesh-local /64, derived from `ot-ctl ipaddr mleid`.

    The firmware PREFERS an off-mesh-local (OMR) address for the LwM2M server
    (src/lwm2m_discover.c addr_is_mesh_local()) because the BR answers from
    its OMR address and a src/dst prefix mismatch breaks the connected UDP
    socket. We reuse the same rule to grade the published SRP record.
    """
    rc, out = ot_ctl(["ipaddr", "mleid"], distro=distro)
    if rc != 0:
        return None
    for line in out.splitlines():
        tok = line.strip()
        try:
            ipaddress.IPv6Address(tok)
        except ValueError:
            continue
        return ipaddress.IPv6Network(f"{tok}/64", strict=False)
    return None


def is_omr(addr: str, ml_prefix) -> bool:
    try:
        a = ipaddress.IPv6Address(addr)
    except ValueError:
        return False
    if a.is_link_local or a.is_loopback:
        return False
    if ml_prefix is not None and a in ml_prefix:
        return False
    return True


# ── ThingsBoard client ─────────────────────────────────────────────────
def tcp_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def candidate_hosts(explicit: str | None, distro: str = DEFAULT_DISTRO) -> list[str]:
    if explicit:
        return [explicit]
    hosts = ["localhost", "127.0.0.1"]
    ip = wsl_ip(distro)
    if ip and ip not in hosts:
        hosts.append(ip)
    return hosts


class TBUnavailable(RuntimeError):
    pass


def wait_for_tb(host: str | None, port: int, user: str, password: str,
                timeout_s: int = 600, distro: str = DEFAULT_DISTRO,
                poll_s: float = 5.0, quiet: bool = False):
    """Block until the bench TB answers /api/auth/login with a token.

    Returns (base_url, tb) where tb is a tools/tb_edge_provision.TB whose
    session carries BOTH `Authorization` and `X-Authorization` bearer headers
    (the resource/profile APIs use the former, the RPC + OTA APIs the latter).

    Raises TBUnavailable on timeout, or SystemExit on a hard credential
    failure - three consecutive 401s means TB is up but the tenant user does
    not exist, which on a fresh CE database means the install ran WITHOUT
    demo data (`install.sh --loadDemo`). Retrying that forever is useless.
    """
    hosts = candidate_hosts(host, distro)
    deadline = time.time() + max(timeout_s, 1)
    unauthorized = 0
    announced = False
    while True:
        for h in hosts:
            if not tcp_open(h, port):
                continue
            base = f"http://{h}:{port}"
            try:
                tb = prov.TB(base, user, password)
            except requests.HTTPError as e:
                code = getattr(e.response, "status_code", 0)
                if code in (401, 403):
                    unauthorized += 1
                    if unauthorized >= 3:
                        raise SystemExit(
                            f"TB at {base} is up but rejected {user!r} (HTTP {code}).\n"
                            "  The ThingsBoard database has no such tenant. A CE "
                            "install without demo data creates only sysadmin.\n"
                            "  Fix: re-run the TB install with --loadDemo, or pass "
                            "--user/--password for a tenant that exists.")
                continue
            except Exception:
                continue
            _dual_header(tb)
            if not quiet:
                print(f"[tb] up at {base}")
            return base, tb
        if time.time() >= deadline:
            raise TBUnavailable(
                f"no ThingsBoard on {hosts} port {port} after {timeout_s}s. "
                "Start the bench stack first (tools/lab_tb/ compose), and check "
                f"`wsl -d {distro} -- docker ps`.")
        if not quiet and not announced:
            print(f"[tb] waiting for ThingsBoard on {hosts}:{port} "
                  f"(up to {timeout_s}s; a cold tb-postgres needs 60-180s)...")
            announced = True
        time.sleep(poll_s)


def tb_client(base: str, user: str, password: str):
    """Bare (non-waiting) client, same dual-header treatment as wait_for_tb."""
    tb = prov.TB(base, user, password)
    _dual_header(tb)
    return tb


def _dual_header(tb) -> None:
    auth = tb.s.headers.get("Authorization")
    if auth:
        tb.s.headers.setdefault("X-Authorization", auth)


def find_device(tb, endpoint: str) -> dict | None:
    """Device lookup by LwM2M endpoint.

    Pass 1 exact name; pass 2 by credentialsId, because a TB Edge that has
    resynced with a cloud instance can mangle the device NAME while the
    credentials (== the endpoint the firmware presents) stay correct. Same
    two-pass rule as tools/provision_node.py:127-157.
    """
    try:
        found = tb.get("/api/tenant/deviceInfos",
                       {"pageSize": 100, "page": 0, "textSearch": endpoint})["data"]
    except Exception:
        return None
    for d in found:
        if d.get("name") == endpoint:
            return d
    for d in found:
        if endpoint not in d.get("name", ""):
            continue
        try:
            creds = tb.get(f"/api/device/{d['id']['id']}/credentials")
        except Exception:
            continue
        if creds.get("credentialsId") == endpoint:
            return d
    return None


def get_profile(tb, name: str = PROFILE_NAME) -> dict | None:
    try:
        profiles = tb.get("/api/deviceProfiles", {"pageSize": 100, "page": 0})["data"]
    except Exception:
        return None
    hit = next((p for p in profiles if p.get("name") == name), None)
    if not hit:
        return None
    try:
        return tb.get(f"/api/deviceProfile/{hit['id']['id']}")
    except Exception:
        return hit


def rpc(tb, device_id: str, method: str, params: dict,
        timeout_ms: int = 15000, oneway: bool = False) -> dict:
    """Two-way LwM2M RPC. Methods are Read / Execute / WriteReplace (plain
    'Write' returns METHOD_NOT_ALLOWED). A successful two-way Read is the only
    cheap proof that the INBOUND (server -> node) direction works - REG_UPDATE
    only proves the outbound half."""
    _dual_header(tb)
    kind = "oneway" if oneway else "twoway"
    r = tb.s.post(f"{tb.base}/api/rpc/{kind}/{device_id}",
                  json={"method": method, "params": params, "timeout": timeout_ms},
                  timeout=(timeout_ms / 1000.0) + 15)
    try:
        return r.json() if r.text else {}
    except ValueError:
        return {"http": r.status_code, "text": r.text[:200]}


def now_ms() -> int:
    return int(time.time() * 1000)
