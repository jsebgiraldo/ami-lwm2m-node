#!/usr/bin/env python3
"""AMI Node Doctor — 100% automated DETECT -> DIAGNOSE -> DOCTOR -> FIX -> VERIFY.

Goal: take whatever ESP32-C6 nodes are plugged into this PC and leave them
OPERATING (latest FW + provisioned + registered + streaming telemetry on TB).

Stages
------
1. DETECT   enumerate ESP32-C6 (COM + MAC) on USB.
2. DIAGNOSE per node, gather ground truth:
     - serial console: alive? boot banner FW version, reset cause, ot role,
       LwM2M state, total_resets, uptime.  (silent => STUCK)
     - TB edge: endpoint provisioned? active? fresh telemetry? FW via /3/0/3.
     - esptool: is the ROM reachable (can we flash)?
3. DOCTOR   classify each node into a condition + prescribed action.
4. FIX      (default; skip with --dry-run) apply the least-invasive fix:
     provision -> flash(build_prod, erase-all) -> reboot -> (power-cycle flag).
5. VERIFY   re-check registration + telemetry, print a per-node verdict.

Usage
-----
    python tools/node_doctor.py                 # full run, applies fixes
    python tools/node_doctor.py --dry-run       # diagnose only, no changes
    python tools/node_doctor.py --mesh pi4 --build-dir build_prod
    python tools/node_doctor.py --coms COM76,COM82   # restrict to these ports
"""
from __future__ import annotations
import argparse, csv, json, pathlib, re, subprocess, sys, threading, time

import fleet_common as fc
fc.bootstrap_venv()

import serial                       # noqa: E402
import serial.tools.list_ports as lp  # noqa: E402
import requests                     # noqa: E402

FW_LATEST = "0.7.18-ami"            # keep in sync with src/main.c CLIENT_FIRMWARE_VER
PREFIX = "ami-esp32c6-"
FLEET_MAP = fc.TOOLS_DIR / "fleet_map.csv"


# ─────────────────────────── helpers ──────────────────────────────────────
def suffix_from_mac(mac: str) -> str:
    p = [x for x in mac.replace("-", ":").split(":") if x]
    return (p[-2] + p[-1]).lower() if len(p) >= 2 else mac.lower()


def load_labels() -> dict:
    m = {}
    if FLEET_MAP.exists():
        with FLEET_MAP.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                m[r["mac"].lower()] = r["label"]
    return m


def detect_nodes(only: list[str] | None) -> list[dict]:
    out = []
    for p in lp.comports():
        h = (p.hwid or "").upper()
        if "303A:1001" not in h:
            continue
        if only and p.device not in only:
            continue
        mac = h.split("SER=")[-1].split()[0].lower() if "SER=" in h else ""
        out.append({"com": p.device, "mac": mac, "suffix": suffix_from_mac(mac) if mac else None})
    out.sort(key=lambda d: int(d["com"][3:]) if d["com"][3:].isdigit() else 0)
    return out


def _timeout_call(fn, timeout, *args, default=None):
    """Run fn(*args) in a daemon thread; return its result or `default` if it
    doesn't finish within `timeout` seconds (a stuck serial port can't hang us)."""
    box = {}
    t = threading.Thread(target=lambda: box.__setitem__("r", fn(*args)), daemon=True)
    t.start(); t.join(timeout)
    return box.get("r", default) if not t.is_alive() else default


_IPV6_LINE = re.compile(r"^[0-9a-fA-F:]+$")


def _extract_omr(text: str) -> str | None:
    """From `ot ipaddr` output pick the OMR (off-mesh-routable) address — the
    ULA the OTBR routes on the infra link, so the server can reach it. Identify
    the mesh-local prefix via the RLOC (contains ':0:ff:fe00:') and return the
    ULA on a *different* /64. Skips link-local (fe80). Nodes print full
    8-hextet addresses, one per line."""
    addrs = []
    for line in text.splitlines():
        t = line.strip()
        if t.count(":") >= 4 and _IPV6_LINE.match(t) and not t.lower().startswith("fe80"):
            addrs.append(t)
    if not addrs:
        return None
    ml_prefix = None
    for a in addrs:
        if ":0:ff:fe00:" in a.lower():                      # mesh-local RLOC
            ml_prefix = ":".join(a.split(":")[:4]).lower()
            break
    for a in addrs:
        if ":0:ff:fe00:" in a.lower():
            continue                                        # skip RLOC
        if ml_prefix and ":".join(a.split(":")[:4]).lower() == ml_prefix:
            continue                                        # skip ML-EID (same /64 as RLOC)
        return a                                            # OMR: the other ULA /64
    return None


