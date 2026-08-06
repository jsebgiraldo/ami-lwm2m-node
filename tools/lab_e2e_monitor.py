#!/usr/bin/env python3
"""lab_e2e_monitor.py — benchtop end-to-end monitor for the AMI LAB test network.

WHY THIS EXISTS
  This is the LAB counterpart of tools/node_monitor.py. node_monitor.py assumes the
  production stack (ThingsBoard Edge REST + a pi4 OTBR reachable over SSH). The
  benchtop lab has NEITHER: there is no TB Edge and no SSH OTBR. Instead:
    * the OTBR is a LOCAL OpenThread Border Router (a docker container in WSL2), so
      mesh state comes from `docker exec <container> ot-ctl <cmd>`, and
    * node state comes DIRECTLY from each node's on-board CoAP diag server
      (src/coap_diag.c, [::]:5685 /diag + /ami) over its Thread OMR IPv6 address, and
    * power comes from a FNIRSI FNB-C2 USB power meter whose samples a separate logger
      writes to tools/fnb_latest.json (this process never opens the FNB device itself).

  Every --interval seconds it samples the WHOLE chain (meter -> node -> mesh -> OTBR
  + power rail), writes the canonical snapshot to tools/lab_e2e_snapshot.json (the exact
  shape the lab dashboard artifact reads), and appends a per-node row to a CSV history.

DESIGN RULES (match the rest of tools/)
  * stdlib-first; the only local import is tools/diag_get.py (reused verbatim for CoAP).
  * robust to a dead source: any failing source -> that field becomes null + a logged
    line, and the loop KEEPS GOING. Never crash the loop on one bad read.
  * honest data: where the firmware/counters cannot give a value we emit null and say
    so in a comment — we never fabricate a number.

CoAP reuse
  The raw CoAP CON GET + parse lives in tools/diag_get.py (COAP_GET_SRC / _run_local).
  We import and call it rather than re-implementing CoAP. In --probe-mode local the GET
  runs from THIS host (needs an IPv6 route onto the OMR prefix). In --probe-mode otbr the
  SAME self-contained prober is piped into the OTBR container (`python3 -`) where a mesh
  route always exists.

OSI-layer byte accounting ("cuantos datos manda en su modelo OSI")
  Per node we fill what the stack actually exposes and null the rest (see build_osi()):
    L2 mac tx/rx  <- `ot-ctl counters mac` Tx/RxTotal. IMPORTANT: PAN-WIDE (the OTBR
                     radio's own frame counters), NOT per-node. In a 1-node bench that is
                     a fair proxy; with >1 node it is shared. Flagged in the output comment.
    L3 ipv6 bytes <- NOT exposed by /diag today -> null (gap).
    L4 udp/coap   <- NOT exposed by /diag today -> null (gap).
    L7 lwm2m tx   <- Object 33000 RID 38 (lwm2m_tx_bytes), EXACT. /diag does not carry it
                     in the current firmware and the lab has no TB to read /33000/0/38, so
                     it is null unless a newer /diag adds a key (we look for it). When it is
                     present we derive bytes_per_min from its delta across cycles.
    app_msgs      <- monitor-observed: count of application-layer replies this node has sent
                     us this run (each good /diag or /ami = one app message). The firmware
                     exposes no per-node app-message counter, so this is our own tally.

Usage
  python tools/lab_e2e_monitor.py --node ami-esp32c6-3bb0:fdxx:...:3bb0 --interval 30
  python tools/lab_e2e_monitor.py --otbr-mode docker --otbr-container otbr --duration 7200
  (nodes may also be listed in tools/lab_nodes.json, maintained by the orchestrator)
"""
from __future__ import annotations
import argparse
import collections
import datetime
import json
import os
import pathlib
import subprocess
import sys
import time

# Reuse the CoAP client shipped in this same directory (do NOT reimplement CoAP).
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import diag_get  # noqa: E402  (local module: COAP_GET_SRC + _run_local)

