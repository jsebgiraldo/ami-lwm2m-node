#!/usr/bin/env python3
"""diag_get.py — server-side, on-demand IPv6 GET of a node's /diag endpoint.

Companion to the firmware CoAP diag server (src/coap_diag.c, fw >= 0.7.15-diag).
Each node runs a tiny CoAP server on [::]:5685/diag that answers a GET with a
JSON status snapshot, INDEPENDENT of its LwM2M/TB registration. This lets the
server ask a node "are you alive and what's your state?" directly over its
Thread IPv6 address — even when the node has dropped off ThingsBoard but is
still on the mesh ("reachable but not registered").

Zero third-party deps for the actual CoAP GET (raw UDP/IPv6, stdlib socket).
paramiko is only needed for the SSH transport modes (already a fleet dep).

Transports
  (default)      SSH to the OTBR host and run the GET from there — the OTBR is
                 on the Thread link and routes to every node's mesh-local/OMR
                 address. Works from any laptop with SSH to the OTBR.
  --local        Run the GET straight from THIS machine (use when this host
                 already has an IPv6 route onto the Thread mesh / OMR prefix).

Targets
  --addr <ipv6>  Probe this address (repeatable).
  --enumerate    Ask the OTBR for its Thread children (`ot-ctl childip`) and
                 probe every child mesh-local address it reports.
  (both may be combined; --enumerate implies the SSH transport.)

Examples
  # Probe two known node addresses via the OTBR:
  python tools/diag_get.py --addr fdde:ad00:beef:0:1234:5678:9abc:def0 \
                           --addr fd11:22:0:0:aaaa:bbbb:cccc:dddd
  # Auto-discover every mesh child and snapshot it:
  python tools/diag_get.py --enumerate
  # Run locally (this host is on the mesh):
  python tools/diag_get.py --local --addr fdde:ad00:beef:0:1234:5678:9abc:def0
"""
from __future__ import annotations
import argparse
import json
import sys

# --- OTBR / edge defaults (match tools/check_otbr_ssh.py) ---------------------
OTBR_HOST = "192.168.1.111"
OTBR_USER = "root"
OTBR_PASS = "root"
DIAG_PORT = 5685

# ─────────────────────────────────────────────────────────────────────────────
# Raw CoAP GET (stdlib only) — this exact source is also shipped to the OTBR
# host and executed there, so it must stay self-contained (no imports beyond
# the stdlib, no references to module-level globals).
# ─────────────────────────────────────────────────────────────────────────────
COAP_GET_SRC = r'''
import socket, os, sys, json

def coap_get(addr, port, path="diag", timeout=2.0, retries=4):
    """CoAP CON GET coap://[addr]:port/<path> with retransmission -> (code, str).

    CoAP framing (RFC 7252): 4-byte header + token + options + 0xFF + payload.
    We build a minimal GET with a single Uri-Path option and locate the payload
    by the 0xFF marker (never a valid option header; our response options carry
    no 0xFF value bytes).

    The request is CON (confirmable), NOT NON: a GET that expects a response
    must be confirmable — the Zephyr coap_server answers CON with a piggybacked
    ACK but does not reliably reply to NON. Over a Thread mesh the first packet
    is occasionally dropped (route/address resolution), so we retransmit up to
    `retries` times exactly as a real CON client would. Target the node's OMR
    address; mesh-local/RLOC addresses are only routable from on the mesh."""
    ver, typ, tkl = 1, 0, 1                       # v1, CON, 1-byte token
    seg = path.encode()
    # Uri-Path is option 11; delta 11 and len<13 fit the 1-byte option header.
    opt = bytes([(11 << 4) | len(seg)]) + seg

    ai = socket.getaddrinfo(addr, port, socket.AF_INET6, socket.SOCK_DGRAM)
    fam, styp, proto, _, sa = ai[0]

    for attempt in range(retries):
        token = os.urandom(tkl)
        mid = (os.getpid() + attempt) & 0xFFFF
        header = bytes([(ver << 6) | (typ << 4) | tkl, 0x01,   # 0x01 = 0.01 GET
                        (mid >> 8) & 0xFF, mid & 0xFF]) + token
        packet = header + opt
        s = socket.socket(fam, styp, proto)
        s.settimeout(timeout)
        try:
            s.sendto(packet, sa)
            data, _ = s.recvfrom(1500)
        except socket.timeout:
            continue
        finally:
            s.close()
        code = data[1] if len(data) > 1 else 0    # response code (e.g. 0x45 = 2.05)
        # Locate the 0xFF payload marker AFTER the header+token: a response token
        # byte can itself be 0xFF (~1/256), and starting the search at offset 4
        # (inside the token) would mistake it for the marker and misparse.
        rtkl = data[0] & 0x0F                       # response token length
        idx = data.find(b"\xFF", 4 + rtkl)
        payload = data[idx + 1:] if idx >= 0 else b""
        return code, payload.decode("utf-8", errors="replace")
    raise socket.timeout()

def probe(addr, port, path="diag"):
    try:
        code, body = coap_get(addr, port, path)
        cls, det = code >> 5, code & 0x1F
        ok = (cls == 2)
        rec = {"addr": addr, "ok": ok, "code": "%d.%02d" % (cls, det), "path": path}
        try:
            rec["data"] = json.loads(body)
        except Exception:
            rec["raw"] = body
        return rec
    except socket.timeout:
        return {"addr": addr, "ok": False, "path": path, "error": "timeout (no route / node off-mesh / server down)"}
    except Exception as e:
        return {"addr": addr, "ok": False, "path": path, "error": "%s: %s" % (type(e).__name__, e)}

def main(argv):
    port = int(argv[0])
    path = argv[1]
    addrs = argv[2:]
    out = [probe(a, port, path) for a in addrs]
    print(json.dumps(out))

if __name__ == "__main__":
    main(sys.argv[1:])
'''