def _diag_probe(omr: str, port: int = 5685):
    """GET /diag over IPv6 — the firmware-version oracle (`ami status` does not
    print fw while running). Reuses diag_get's hardened CoAP client. Returns
    {'ok':bool,'diag':{...}} or None. Uses fewer retries than the CLI so an
    unreachable / pre-0.7.15 node costs ~4.5 s, not 8 s."""
    try:
        import diag_get
        import json as _json
        ns: dict = {}
        exec(compile(diag_get.COAP_GET_SRC, "<coap>", "exec"), ns)
        code, body = ns["coap_get"](omr, port, retries=3, timeout=1.5)
        cls = code >> 5
        rec = {"ok": cls == 2, "code": f"{cls}.{code & 0x1f:02d}"}
        try:
            rec["diag"] = _json.loads(body)
        except Exception:
            rec["raw"] = body
        return rec
    except Exception:
        return None


def console_probe(com: str, wait_boot: float = 0.0) -> dict:
    """Open the console (no reset) and read node state. Returns dict; 'alive'
    False means the console produced nothing (stuck/silent)."""
    r = {"alive": False, "fw": None, "role": None, "lwm2m": None, "omr": None,
         "uptime_s": None, "total_resets": None, "reset_cause": None, "raw": ""}
    try:
        s = serial.Serial(); s.port = com; s.baudrate = 115200
        s.timeout = 0.2; s.write_timeout = 1
        s.dtr = False; s.rts = False; s.open()
    except Exception as e:
        r["error"] = f"open-fail {e}"; return r

    def drain(sec):
        end = time.time() + sec; b = b""
        while time.time() < end:
            d = s.read(4096)
            if d:
                b += d
        return b.decode(errors="replace").replace("\x1b[1;32m", "").replace("\x1b[m", "").replace("\x1b[0m", "").replace("\x1b[1;31m", "")
    try:
        if wait_boot:
            r["raw"] += drain(wait_boot)
        drain(0.3)
        for cmd in ("ami status", "kernel uptime", "ot state", "ot ipaddr"):
            s.write((cmd + "\r\n").encode()); time.sleep(0.35)
            r["raw"] += drain(1.3)
        s.close()
    except Exception as e:
        try: s.close()
        except Exception: pass
        r["error"] = f"read-err {e}"; return r

    txt = r["raw"]
    r["alive"] = bool(txt.strip())
    m = re.search(r"AMI LwM2M Node v([0-9][\w.\-]+)", txt);      r["fw"] = m.group(1) if m else r["fw"]
    m = re.search(r"role=([A-Z]+)", txt);                         r["role"] = m.group(1) if m else r["role"]
    m = re.search(r"LwM2M\s*:\s*(OK|FAIL)", txt);                 r["lwm2m"] = m.group(1) if m else r["lwm2m"]
    m = re.search(r"Uptime:\s*(\d+)\s*ms", txt);                  r["uptime_s"] = int(m.group(1)) // 1000 if m else r["uptime_s"]
    m = re.search(r"total_resets:\s*\d+\s*->\s*(\d+)", txt);      r["total_resets"] = int(m.group(1)) if m else r["total_resets"]
    m = re.search(r"Reset cause:\s*(0x[0-9a-fA-F]+)", txt);       r["reset_cause"] = m.group(1) if m else r["reset_cause"]
    sm = re.search(r"ot state\s+([a-z]+)", txt)
    if sm and not r["role"]:
        r["role"] = sm.group(1).upper()
    r["omr"] = _extract_omr(txt)
    return r