# ── defaults / tunables ──────────────────────────────────────────────────────
DEFAULT_SNAPSHOT = HERE / "lab_e2e_snapshot.json"
DEFAULT_CSV = HERE / "lab_e2e_history.csv"
DEFAULT_NODES_JSON = HERE / "lab_nodes.json"
DEFAULT_FNB_JSON = HERE / "fnb_latest.json"
DIAG_PORT = diag_get.DIAG_PORT          # 5685
AVAIL_WINDOW = 720                      # rolling window (samples) for availability_pct
HISTORY_MAX = 720                       # snapshot["history"] ring-buffer length
FNB_STALE_S = 10                        # fnb_latest.json older than this -> source degraded

# psu_risk thresholds on the observed PEAK bus current (mA).
#   The lab's chronic failure mode (see MEMORY: "LwM2M burst = USB cliff") is a USB-HOST
#   OVERCURRENT shutdown, not an on-board brownout: an LwM2M/REGISTER TX burst drives a
#   short current spike that trips the host port. A USB-2.0 host guarantees only 500 mA, so
#   a peak approaching that ceiling is the danger signal — hence thresholds on i_peak_ma,
#   not on the mean. Tune to your host/hub; documented here so the dashboard and this loop
#   agree on what OK/WARN/BLOCK mean.
PSU_WARN_MA = 350.0                     # sustained/peak headroom shrinking
PSU_BLOCK_MA = 480.0                    # within ~4% of the 500 mA USB-2.0 host limit


# ── small logging helper (stderr, never throws) ──────────────────────────────
def log(msg: str) -> None:
    print(f"[lab_e2e {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


# ── subprocess exec (robust: never raises, returns (stdout, stderr, rc)) ──────
def _exec(argv, timeout, stdin_text=None):
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, input=stdin_text)
        return p.stdout or "", p.stderr or "", p.returncode
    except FileNotFoundError as e:
        return "", f"{type(e).__name__}: {e}", 127
    except subprocess.TimeoutExpired:
        return "", "timeout", 124
    except Exception as e:                                # pragma: no cover (defensive)
        return "", f"{type(e).__name__}: {e}", 1


# ─────────────────────────────────────────────────────────────────────────────
# OTBR access — pluggable transport to the LOCAL border router's `ot-ctl`.
#   docker (default): docker exec -i <container> ot-ctl <cmd>
#   wsl             : wsl docker exec -i <container> ot-ctl <cmd>   (docker lives in WSL)
#   ssh             : ssh user@host docker exec -i <container> ot-ctl <cmd>  (remote lab OTBR)
# The SAME prefix runs the CoAP prober inside the container in --probe-mode otbr, where a
# mesh route to the node's OMR address always exists.
# ─────────────────────────────────────────────────────────────────────────────
class OTBR:
    def __init__(self, mode, container, host, user, timeout=20):
        self.mode = mode
        self.container = container
        self.host = host
        self.user = user
        self.timeout = timeout

    def _prefix(self):
        if self.mode == "native":
            return []   # native systemd otbr-agent: run `ot-ctl` directly (we ARE on the OTBR host)
        exec_ = ["docker", "exec", "-i", self.container]
        if self.mode == "docker":
            return exec_
        if self.mode == "wsl":
            return ["wsl"] + exec_
        if self.mode == "ssh":
            target = f"{self.user}@{self.host}" if self.user else self.host
            return ["ssh", target] + exec_
        raise ValueError(f"unknown otbr mode {self.mode!r}")

    def ot(self, *cmd):
        """Run `ot-ctl <cmd...>` and return its stdout, or "" on failure.

        We key success on the process exit code: a working `docker exec ... ot-ctl`
        exits 0 and prints the reply to stdout, whereas a missing container / dead
        OTBR exits non-zero and writes a daemon error to stderr. Returning "" on
        failure makes every downstream parser degrade to null (role=None -> up=False)
        instead of mistaking the error text for real ot-ctl output."""
        out, err, rc = _exec(self._prefix() + ["ot-ctl", *cmd], self.timeout)
        if rc != 0:
            log(f"otbr ot-ctl {' '.join(cmd)} failed rc={rc}: {(err or out).strip()[:160]!r}")
            return ""
        return out

    def probe(self, addrs, port, path):
        """Ship diag_get's self-contained CoAP prober into the container over stdin
        (`python3 -`) and return its stdout (a JSON list, one record per address)."""
        argv = self._prefix() + ["python3", "-", str(port), path, *addrs]
        out, err, _rc = _exec(argv, self.timeout, stdin_text=diag_get.COAP_GET_SRC)
        if err.strip():
            log(f"otbr-probe stderr: {err.strip()[:200]}")
        return out


