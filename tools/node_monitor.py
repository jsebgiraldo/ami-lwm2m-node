#!/usr/bin/env python3
"""Active system monitor + e2e/gap analyzer for a connected XIAO ESP32-C6 AMI node
on the pi4/Thread mesh — the Thread-stack analog of the R1000 HaLow research monitor.

Each run samples the WHOLE pipeline for ONE node and:
  - maps per-OSI-layer state of the 802.15.4/Thread/LwM2M stack (L1..L7),
  - checks every e2e LINK (meter -> node -> mesh -> OTBR -> TB Edge -> TB Server),
  - flags GAPS: for each expected datum, is it flowing? if not, WHERE did the chain
    break and WHY,
  - appends one record to node_history_<suffix>.json and renders node_report_<suffix>.html.

Sources (sequential, single TB token + single OTBR SSH — deliberately NOT parallel, to
avoid saturating TB Edge's Postgres pool; same lesson as tools/net_capacity.py):
  - TB Edge REST (firmware telemetry: mac_*, lwm2m_tx_bytes RID38, reg_*, recover, role, meter)
  - /diag + /ami CoAP GET over IPv6 (fw, role, reg_ok, live OBIS) — reuses tools/diag_get.py
  - OTBR ot-ctl over SSH (mesh role, mac counters, router/child/neighbor, partition)
  - TB Server central (is the edge actually syncing the device+telemetry upstream?)

Usage:  python tools/node_monitor.py --suffix 25c0            # one collect + render + text report
        python tools/node_monitor.py --suffix 25c0 --text     # text e2e/gap report only (no html/history)
"""
from __future__ import annotations
import argparse, json, os, re, sys, time, datetime, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fleet_common as fc
fc.bootstrap_venv()
import requests
requests.packages.urllib3.disable_warnings()

HERE = pathlib.Path(__file__).resolve().parent
LOC = datetime.timezone(datetime.timedelta(hours=-5))

# --- infra endpoints ---------------------------------------------------------
EDGE_HOST, EDGE_PORT = fc.MESH_TO_EDGE["pi4"]          # 192.168.1.111:8090
EDGE = f"http://{EDGE_HOST}:{EDGE_PORT}"
SERVER = "https://192.168.1.159"                       # central TB server (local docker stack)
OTBR_HOST, OTBR_USER, OTBR_PASS = "192.168.1.111", "root", "root"
TENANT_USER, TENANT_PASS = "tenant@thingsboard.org", "tenant"

# firmware telemetry keys grouped by the OSI layer they characterize
LAYER_KEYS = {
    "L2_mac": ["mac_tx_total", "mac_rx_total", "mac_tx_unicast", "mac_rx_unicast",
               "mac_tx_broadcast", "mac_rx_broadcast", "mac_rx_err_no_frame", "mac_tx_err_abort"],
    "L4L7_app": ["lwm2m_tx_bytes", "notify_emitted", "notify_throttled"],
    "L5_session": ["reg_success", "reg_attempts", "recover_count", "restart_success",
                   "storm_backoff_applied", "thread_role", "thread_partition_id"],
    "health": ["uptime_s", "total_resets", "watchdog_count", "last_reset_reason", "last_error_code"],
    "L7_meter": ["voltage", "current", "activePower", "activePower3P", "reactivePower3P",
                 "apparentPower3P", "powerFactor3P", "activeEnergy", "frequency"],
}


def _tb_login(base, user, pw, timeout=10):
    r = requests.post(base + "/api/auth/login",
                      json={"username": user, "password": pw}, verify=False, timeout=timeout)
    r.raise_for_status()
    return {"X-Authorization": "Bearer " + r.json()["token"]}


def _tb_device(base, H, name, timeout=10):
    r = requests.get(base + "/api/tenant/devices", headers=H,
                     params={"pageSize": 1, "page": 0, "textSearch": name}, verify=False, timeout=timeout).json()
    for d in r.get("data", []):
        if d.get("name") == name:
            return d
    return None


def _latest(base, H, did, keys, timeout=15):
    r = requests.get(base + f"/api/plugins/telemetry/DEVICE/{did}/values/timeseries",
                     headers=H, params={"keys": ",".join(keys)}, verify=False, timeout=timeout).json()
    return {k: (v[0].get("value"), int(v[0].get("ts"))) for k, v in r.items() if v}