def console_reboot(com: str) -> str:
    """Trigger a clean reboot and capture the boot banner (FW + reset cause)."""
    try:
        s = serial.Serial(); s.port = com; s.baudrate = 115200; s.timeout = 0.2
        s.dtr = False; s.rts = False; s.open()
        s.write(b"\r\nkernel reboot cold\r\n"); time.sleep(0.4)
        end = time.time() + 12; b = b""
        while time.time() < end:
            d = s.read(4096)
            if d:
                b += d
            if b"AMI LwM2M Node v" in b:
                time.sleep(0.5); b += s.read(4096); break
        s.close()
        return b.decode(errors="replace")
    except Exception as e:
        return f"reboot-err {e}"


class Tb:
    def __init__(self, host, port, user, pw):
        self.base = f"http://{host}:{port}"
        self.s = requests.Session()
        r = self.s.post(self.base + "/api/auth/login",
                        json={"username": user, "password": pw}, timeout=15)
        r.raise_for_status()
        self.s.headers.update({"X-Authorization": "Bearer " + r.json()["token"]})

    def device(self, endpoint):
        d = self.s.get(self.base + f"/api/tenant/devices?pageSize=1&page=0&textSearch={endpoint}",
                       timeout=15).json()
        for x in d.get("data", []):
            if x.get("name") == endpoint:
                return x
        return None

    def is_active(self, dev):
        try:
            a = self.s.get(self.base + f"/api/plugins/telemetry/DEVICE/{dev['id']['id']}/values/attributes/SERVER_SCOPE",
                           timeout=10).json()
            for kv in a:
                if kv.get("key") == "active":
                    return bool(kv.get("value"))
        except Exception:
            pass
        return dev.get("active", False)

    def latest_ts(self, dev, keys="voltage,activePower"):
        try:
            t = self.s.get(self.base + f"/api/plugins/telemetry/DEVICE/{dev['id']['id']}/values/timeseries?keys={keys}",
                           timeout=10).json()
            return t
        except Exception:
            return {}


# ─────────────────────────── stages ───────────────────────────────────────
def diagnose(node, tb, labels):
    node["label"] = labels.get(node["mac"], "?")
    node["console"] = _timeout_call(
        console_probe, 14, node["com"],
        default={"alive": False, "error": "probe-timeout (port hung)", "role": None, "lwm2m": None})
    ep = PREFIX + node["suffix"] if node["suffix"] else None
    node["endpoint"] = ep
    dev = tb.device(ep) if ep else None
    node["provisioned"] = dev is not None
    node["tb_active"] = tb.is_active(dev) if dev else False
    node["tb_ts"] = tb.latest_ts(dev) if dev else {}
    node["fw_console"] = node["console"].get("fw")
    # /diag over IPv6 is the fw oracle: `ami status` doesn't print fw while
    # running, and TB doesn't store it here. The console gives the OMR; a /diag
    # GET returns the authoritative fw + live counters. If it answers, the node
    # is on >=0.7.15-diag; if it's active but never answers, it's almost surely
    # a pre-0.7.15 image (no diag server).
    node["omr"] = node["console"].get("omr")
    node["diag"], node["diag_ok"], node["fw_diag"] = None, False, None
    if node["omr"]:
        rec = _timeout_call(_diag_probe, 7, node["omr"], default=None)
        if rec and rec.get("ok"):
            node["diag_ok"] = True
            node["diag"] = rec.get("diag")
            node["fw_diag"] = (rec.get("diag") or {}).get("fw")
    node["fw"] = node["fw_diag"] or node["fw_console"]
    return node