# ── ot-ctl output parsing (mirrors tools/net_soak.py) ─────────────────────────
def _first_value(text):
    """First meaningful line of an ot-ctl reply (skip blanks and the trailing 'Done')."""
    for line in text.splitlines():
        s = line.strip()
        if s and s.lower() != "done":
            return s
    return None


def _table_count(text):
    """Rows in an ot-ctl table = lines starting with '|' minus the header (>=0)."""
    return max(sum(l.strip().startswith("|") for l in text.splitlines()) - 1, 0)


def _mac_counter(text, key):
    """Pull an integer `Key: <n>` value out of `ot-ctl counters mac` (else None)."""
    for l in text.splitlines():
        s = l.strip()
        if s.startswith(key + ":"):
            v = s.split(":", 1)[1].strip()
            return int(v) if v.lstrip("-").isdigit() else None
    return None


def read_otbr(otbr):
    """One OTBR mesh snapshot -> the canonical snapshot['otbr'] block.
    Any missing datum degrades to null; a totally-down OTBR yields up=False."""
    o = {"up": False, "role": None, "channel": None, "panid": None,
         "routers": 0, "children": 0, "eid_resolved": 0, "eid_retry": 0,
         "tx_err_cca": None, "rx_total": None, "tx_total": None}

    role = _first_value(otbr.ot("state"))
    if role:
        role = role.lower()
        if role in ("disabled", "detached", "child", "router", "leader"):
            o["role"] = role
            o["up"] = role not in ("disabled", "detached")
        else:
            log(f"otbr unexpected state reply: {role!r}")

    ch = _first_value(otbr.ot("channel"))
    if ch and ch.isdigit():
        o["channel"] = int(ch)

    pan = _first_value(otbr.ot("panid"))
    if pan:
        o["panid"] = ("0x" + pan[2:].upper()) if pan.lower().startswith("0x") else pan

    o["routers"] = _table_count(otbr.ot("router", "table"))
    o["children"] = _table_count(otbr.ot("child", "table"))

    # eidcache: a "cache"/resolved entry has no "retry"; a pending one says "retry"
    # (same heuristic tools/net_soak.py uses).
    ec = otbr.ot("eidcache").splitlines()
    o["eid_resolved"] = sum(("cache" in l and "retry" not in l) for l in ec)
    o["eid_retry"] = sum(("retry" in l) for l in ec)

    mac = otbr.ot("counters", "mac")
    o["tx_err_cca"] = _mac_counter(mac, "TxErrCca")
    o["rx_total"] = _mac_counter(mac, "RxTotal")
    o["tx_total"] = _mac_counter(mac, "TxTotal")
    return o


# ── node probing (CoAP /diag + /ami, reusing diag_get) ────────────────────────
def _first_rec(records, addr):
    """Pick this address's record out of diag_get's result list (defensive)."""
    if not records:
        return {"addr": addr, "ok": False, "error": "no response record"}
    for r in records:
        if r.get("addr") == addr:
            return r
    return records[0]


def probe_path(addr, port, path, probe_mode, otbr):
    """Return one diag_get-style record: {addr, ok, data|raw|error, ...}."""
    if probe_mode == "local":
        # diag_get._run_local compiles COAP_GET_SRC and runs the CON GET in-process.
        try:
            return _first_rec(diag_get._run_local([addr], port, path), addr)
        except Exception as e:
            return {"addr": addr, "ok": False, "error": f"{type(e).__name__}: {e}"}
    # probe_mode == "otbr": run the same prober inside the border-router container.
    out = otbr.probe([addr], port, path)
    try:
        return _first_rec(json.loads(out), addr)
    except Exception:
        return {"addr": addr, "ok": False, "error": f"otbr-probe parse (raw={out[:120]!r})"}