def _server_ssh_omr_from_transportlog(base, H, did, timeout=10):
    now = int(time.time() * 1000)
    r = requests.get(base + f"/api/plugins/telemetry/DEVICE/{did}/values/timeseries",
                     headers=H, params={"keys": "transportLog", "startTs": now - 300000,
                                        "endTs": now, "limit": 20, "orderBy": "DESC"}, verify=False, timeout=timeout).json()
    for e in r.get("transportLog", []):
        m = re.search(r"\[([0-9a-fA-F:]+)\]:\d+", e.get("value", ""))
        if m:
            return m.group(1)
    return None


def _coap_get(omr, path, port=5685):
    """/diag or /ami over IPv6 via tools/diag_get.py's hardened client."""
    if not omr:
        return None
    try:
        import diag_get
        ns: dict = {}
        exec(compile(diag_get.COAP_GET_SRC, "<coap>", "exec"), ns)
        code, body = ns["coap_get"](omr, port, path, retries=3, timeout=1.5)
        if (code >> 5) == 2:
            return json.loads(body)
    except Exception:
        pass
    return None


def _otbr(cmds, timeout=20):
    """Run ot-ctl commands on the OTBR over one SSH session. Returns {label: output}."""
    try:
        import paramiko
    except Exception:
        return {}
    out = {}
    try:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(OTBR_HOST, username=OTBR_USER, password=OTBR_PASS, timeout=8)
        for label, cmd in cmds:
            try:
                _i, o, _e = c.exec_command(cmd, timeout=timeout)
                out[label] = o.read().decode(errors="replace").strip()
            except Exception:
                out[label] = None
        c.close()
    except Exception:
        return out
    return out