def _run_local(addrs, port, path="diag"):
    """Execute the raw-CoAP prober in-process (this host is on the mesh)."""
    ns: dict = {}
    exec(compile(COAP_GET_SRC, "<coap_get>", "exec"), ns)
    return [ns["probe"](a, port, path) for a in addrs]


def _ssh_connect():
    try:
        import paramiko
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "paramiko", "-q"])
        import paramiko
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(OTBR_HOST, username=OTBR_USER, password=OTBR_PASS, timeout=8)
    return cli


def _ssh_exec(cli, cmd, timeout=25):
    _in, out, err = cli.exec_command(cmd, timeout=timeout)
    o = out.read().decode(errors="replace").strip()
    e = err.read().decode(errors="replace").strip()
    return o, e


def _enumerate_child_addrs(cli):
    """Parse `ot-ctl childip` -> list of child mesh-local IPv6 addresses.

    Output form:  '<childid>: <ipv6> <ipv6> ...' (one child per line)."""
    out, _ = _ssh_exec(cli, "ot-ctl childip")
    addrs = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.lower() in ("done", ""):
            continue
        # strip the leading "<id>:" label, keep the address tokens
        parts = line.split(":", 1)
        rhs = parts[1] if len(parts) == 2 else line
        for tok in rhs.split():
            if ":" in tok and len(tok) >= 3:      # looks like an IPv6 address
                addrs.append(tok)
    # de-dup, keep order
    seen, uniq = set(), []
    for a in addrs:
        if a not in seen:
            seen.add(a)
            uniq.append(a)
    return uniq


def _run_via_otbr(addrs, port, enumerate_children, path="diag"):
    cli = _ssh_connect()
    try:
        if enumerate_children:
            discovered = _enumerate_child_addrs(cli)
            print(f"[otbr] ot-ctl childip -> {len(discovered)} child address(es)", file=sys.stderr)
            addrs = list(dict.fromkeys(list(addrs) + discovered))
        if not addrs:
            print("[otbr] no target addresses (pass --addr or --enumerate)", file=sys.stderr)
            return []
        # Ship the prober to the OTBR host and run it there (python3 present on
        # Raspberry Pi OS). Feed it: <port> <path> <addr...>.
        import shlex
        remote = "/tmp/_ami_diag_get.py"
        sftp = cli.open_sftp()
        with sftp.file(remote, "w") as f:
            f.write(COAP_GET_SRC)
        sftp.close()
        args = " ".join(shlex.quote(a) for a in ([str(port), path] + list(addrs)))
        out, err = _ssh_exec(cli, f"python3 {remote} {args}")
        if err:
            print(f"[otbr:stderr] {err}", file=sys.stderr)
        try:
            return json.loads(out)
        except Exception:
            print(f"[otbr] unparseable output: {out!r}", file=sys.stderr)
            return []
    finally:
        cli.close()