def map_ami(data):
    """Map the node's /ami phase-R/S/T OBIS to the snapshot's single-value ami block.
    voltage/current are phase R; power is TOTAL active power (kW); all phases + energy
    remain in the raw /ami payload if a caller wants them. null when no valid reading."""
    if not data or not data.get("valid"):
        return {"voltage": None, "current": None, "power": None, "freq": None}
    return {
        "voltage": data.get("vr"),      # phase-R RMS voltage (V)
        "current": data.get("ir"),      # phase-R RMS current (A)
        "power": data.get("ptot"),      # total 3-phase active power (kW)
        "freq": data.get("freq"),       # line frequency (Hz)
    }


# ── per-node rolling state (availability, last-seen, byte deltas) ─────────────
class NodeState:
    def __init__(self, name, omr):
        self.name = name
        self.omr = omr
        self.window = collections.deque(maxlen=AVAIL_WINDOW)  # 1=diag ok, 0=diag fail
        self.last_ok_mono = None       # monotonic time of last successful /diag
        self.app_msgs = 0              # monitor-observed app replies (diag+ami) this run
        self.prev_l7 = None            # last lwm2m_tx_bytes value (if firmware exposes it)
        self.prev_l7_mono = None

    def record_probe(self, diag_ok, ami_ok, mono):
        self.window.append(1 if diag_ok else 0)
        if diag_ok:
            self.last_ok_mono = mono
            self.app_msgs += 1
        if ami_ok:
            self.app_msgs += 1

    @property
    def availability_pct(self):
        if not self.window:
            return None
        return round(100.0 * sum(self.window) / len(self.window), 2)

    def last_seen_s(self, mono):
        if self.last_ok_mono is None:
            return None
        return round(mono - self.last_ok_mono, 1)

    def bytes_per_min(self, l7_bytes, mono):
        """Derive L7 bytes/min from the delta of lwm2m_tx_bytes across cycles.
        Returns None until the firmware exposes the counter and we have two readings."""
        bpm = None
        if l7_bytes is not None:
            if self.prev_l7 is not None and self.prev_l7_mono is not None and mono > self.prev_l7_mono:
                delta = l7_bytes - self.prev_l7
                if delta >= 0:                            # ignore counter resets (reboot)
                    bpm = round(delta / ((mono - self.prev_l7_mono) / 60.0), 2)
            self.prev_l7 = l7_bytes
            self.prev_l7_mono = mono
        return bpm


def build_osi(diag_data, otbr_state, l7_bytes, state, bpm):
    """Assemble the per-node OSI byte-accounting block. See module docstring for the
    per-layer provenance and which fields are genuine gaps (null)."""
    return {
        # L2: there is NO per-node 802.15.4 byte/frame counter. The radio's Rx/TxTotal
        # are PAN-WIDE and are surfaced in the OTBR block (otbr.rx_total / otbr.tx_total);
        # attributing them per-node would falsely show identical traffic on every node.
        "l2_mac_tx": None,
        "l2_mac_rx": None,
        # L3/L4: not exposed by the current /diag firmware -> honest gap.
        "l3_ipv6_bytes": None,
        "l4_udp_coap_bytes": None,
        # L7: exact LwM2M TX bytes (Obj 33000 RID 38) IF /diag carries it; else gap.
        "l7_lwm2m_tx_bytes": l7_bytes,
        # app-layer messages observed by this monitor (see NodeState.record_probe).
        "app_msgs": state.app_msgs,
        # derived: only meaningful once a real byte counter (L7) is present.
        "bytes_per_min": bpm,
        # avg packet size needs a per-node packet count the firmware does not expose -> gap.
        "avg_pkt_bytes": None,
    }