def collect(suffix):
    now = datetime.datetime.now(LOC)
    name = fc.PREFIX + suffix if hasattr(fc, "PREFIX") else f"ami-esp32c6-{suffix}"
    s = {"ts": int(now.timestamp() * 1000), "iso": now.strftime("%Y-%m-%d %H:%M"),
         "suffix": suffix, "endpoint": name, "links": {}, "gaps": []}

    def link(k, ok, detail):
        s["links"][k] = {"ok": bool(ok), "detail": detail}
        if not ok:
            s["gaps"].append({"link": k, "why": detail})

    # ---- LINK 5-6: TB Edge (does the edge have the device, is it active/fresh?) ----
    edge_H = None
    dev = None
    try:
        edge_H = _tb_login(EDGE, TENANT_USER, TENANT_PASS)
        dev = _tb_device(EDGE, edge_H, name)
        link("edge_reachable", True, f"{EDGE} up; device {'found' if dev else 'MISSING'}")
    except Exception as e:
        link("edge_reachable", False, f"TB Edge login/query failed: {type(e).__name__}: {e}")

    tele = {}
    active = None
    la_s = None
    if dev and edge_H:
        did = dev["id"]["id"]
        s["did"] = did
        try:
            attrs = {a["key"]: a["value"] for a in requests.get(
                EDGE + f"/api/plugins/telemetry/DEVICE/{did}/values/attributes/SERVER_SCOPE",
                headers=edge_H, verify=False, timeout=10).json()}
            active = attrs.get("active")
            la = attrs.get("lastActivityTime")
            la_s = (s["ts"] - la) / 1000 if la else None
        except Exception:
            pass
        allkeys = [k for grp in LAYER_KEYS.values() for k in grp]
        tele = _latest(EDGE, edge_H, did, allkeys)
        s["tele"] = {k: tele[k][0] for k in tele}
        # freshness of the key telemetry (activePower cadence)
        ap_age = (s["ts"] - tele["activePower"][1]) / 1000 if "activePower" in tele else None
        s["ap_age_s"] = ap_age
        link("otbr_to_edge_registered", bool(active),
             f"active={active} lastActivity={la_s:.0f}s ago" if la_s is not None else f"active={active}")
        link("app_telemetry_fresh", ap_age is not None and ap_age < 180,
             f"activePower last point {ap_age:.0f}s ago" if ap_age is not None else "no activePower telemetry")
        # coverage over the last hour (expected ~1/min for LwM2M update period 60-300s)
        try:
            end = s["ts"]; start = end - 3600 * 1000
            d = requests.get(EDGE + f"/api/plugins/telemetry/DEVICE/{did}/values/timeseries",
                             headers=edge_H, params={"keys": "activePower", "startTs": start, "endTs": end,
                                                     "limit": 5000, "agg": "NONE", "orderBy": "ASC"},
                             verify=False, timeout=20).json().get("activePower", [])
            tss = sorted(int(p["ts"]) for p in d)
            s["cov_pts"] = len(tss)
            s["cov_gaps"] = sum(1 for a, b in zip(tss, tss[1:]) if (b - a) / 1000 > 180)
        except Exception:
            s["cov_pts"] = None; s["cov_gaps"] = None

    # ---- LINK 2-4: node over IPv6 (/diag + /ami) — proves radio+mesh+routing+coap ----
    omr = None
    if dev and edge_H:
        omr = _server_ssh_omr_from_transportlog(EDGE, edge_H, dev["id"]["id"])
    s["omr"] = omr
    diag = _coap_get(omr, "diag")
    ami = _coap_get(omr, "ami")
    s["diag"] = diag
    s["ami"] = ami
    link("mesh_routing_ipv6", bool(diag),
         f"/diag over OMR {omr}: fw={diag.get('fw')} role={diag.get('role')} reg_ok={diag.get('reg_ok')}"
         if diag else (f"OMR {omr} unreachable over IPv6" if omr else "no OMR (node never registered / transportLog empty)"))
    if ami is not None:
        if ami.get("valid"):
            link("meter_to_node_dlms", ami.get("reads", 0) and ami.get("errs", 1) == 0,
                 f"DLMS reads={ami.get('reads')} errs={ami.get('errs')} V={ami.get('vr')} P={ami.get('ptot')}kW f={ami.get('freq')}")
        else:
            link("meter_to_node_dlms", False, "/ami valid=false — no DLMS meter reading (demo node or RS485 down)")
    else:
        link("meter_to_node_dlms", False, "/ami unreachable (need node on >=0.7.15 + OMR routable)")

    # ---- LINK 2-3: mesh from the OTBR side ----
    ot = _otbr([("state", "ot-ctl state"),
                ("counters", "ot-ctl counters mac"),
                ("router", "ot-ctl router table"),
                ("child", "ot-ctl child table"),
                ("neighbor", "ot-ctl neighbor table"),
                ("partition", "ot-ctl partitionid"),
                ("channel", "ot-ctl channel")])
    s["otbr"] = {"state": (ot.get("state") or "").splitlines()[0] if ot.get("state") else None}
    if ot.get("counters"):
        for m in re.finditer(r"(TxTotal|RxTotal|TxUnicast|RxUnicast|TxBroadcast|RxBroadcast|RxErr\w*):\s*(\d+)", ot["counters"]):
            s["otbr"][m.group(1)] = int(m.group(2))
    for lbl in ("router", "child", "neighbor"):
        if ot.get(lbl):
            s["otbr"][lbl + "_rows"] = max(0, len([l for l in ot[lbl].splitlines() if "|" in l]) - 1)
    link("otbr_mesh_up", s["otbr"].get("state") in ("leader", "router", "child"),
         f"OTBR state={s['otbr'].get('state')} RxTotal={s['otbr'].get('RxTotal')} "
         f"routers={s['otbr'].get('router_rows')} children={s['otbr'].get('child_rows')}")

    # ---- LINK 6: TB Server central (is the edge syncing the device upstream?) ----
    try:
        srv_H = _tb_login(SERVER, TENANT_USER, TENANT_PASS, timeout=10)
        srv_dev = _tb_device(SERVER, srv_H, name)
        if srv_dev:
            sd = srv_dev["id"]["id"]
            sattrs = {a["key"]: a["value"] for a in requests.get(
                SERVER + f"/api/plugins/telemetry/DEVICE/{sd}/values/attributes/SERVER_SCOPE",
                headers=srv_H, verify=False, timeout=10).json()}
            s_la = (s["ts"] - sattrs.get("lastActivityTime", 0)) / 1000 if sattrs.get("lastActivityTime") else None
            link("edge_to_server_sync", bool(sattrs.get("active")) or (s_la is not None and s_la < 600),
                 f"server has device; active={sattrs.get('active')} lastActivity={f'{s_la:.0f}s' if s_la is not None else '?'} ago")
            s["server_la_s"] = s_la
        else:
            link("edge_to_server_sync", False, "device NOT present on central TB server — edge->server sync gap")
    except Exception as e:
        link("edge_to_server_sync", False, f"TB server unreachable/login failed: {type(e).__name__}")

    return s