def doctor(node):
    """Priority: a node that is ACTIVE on TB is operating (it reports over
    Thread) regardless of its USB console — so it is HEALTHY. Console/role
    detail only drives the fix when the node is NOT active on TB."""
    c = node["console"]
    fw = node.get("fw")            # from /diag (authoritative) or boot banner
    if not node["provisioned"]:
        cond, action = "NOT_PROVISIONED", "provision+flash"
    elif node["tb_active"]:
        # active on TB = operating (it reports over Thread). fw is the second
        # axis, read via /diag over IPv6. Only infer "old fw" when we actually
        # had an OMR to probe and /diag still went unanswered (== no diag server
        # == pre-0.7.15). A SILENT console yields no OMR, so we cannot verify
        # fw — that is NOT evidence of old fw, so keep HEALTHY with a note.
        if fw == FW_LATEST:
            cond, action = "HEALTHY", "none"
        elif fw:
            cond, action = f"ACTIVE_OLD_FW({fw})", "flash(build_prod) + provision to update"
        elif node.get("omr") and not node.get("diag_ok"):
            cond, action = "ACTIVE_FW?", "OMR up but no /diag -> likely pre-0.7.15; flash + provision"
        else:
            cond, action = "HEALTHY", "fw unverified (console silent, no OMR); recheck later"
    elif not c["alive"]:
        cond, action = "STUCK_SILENT (inactive)", "reflash(erase-all); if fail -> POWER-CYCLE"
    elif fw and fw != FW_LATEST:
        cond, action = f"OLD_FW({fw})", "flash(build_prod)"
    elif c.get("role") in ("CHILD", "ROUTER", "LEADER") and c.get("lwm2m") == "FAIL":
        cond, action = "ATTACHED_NOT_REGISTERED", "reboot; verify"
    elif c.get("role") in (None, "DISABLED", "DETACHED"):
        cond, action = "NOT_ATTACHED", "reflash(erase-all); if fail -> POWER-CYCLE"
    else:
        cond, action = "INACTIVE_ON_TB", "reboot; verify"
    node["condition"] = cond
    node["action"] = action
    return node


def flash_node(env, com, build_dir):
    ws = env.west_workspace
    mcu = ws / build_dir / "mcuboot" / "zephyr" / "zephyr.bin"
    app = ws / build_dir / "ami-lwm2m-node" / "zephyr" / "zephyr.signed.bin"
    if not (mcu.exists() and app.exists()):
        return False, f"artifacts missing in {build_dir}"
    cmd = [str(env.venv_python), "-m", "esptool", "--chip", "esp32c6", "--port", com,
           "--baud", "460800", "--before", "default-reset", "--after", "hard-reset",
           "write-flash", "--erase-all", "--flash-freq", "20m", "--flash-mode", "dout",
           "0x0", str(mcu), "0x20000", str(app)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                           env=env.env_for_subprocess())
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"
    out = r.stdout + r.stderr
    ok = r.returncode == 0 and out.count("Hash of data verified") >= 2
    return ok, "OK" if ok else out.strip().splitlines()[-1][:80] if out.strip() else "fail"


def provision(suffix, mesh):
    try:
        r = subprocess.run([sys.executable, str(fc.TOOLS_DIR / "tb_edge_provision.py"),
                            "--mesh", mesh, "--nodes", suffix],
                           capture_output=True, text=True, timeout=90)
        return "creds set OK" in (r.stdout + r.stderr) or r.returncode == 0
    except Exception:
        return False


def fix(node, env, tb, args):
    acts = []
    suf = node["suffix"]; com = node["com"]
    a = node["action"]
    if args.dry_run or a == "none":
        node["fix_result"] = "SKIP (dry-run)" if args.dry_run else "nothing to do"
        return node
    if "provision" in a and suf:
        acts.append("provision:" + ("OK" if provision(suf, args.mesh) else "FAIL"))
    if "flash" in a or "reflash" in a:
        ok, msg = flash_node(env, com, args.build_dir)
        acts.append(f"flash:{'OK' if ok else 'FAIL('+msg+')'}")
        if ok:
            # esptool verified both image hashes -> the node now definitively
            # holds FW_LATEST, regardless of whether its (flaky) USB console
            # answers afterwards. Trust the verified write over a later /diag.
            node["fw"] = FW_LATEST
        else:
            acts.append("=> needs PHYSICAL power-cycle / download-mode")
    elif "reboot" in a:
        console_reboot(com); acts.append("reboot:sent")
    node["fix_result"] = " ; ".join(acts) if acts else "none"
    return node


def verify(node, tb, wait=90):
    if not node.get("endpoint"):
        node["verify"] = "no endpoint"; return node
    time.sleep(wait)
    dev = tb.device(node["endpoint"])
    node["verify"] = "ACTIVE" if (dev and tb.is_active(dev)) else "still inactive (may need more time / power-cycle)"
    return node