def read_node(state, port, probe_mode, otbr, otbr_state, mono):
    """Probe one node (CoAP /diag + /ami) and build its canonical snapshot entry."""
    drec = probe_path(state.omr, port, "diag", probe_mode, otbr)
    arec = probe_path(state.omr, port, "ami", probe_mode, otbr)

    diag_ok = bool(drec.get("ok") and "data" in drec)
    ami_ok = bool(arec.get("ok") and "data" in arec)
    ddata = drec.get("data") if diag_ok else None
    adata = arec.get("data") if ami_ok else None
    # A node can answer CoAP with valid JSON that is NOT an object (a scalar/list);
    # .get() on that below would raise and kill the whole monitor loop. Treat a
    # non-dict body as "no usable data" (node effectively unreachable this cycle).
    if not isinstance(ddata, dict):
        ddata, diag_ok = None, False
    if not isinstance(adata, dict):
        adata, ami_ok = None, False

    state.record_probe(diag_ok, ami_ok, mono)

    if not diag_ok:
        log(f"node {state.name} /diag unreachable: {drec.get('error') or drec.get('code')}")

    # L7 exact bytes: current /diag does not carry it, but forward-compat: accept any of
    # these keys if a future firmware adds lwm2m_tx_bytes (Obj 33000 RID 38) to /diag.
    l7 = None
    if ddata:
        for k in ("lwm2m_tx_bytes", "tx_bytes", "l7_tx_bytes"):
            if isinstance(ddata.get(k), (int, float)):
                l7 = ddata[k]
                break
    bpm = state.bytes_per_min(l7, mono)

    node = {
        "name": state.name,
        "omr": state.omr,
        "reachable": diag_ok,
        "fw": ddata.get("fw") if ddata else None,
        "role": ddata.get("role") if ddata else None,
        "uptime_s": ddata.get("uptime_s") if ddata else None,
        "resets": ddata.get("resets") if ddata else None,
        "reg_ok": ddata.get("reg_ok") if ddata else None,
        "recover": ddata.get("recover") if ddata else None,
        "wdog": ddata.get("wdog") if ddata else None,
        "rloc16": ddata.get("rloc16") if ddata else None,
        "availability_pct": state.availability_pct,
        "last_seen_s": state.last_seen_s(mono),
        "osi": build_osi(ddata, otbr_state, l7, state, bpm),
        "ami": map_ami(adata),
    }
    return node


def safe_read_node(state, port, probe_mode, otbr, otbr_state, mono):
    """read_node with a per-node guard: any unexpected error degrades THIS node to a
    minimal unreachable entry (and is logged) instead of aborting the whole loop."""
    try:
        return read_node(state, port, probe_mode, otbr, otbr_state, mono)
    except Exception as e:
        log(f"node {state.name} read crashed ({type(e).__name__}: {e}) — degraded to unreachable")
        state.record_probe(False, False, mono)
        return {
            "name": state.name, "omr": state.omr, "reachable": False,
            "fw": None, "role": None, "uptime_s": None, "resets": None,
            "reg_ok": None, "recover": None, "wdog": None, "rloc16": None,
            "availability_pct": state.availability_pct,
            "last_seen_s": state.last_seen_s(mono),
            "osi": build_osi(None, otbr_state, None, state, None),
            "ami": map_ami(None),
        }


# ── power (read the FNB logger's JSON; never open the device here) ────────────
def read_power(fnb_path, stale_s=FNB_STALE_S):
    """Merge tools/fnb_latest.json's power sample. Missing/stale/garbage -> graceful
    degrade: numeric fields null-or-last, source suffixed, psu_risk from i_peak_ma."""
    base = {"source": "FNB-C2", "v_bus": None, "i_ma": None, "p_w": None,
            "mah_cum": None, "wh_cum": None, "i_peak_ma": None, "samples": 0,
            "psu_risk": "UNKNOWN"}
    try:
        st = fnb_path.stat()
    except FileNotFoundError:
        base["source"] = "FNB-C2 (absent)"
        return base
    except Exception as e:
        log(f"power stat failed: {e}")
        base["source"] = "FNB-C2 (error)"
        return base

    age = time.time() - st.st_mtime
    try:
        raw = json.loads(fnb_path.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"power json parse failed: {e}")
        base["source"] = "FNB-C2 (unparseable)"
        return base

    # Accept either a nested {"power": {...}} block or a flat object.
    blk = raw.get("power", raw) if isinstance(raw, dict) else {}
    for k in ("v_bus", "i_ma", "p_w", "mah_cum", "wh_cum", "i_peak_ma", "samples"):
        if isinstance(blk.get(k), (int, float)):
            base[k] = blk[k]
    if isinstance(blk.get("source"), str):
        base["source"] = blk["source"]

    if age > stale_s:
        # A dead FNB logger must NOT render as live power: null the instantaneous
        # fields (keep cumulative mAh/Wh/samples as last-known) and mark risk unknown.
        base["v_bus"] = base["i_ma"] = base["p_w"] = base["i_peak_ma"] = None
        base["psu_risk"] = "UNKNOWN"
        base["source"] = f"{base['source']} (stale {int(age)}s)"
        log(f"power source stale: fnb_latest.json is {int(age)}s old (> {stale_s}s)")
    else:
        base["psu_risk"] = classify_psu(base["i_peak_ma"])
    return base