def text_report(s):
    print("=" * 78)
    print(f"AMI NODE MONITOR — {s['endpoint']}   {s['iso']}   (pi4/Thread e2e)")
    print("=" * 78)
    CHAIN = [
        ("meter_to_node_dlms",    "L7  Medidor  -> Nodo    (DLMS/RS485)"),
        ("mesh_routing_ipv6",     "L3  Nodo     -> Malla   (802.15.4 + 6LoWPAN, /diag IPv6)"),
        ("otbr_mesh_up",          "L2  Malla    -> OTBR    (Thread border router)"),
        ("otbr_to_edge_registered","L5 OTBR     -> TB Edge (LwM2M registro)"),
        ("app_telemetry_fresh",   "L7  Nodo     -> TB Edge (telemetría fresca)"),
        ("edge_to_server_sync",   "    TB Edge  -> TB Server (sync upstream)"),
    ]
    for k, label in CHAIN:
        lk = s["links"].get(k)
        mark = "OK  " if (lk and lk["ok"]) else "GAP "
        print(f"  [{mark}] {label}")
        if lk:
            print(f"          {lk['detail']}")
    print("-" * 78)
    if s["gaps"]:
        print(f"GAPS ({len(s['gaps'])}) — dónde/por qué se rompió:")
        for g in s["gaps"]:
            print(f"  ✗ {g['link']}: {g['why']}")
    else:
        print("SIN GAPS — cadena e2e completa.")
    t = s.get("tele", {})
    if t:
        print("-" * 78)
        print("Telemetría por capa (último valor):")
        print(f"  L2 MAC : tx_total={t.get('mac_tx_total')} rx_total={t.get('mac_rx_total')} "
              f"rx_err={t.get('mac_rx_err_no_frame')} tx_abort={t.get('mac_tx_err_abort')}")
        print(f"  L4/L7  : lwm2m_tx_bytes={t.get('lwm2m_tx_bytes')} notify_emitted={t.get('notify_emitted')} "
              f"throttled={t.get('notify_throttled')}")
        print(f"  L5 sess: reg_success={t.get('reg_success')} reg_attempts={t.get('reg_attempts')} "
              f"recover={t.get('recover_count')} role={t.get('thread_role')} part={t.get('thread_partition_id')}")
        print(f"  health : uptime_s={t.get('uptime_s')} resets={t.get('total_resets')} wdog={t.get('watchdog_count')} "
              f"last_err={t.get('last_error_code')}")
        print(f"  L7 mtr : V={t.get('voltage')} I={t.get('current')} P={t.get('activePower')} "
              f"E={t.get('activeEnergy')} f={t.get('frequency')}")
    if s.get("cov_pts") is not None:
        print(f"  cobertura 1h: {s['cov_pts']} pts activePower, {s.get('cov_gaps')} huecos(>180s)")
    print("=" * 78)


CHAIN = [
    ("meter_to_node_dlms",      "L7 · Medidor → Nodo",   "DLMS/RS485 lee el medidor"),
    ("mesh_routing_ipv6",       "L3 · Nodo → Malla",     "802.15.4 + 6LoWPAN, /diag por IPv6"),
    ("otbr_mesh_up",            "L2 · Malla → OTBR",      "Thread border router"),
    ("otbr_to_edge_registered", "L5 · OTBR → TB Edge",    "registro LwM2M"),
    ("app_telemetry_fresh",     "L7 · Nodo → TB Edge",    "telemetría fresca"),
    ("edge_to_server_sync",     "·· · TB Edge → Server",  "sync upstream al TB central"),
]