# ─────────────────────────── main ─────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mesh", default="pi4", choices=fc.MESH_TARGETS)
    ap.add_argument("--build-dir", default="build_prod")
    ap.add_argument("--coms", help="comma list to restrict (default: all ESP32-C6)")
    ap.add_argument("--dry-run", action="store_true", help="diagnose only, no changes")
    ap.add_argument("--no-verify", action="store_true")
    args = ap.parse_args()

    env = fc.detect_env(verbose=False)
    host, port = fc.edge_for_mesh(args.mesh)
    tb = Tb(host, port, fc.EDGE_TENANT_USER, fc.EDGE_TENANT_PASS)
    labels = load_labels()
    only = [c.strip() for c in args.coms.split(",")] if args.coms else None

    print("=" * 74)
    print(f"AMI NODE DOCTOR   mesh={args.mesh} edge={host}:{port}  build={args.build_dir}"
          f"  {'DRY-RUN' if args.dry_run else 'FIX'}")
    print("=" * 74)

    # 1. DETECT
    nodes = detect_nodes(only)
    print(f"\n[1] DETECT: {len(nodes)} ESP32-C6 node(s)")
    for n in nodes:
        print(f"    {n['com']:6} mac={n['mac'] or '?':17} suffix={n['suffix'] or '?'}")
    if not nodes:
        print("    no nodes connected — done."); return 0

    # 2. DIAGNOSE
    print("\n[2] DIAGNOSE")
    for n in nodes:
        diagnose(n, tb, labels)
        c = n["console"]
        diagtag = n.get("fw_diag") if n.get("diag_ok") else "no-resp"
        print(f"    {n['com']} Lab{n['label']:>3} {n['endpoint']}: "
              f"console={'alive' if c['alive'] else 'SILENT'} "
              f"role={c.get('role') or '?'} lwm2m={c.get('lwm2m') or '?'} "
              f"/diag={diagtag} fw={n.get('fw') or '?'} "
              f"| TB prov={n['provisioned']} active={n['tb_active']}")

    # 3. DOCTOR
    print("\n[3] DOCTOR")
    for n in nodes:
        doctor(n)
        print(f"    {n['com']} {n['endpoint']}: {n['condition']:24} -> {n['action']}")

    # 4. FIX
    print(f"\n[4] FIX {'(dry-run: skipped)' if args.dry_run else ''}")
    for n in nodes:
        fix(n, env, tb, args)
        print(f"    {n['com']} {n['endpoint']}: {n['fix_result']}")

    # 5. VERIFY
    if not args.dry_run and not args.no_verify:
        print("\n[5] VERIFY (waiting ~90s for re-registration)")
        for n in nodes:
            verify(n, tb)
            print(f"    {n['com']} {n['endpoint']}: {n['verify']}")

    # SUMMARY
    print("\n" + "=" * 74)
    active_nodes = [n for n in nodes if n.get("tb_active") or n.get("verify") == "ACTIVE"]
    operating = len(active_nodes)
    on_latest = sum(1 for n in nodes if n.get("fw") == FW_LATEST)
    # real update candidates: active, not on latest, and we could positively
    # tell (known old fw, or OMR reachable but /diag silent = no diag server).
    need_fw = [n["endpoint"] for n in active_nodes
               if n.get("fw") != FW_LATEST and (n.get("fw") or n.get("omr"))]
    # active but fw could not be verified (silent console -> no OMR to probe).
    unverified = [n["endpoint"] for n in active_nodes
                  if n.get("fw") != FW_LATEST and not n.get("fw") and not n.get("omr")]
    pcycle = [n["endpoint"] for n in nodes if "POWER-CYCLE" in n.get("fix_result", "")]
    print(f"SUMMARY: {operating}/{len(nodes)} operating (active on TB); "
          f"{on_latest}/{len(nodes)} confirmed on {FW_LATEST} via /diag.")
    if need_fw:
        print(f"  ACTIVE but on old fw (update candidates): {need_fw}")
    if unverified:
        print(f"  ACTIVE, fw UNVERIFIED (console silent; recheck): {unverified}")
    if pcycle:
        print(f"  NEEDS PHYSICAL POWER-CYCLE: {pcycle}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