def classify_psu(i_peak_ma):
    if i_peak_ma is None:
        return "UNKNOWN"
    if i_peak_ma >= PSU_BLOCK_MA:
        return "BLOCK"
    if i_peak_ma >= PSU_WARN_MA:
        return "WARN"
    return "OK"


# ── node discovery ────────────────────────────────────────────────────────────
def parse_node_arg(spec):
    """'NAME:OMR' -> (name, omr). Split on the FIRST ':' only — the OMR IPv6 address is
    full of colons, so the name must not contain one."""
    if ":" not in spec:
        raise ValueError(f"--node must be NAME:OMR (got {spec!r})")
    name, omr = spec.split(":", 1)
    name, omr = name.strip(), omr.strip()
    if not name or not omr:
        raise ValueError(f"--node NAME and OMR must both be non-empty (got {spec!r})")
    return name, omr


def load_nodes_file(path):
    """Read the orchestrator-maintained node list. Accepts:
       {"nodes":[{"name":..,"omr":..}, ..]}  |  [{"name":..,"omr":..}]  |  {"name":"omr"}."""
    out = []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return out
    except Exception as e:
        log(f"nodes file {path.name} unreadable: {e}")
        return out
    items = raw.get("nodes", raw) if isinstance(raw, dict) else raw
    if isinstance(items, dict):                      # {name: omr}
        items = [{"name": k, "omr": v} for k, v in items.items()]
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict) and it.get("name") and it.get("omr"):
                out.append((str(it["name"]), str(it["omr"])))
    return out


def collect_nodes(cli_nodes, nodes_json):
    """Merge --node args and lab_nodes.json (CLI wins on name collision, keeps order)."""
    seen, nodes = set(), []
    for spec in cli_nodes:
        name, omr = parse_node_arg(spec)
        if name not in seen:
            seen.add(name)
            nodes.append((name, omr))
    for name, omr in load_nodes_file(nodes_json):
        if name not in seen:
            seen.add(name)
            nodes.append((name, omr))
    return nodes


# ── CSV history ───────────────────────────────────────────────────────────────
CSV_COLS = [
    "ts_iso", "node", "omr", "reachable", "availability_pct", "last_seen_s",
    "fw", "role", "uptime_s", "resets", "reg_ok", "recover", "wdog", "rloc16",
    "voltage", "current", "power", "freq",
    "l2_mac_tx", "l2_mac_rx", "l7_lwm2m_tx_bytes", "bytes_per_min", "app_msgs",
    "otbr_up", "otbr_role", "otbr_channel", "otbr_panid", "routers", "children",
    "eid_resolved", "eid_retry", "tx_err_cca", "rx_total", "tx_total",
    "pwr_source", "pwr_v_bus", "pwr_i_ma", "pwr_p_w", "pwr_i_peak_ma",
    "pwr_mah_cum", "pwr_wh_cum", "psu_risk",
]


def _cell(v):
    return "" if v is None else str(v)


def ensure_csv(path):
    if not path.exists():
        path.write_text(",".join(CSV_COLS) + "\n", encoding="utf-8")