def render(hist, out_path):
    import html as _h
    s = hist[-1]
    links = s.get("links", {})
    gaps = s.get("gaps", [])
    chain_ok = sum(1 for k, *_ in CHAIN if links.get(k, {}).get("ok"))
    healthy = chain_ok == len(CHAIN)
    t = s.get("tele", {})
    diag = s.get("diag") or {}
    ami = s.get("ami") or {}
    ot = s.get("otbr") or {}

    def esc(x):
        return _h.escape(str(x)) if x is not None else "—"

    # e2e chain rows
    chain_html = ""
    for k, label, sub in CHAIN:
        lk = links.get(k, {})
        ok = lk.get("ok")
        cls = "ok" if ok else "gap"
        chain_html += (f'<div class="link {cls}"><div class="lk-h"><span class="dot"></span>'
                       f'<b>{label}</b><span class="badge">{"OK" if ok else "GAP"}</span></div>'
                       f'<div class="lk-sub">{esc(sub)}</div>'
                       f'<div class="lk-det">{esc(lk.get("detail"))}</div></div>')

    # per-layer cards
    def card(title, big, sub):
        return f'<div class="card"><div class="ct">{title}</div><div class="cb">{big}</div><div class="cs">{esc(sub)}</div></div>'
    cards = ""
    cards += card("L1/L2 · Radio", f'{esc(diag.get("role") or t.get("thread_role"))}',
                  f'rloc {esc(diag.get("rloc16"))} · part {esc(t.get("thread_partition_id"))}')
    cards += card("L2 · MAC 802.15.4", f'{esc(t.get("mac_tx_total"))}<span class="u"> tx</span> / {esc(t.get("mac_rx_total"))}<span class="u"> rx</span>',
                  f'err {esc(t.get("mac_rx_err_no_frame"))} · abort {esc(t.get("mac_tx_err_abort"))}')
    cards += card("L4 · Bytes app (CoAP)", f'{esc(t.get("lwm2m_tx_bytes"))}<span class="u"> B</span>',
                  f'notify {esc(t.get("notify_emitted"))} · throttled {esc(t.get("notify_throttled"))}')
    cards += card("L5 · Sesión LwM2M", f'reg {esc(t.get("reg_success"))}/{esc(t.get("reg_attempts"))}',
                  f'recover {esc(t.get("recover_count"))} · wdog {esc(t.get("watchdog_count"))}')
    cards += card("L7 · Medidor (OBIS)",
                  (f'{ami.get("vr")}<span class="u"> V</span> · {ami.get("ptot")}<span class="u"> kW</span>' if ami.get("valid")
                   else f'{esc(t.get("voltage"))}<span class="u"> V</span> · {esc(t.get("activePower"))}<span class="u"> kW</span>'),
                  (f'reads {ami.get("reads")} errs {ami.get("errs")} f={ami.get("freq")}Hz' if ami.get("valid")
                   else f'E={esc(t.get("activeEnergy"))}kWh f={esc(t.get("frequency"))}'))
    cards += card("·· · Salud nodo", f'{esc(t.get("uptime_s"))}<span class="u"> s up</span>',
                  f'resets {esc(t.get("total_resets"))} · last_err {esc(t.get("last_error_code"))}')

    # gaps
    gap_html = "".join(f'<li><b>{esc(g["link"])}</b> — {esc(g["why"])}</li>' for g in gaps) or "<li>sin gaps — cadena e2e completa</li>"

    # history table (newest first, cap 48)
    rows = ""
    for e in reversed(hist[-48:]):
        el = e.get("links", {})
        okc = sum(1 for k, *_ in CHAIN if el.get(k, {}).get("ok"))
        te = e.get("tele", {})
        rows += (f'<tr><td>{esc(e.get("iso"))}</td><td class="{"okc" if okc==len(CHAIN) else "gapc"}">{okc}/{len(CHAIN)}</td>'
                 f'<td>{esc(te.get("thread_role"))}</td><td>{esc(te.get("mac_tx_total"))}/{esc(te.get("mac_rx_total"))}</td>'
                 f'<td>{esc(te.get("reg_success"))}</td><td>{esc(te.get("recover_count"))}</td>'
                 f'<td>{esc(te.get("activePower"))}</td><td>{esc(e.get("cov_pts"))}</td>'
                 f'<td>{esc(len(e.get("gaps",[])))}</td></tr>')

    status = "E2E COMPLETO" if healthy else f"{len(gaps)} GAP(S)"
    scls = "up" if healthy else "down"
    html_doc = f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AMI Node Monitor · {esc(s.get("suffix"))}</title>