def _fmt(rec):
    if rec.get("ok") and "data" in rec:
        d = rec["data"]
        if rec.get("path") == "ami":
            if not d.get("valid"):
                return f"  OK   {rec['addr']}  meter: no valid reading yet"
            return (f"  OK   {rec['addr']}  (meter, {d.get('age_s')}s old)\n"
                    f"       V r/s/t = {d.get('vr')}/{d.get('vs')}/{d.get('vt')} V   "
                    f"I r/s/t = {d.get('ir')}/{d.get('is')}/{d.get('it')} A\n"
                    f"       P={d.get('ptot')}kW Q={d.get('qtot')}kvar S={d.get('stot')}kVA pf={d.get('pf')}  "
                    f"E={d.get('eact')}kWh  f={d.get('freq')}Hz  "
                    f"reads={d.get('reads')} errs={d.get('errs')} mask={d.get('mask')}")
        return (f"  OK   {rec['addr']}\n"
                f"       fw={d.get('fw')} role={d.get('role')} up={d.get('uptime_s')}s "
                f"resets={d.get('resets')} reg_ok={d.get('reg_ok')} recover={d.get('recover')} "
                f"wdog={d.get('wdog')} boot_burst={d.get('boot_burst')} rloc16={d.get('rloc16')}")
    if rec.get("ok"):
        return f"  OK   {rec['addr']}  (raw: {rec.get('raw')})"
    return f"  FAIL {rec['addr']}  {rec.get('error') or rec.get('code')}"


def main():
    ap = argparse.ArgumentParser(
        description="Server-side IPv6 GET of node /diag (CoAP). "
                    "By default runs the GET from THIS host, which works from any "
                    "machine on the LAN that receives the OTBR's OMR-prefix route "
                    "(the central server and operator laptops do). Target the node's "
                    "OMR address (fdxx:...); mesh-local/RLOC addresses only route from "
                    "on the mesh.")
    ap.add_argument("--addr", action="append", default=[], metavar="IPV6",
                    help="node OMR IPv6 address to probe (repeatable)")
    ap.add_argument("--via-otbr", action="store_true",
                    help="tunnel the GET through the OTBR host over SSH (needs python3 on "
                         "the OTBR); use when this host has no route onto the mesh")
    ap.add_argument("--enumerate", action="store_true",
                    help="discover child addresses from the OTBR (ot-ctl childip) and probe "
                         "them; implies --via-otbr (child addrs are mesh-local)")
    ap.add_argument("--local", action="store_true",
                    help="(default) run the GET from THIS host")
    ap.add_argument("--port", type=int, default=DIAG_PORT, help=f"CoAP diag port (default {DIAG_PORT})")
    ap.add_argument("--path", default="diag",
                    help="CoAP resource to GET: 'diag' (node status, default) or 'ami' (live meter/OBIS)")
    ap.add_argument("--ami", action="store_true", help="shortcut for --path ami (live AMI metering values)")
    ap.add_argument("--json", action="store_true", help="emit raw JSON results")
    args = ap.parse_args()

    path = "ami" if args.ami else args.path

    use_ssh = args.via_otbr or args.enumerate
    if use_ssh:
        results = _run_via_otbr(args.addr, args.port, args.enumerate, path)
    else:
        if not args.addr:
            ap.error("no targets: pass --addr <ipv6> (or --enumerate to auto-discover via the OTBR)")
        results = _run_local(args.addr, args.port, path)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        alive = sum(1 for r in results if r.get("ok"))
        print(f"\n/{path} over IPv6  --  {alive}/{len(results)} reachable  (port {args.port})")
        for r in results:
            print(_fmt(r))

    # exit non-zero if nothing answered (useful in CI / health scripts)
    sys.exit(0 if any(r.get("ok") for r in results) else 1)


if __name__ == "__main__":
    main()