def append_csv(path, ts, node, otbr_state, power):
    o, p = otbr_state, power
    ami = node["ami"]
    osi = node["osi"]
    row = {
        "ts_iso": ts, "node": node["name"], "omr": node["omr"],
        "reachable": node["reachable"], "availability_pct": node["availability_pct"],
        "last_seen_s": node["last_seen_s"], "fw": node["fw"], "role": node["role"],
        "uptime_s": node["uptime_s"], "resets": node["resets"], "reg_ok": node["reg_ok"],
        "recover": node["recover"], "wdog": node["wdog"], "rloc16": node["rloc16"],
        "voltage": ami["voltage"], "current": ami["current"], "power": ami["power"],
        "freq": ami["freq"], "l2_mac_tx": osi["l2_mac_tx"], "l2_mac_rx": osi["l2_mac_rx"],
        "l7_lwm2m_tx_bytes": osi["l7_lwm2m_tx_bytes"], "bytes_per_min": osi["bytes_per_min"],
        "app_msgs": osi["app_msgs"], "otbr_up": o["up"], "otbr_role": o["role"],
        "otbr_channel": o["channel"], "otbr_panid": o["panid"], "routers": o["routers"],
        "children": o["children"], "eid_resolved": o["eid_resolved"], "eid_retry": o["eid_retry"],
        "tx_err_cca": o["tx_err_cca"], "rx_total": o["rx_total"], "tx_total": o["tx_total"],
        "pwr_source": p["source"], "pwr_v_bus": p["v_bus"], "pwr_i_ma": p["i_ma"],
        "pwr_p_w": p["p_w"], "pwr_i_peak_ma": p["i_peak_ma"], "pwr_mah_cum": p["mah_cum"],
        "pwr_wh_cum": p["wh_cum"], "psu_risk": p["psu_risk"],
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(",".join(_cell(row[c]) for c in CSV_COLS) + "\n")


# ── snapshot write (atomic) + history seeding ─────────────────────────────────
def write_snapshot(path, snap):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(snap, indent=2), encoding="utf-8")
    os.replace(tmp, path)               # atomic on Windows + POSIX


def seed_history(path):
    """Continue the ring buffer across restarts by re-loading the last snapshot's history."""
    hist = collections.deque(maxlen=HISTORY_MAX)
    try:
        prev = json.loads(path.read_text(encoding="utf-8"))
        for pt in prev.get("history", [])[-HISTORY_MAX:]:
            hist.append(pt)
        if hist:
            log(f"seeded history with {len(hist)} prior point(s) from {path.name}")
    except Exception:
        pass                            # no prior snapshot / unreadable -> start fresh
    return hist


# ── one-line per-cycle status ─────────────────────────────────────────────────
def status_line(otbr_state, nodes, power):
    o = otbr_state
    reach = sum(1 for n in nodes if n["reachable"])
    parts = [f"otbr={o['role'] or 'down'} R/C={o['routers']}/{o['children']} "
             f"eid={o['eid_resolved']}/{o['eid_retry']}r cca={o['tx_err_cca']}"]
    for n in nodes:
        parts.append(f"{n['name']} {'OK' if n['reachable'] else 'XX'} "
                     f"up={n['uptime_s']}s avail={n['availability_pct']}% "
                     f"V={n['ami']['voltage']} P={n['ami']['power']}")
    parts.append(f"pwr {power['v_bus']}V {power['i_ma']}mA peak={power['i_peak_ma']}mA "
                 f"risk={power['psu_risk']}")
    return f"reach={reach}/{len(nodes)} | " + " | ".join(parts)