<style>
:root{{--bg:#0d1117;--fg:#e6edf3;--mut:#8b949e;--card:#161b22;--bd:#30363d;--ok:#3fb950;--gap:#f85149;--acc:#58a6ff}}
@media(prefers-color-scheme:light){{:root{{--bg:#f6f8fa;--fg:#1f2328;--mut:#59636e;--card:#fff;--bd:#d0d7de}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}}
.wrap{{max-width:1100px;margin:0 auto;padding:24px}}h1{{font-size:20px;margin:0 0 2px}}.sub{{color:var(--mut);font-size:13px}}
.status{{display:inline-block;padding:3px 12px;border-radius:20px;font-weight:600;font-size:12px;margin-top:8px}}
.status.up{{background:rgba(63,185,80,.15);color:var(--ok)}}.status.down{{background:rgba(248,81,73,.15);color:var(--gap)}}
.chain{{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:8px;margin:18px 0}}
.link{{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:10px 12px;border-left:3px solid var(--bd)}}
.link.ok{{border-left-color:var(--ok)}}.link.gap{{border-left-color:var(--gap)}}
.lk-h{{display:flex;align-items:center;gap:6px;font-size:13px}}.badge{{margin-left:auto;font-size:10px;font-weight:700;padding:1px 6px;border-radius:4px;background:var(--bd)}}
.link.ok .badge{{background:rgba(63,185,80,.2);color:var(--ok)}}.link.gap .badge{{background:rgba(248,81,73,.2);color:var(--gap)}}
.dot{{width:8px;height:8px;border-radius:50%;background:var(--gap)}}.link.ok .dot{{background:var(--ok)}}
.lk-sub{{color:var(--mut);font-size:11px;margin:3px 0}}.lk-det{{font-size:11px;font-family:ui-monospace,monospace;color:var(--fg);word-break:break-word}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin:14px 0}}
.card{{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:12px}}
.ct{{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.4px}}.cb{{font-size:22px;font-weight:600;margin:4px 0}}.cb .u{{font-size:12px;color:var(--mut);font-weight:400}}.cs{{color:var(--mut);font-size:12px}}
.gaps{{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:12px 16px;margin:14px 0}}.gaps h3{{margin:0 0 6px;font-size:13px;color:var(--gap)}}.gaps ul{{margin:0;padding-left:18px}}.gaps li{{font-size:12px;margin:3px 0}}
table{{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}}th,td{{text-align:left;padding:5px 8px;border-bottom:1px solid var(--bd)}}th{{color:var(--mut);font-weight:600}}
.okc{{color:var(--ok);font-weight:600}}.gapc{{color:var(--gap);font-weight:600}}.tw{{overflow-x:auto}}
h2{{font-size:14px;margin:20px 0 6px;color:var(--mut)}}
</style></head><body><div class="wrap">
<h1>AMI Node Monitor · {esc(s.get("endpoint"))}</h1>
<div class="sub">Telemetría e2e por capa OSI del nodo XIAO ESP32-C6 sobre malla Thread (802.15.4) → OTBR → TB Edge → TB Server. Muestreo para análisis/investigación.</div>
<span class="status {scls}">● {status}</span> <span class="sub">· actualizado {esc(s.get("iso"))} · {len(hist)} muestras</span>
<h2>Cadena end-to-end</h2>
<div class="chain">{chain_html}</div>
<h2>Estado por capa (último valor)</h2>
<div class="cards">{cards}</div>
<div class="gaps"><h3>Gaps — dónde y por qué se rompió</h3><ul>{gap_html}</ul></div>
<h2>Registro de muestras</h2>
<div class="tw"><table><tr><th>Hora</th><th>e2e</th><th>role</th><th>MAC tx/rx</th><th>reg</th><th>recover</th><th>P (kW)</th><th>cov pts</th><th>gaps</th></tr>{rows}</table></div>
<div class="sub" style="margin-top:16px">OTBR: state={esc(ot.get("state"))} · RxTotal={esc(ot.get("RxTotal"))} · routers={esc(ot.get("router_rows"))} · children={esc(ot.get("child_rows"))} · edge {esc(EDGE)} · server {esc(SERVER)}</div>
</div></body></html>"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--suffix", default="25c0", help="node suffix (default 25c0 = real EMSITECH node)")
    ap.add_argument("--text", action="store_true", help="text e2e/gap report only (no html/history)")
    args = ap.parse_args()
    if not hasattr(fc, "PREFIX"):
        fc.PREFIX = "ami-esp32c6-"
    s = collect(args.suffix)
    text_report(s)
    if not args.text:
        hist_path = HERE / f"node_history_{args.suffix}.json"
        try:
            hist = json.load(open(hist_path))
        except Exception:
            hist = []
        hist.append(s)
        json.dump(hist, open(hist_path, "w"), indent=0)
        out_html = HERE / f"node_report_{args.suffix}.html"
        render(hist, out_html)
        print(f"history: {len(hist)} samples -> {hist_path.name} | dashboard -> {out_html.name}")


if __name__ == "__main__":
    main()