# ── main loop ─────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description="Benchtop e2e monitor for the AMI lab (local docker OTBR + node CoAP "
                    "/diag + FNB power JSON). Writes the canonical lab snapshot + CSV history.")
    ap.add_argument("--interval", type=float, default=30.0,
                    help="seconds between full-chain samples (default 30)")
    ap.add_argument("--duration", type=float, default=0.0,
                    help="total run seconds; <=0 = run until interrupted (default)")
    ap.add_argument("--otbr-mode", choices=("docker", "wsl", "ssh", "native"), default="docker",
                    help="how to reach the local OTBR's ot-ctl (default docker exec; "
                         "'native' = run ot-ctl directly, for a native systemd otbr-agent)")
    ap.add_argument("--otbr-container", default="otbr",
                    help="OTBR docker container name (default 'otbr')")
    ap.add_argument("--otbr-host", default="127.0.0.1",
                    help="host for --otbr-mode ssh (default 127.0.0.1)")
    ap.add_argument("--otbr-user", default="root",
                    help="ssh user for --otbr-mode ssh (default root)")
    ap.add_argument("--probe-mode", choices=("local", "otbr"), default="local",
                    help="run the CoAP /diag GET from THIS host (local, needs an OMR route) "
                         "or inside the OTBR container (otbr, always on-mesh). Default local.")
    ap.add_argument("--node", action="append", default=[], metavar="NAME:OMR",
                    help="node to monitor as NAME:OMR_IPv6 (repeatable)")
    ap.add_argument("--nodes-json", type=pathlib.Path, default=DEFAULT_NODES_JSON,
                    help=f"extra nodes file (default {DEFAULT_NODES_JSON.name})")
    ap.add_argument("--port", type=int, default=DIAG_PORT,
                    help=f"node CoAP diag port (default {DIAG_PORT})")
    ap.add_argument("--fnb-json", type=pathlib.Path, default=DEFAULT_FNB_JSON,
                    help=f"FNB logger output to read power from (default {DEFAULT_FNB_JSON.name})")
    ap.add_argument("--snapshot-out", type=pathlib.Path, default=DEFAULT_SNAPSHOT,
                    help=f"canonical snapshot JSON path (default {DEFAULT_SNAPSHOT.name})")
    ap.add_argument("--history-csv", type=pathlib.Path, default=DEFAULT_CSV,
                    help=f"CSV history path (default {DEFAULT_CSV.name})")
    args = ap.parse_args()

    try:
        node_specs = collect_nodes(args.node, args.nodes_json)
    except ValueError as e:
        ap.error(str(e))
    if not node_specs:
        ap.error(f"no nodes: pass --node NAME:OMR or populate {args.nodes_json}")

    states = [NodeState(name, omr) for name, omr in node_specs]
    otbr = OTBR(args.otbr_mode, args.otbr_container, args.otbr_host, args.otbr_user)
    history = seed_history(args.snapshot_out)
    ensure_csv(args.history_csv)

    log(f"monitoring {len(states)} node(s): {', '.join(s.name for s in states)}")
    log(f"otbr={args.otbr_mode}:{args.otbr_container}  probe={args.probe_mode}  "
        f"interval={args.interval}s  duration={'inf' if args.duration <= 0 else args.duration}")
    log(f"snapshot -> {args.snapshot_out}   history -> {args.history_csv}")

    t0 = time.time()
    deadline = None if args.duration <= 0 else t0 + args.duration
    cycle = 0
    try:
        while True:
            cycle += 1
            ts = now_iso()
            mono = time.monotonic()

            # --- sample every source; each failure is contained to null + log ---
            otbr_state = read_otbr(otbr)
            nodes = [safe_read_node(s, args.port, args.probe_mode, otbr, otbr_state, mono)
                     for s in states]
            power = read_power(args.fnb_json)

            # --- history ring point (fleet-level rollups) ---
            avails = [n["availability_pct"] for n in nodes if n["availability_pct"] is not None]
            bpms = [n["osi"]["bytes_per_min"] for n in nodes if n["osi"]["bytes_per_min"] is not None]
            history.append({
                "ts_iso": ts,
                "i_ma": power["i_ma"],
                "availability": round(sum(avails) / len(avails), 2) if avails else None,
                "bytes_per_min": round(sum(bpms), 2) if bpms else None,
            })

            snap = {"ts_iso": ts, "otbr": otbr_state, "nodes": nodes,
                    "power": power, "history": list(history)}

            # --- persist (snapshot atomically; append CSV rows) ---
            try:
                write_snapshot(args.snapshot_out, snap)
            except Exception as e:
                log(f"snapshot write failed: {e}")
            for n in nodes:
                try:
                    append_csv(args.history_csv, ts, n, otbr_state, power)
                except Exception as e:
                    log(f"csv append failed for {n['name']}: {e}")

            print(f"+{(time.time() - t0) / 60:6.1f}m  {status_line(otbr_state, nodes, power)}",
                  flush=True)

            # --- sleep, waking early near the deadline ---
            if deadline is not None and time.time() >= deadline:
                break
            nap = args.interval
            if deadline is not None:
                nap = min(nap, max(0.0, deadline - time.time()))
            if nap > 0:
                time.sleep(nap)
    except KeyboardInterrupt:
        log("interrupted — final snapshot already written")

    log(f"done: {cycle} cycle(s), snapshot at {args.snapshot_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
