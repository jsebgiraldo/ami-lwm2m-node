"""Single-node continuous observability harness for soak/stability testing.

Goal: capture EVERYTHING that happens to one node over 12-24+ hours so we can
diagnose the "node dies after ~12 h" failure mode. Without this, every test we
write is guessing what failed.

Streams captured in parallel, each to its own append-only file under
`soak_logs/<run-tag>/` (gitignored):

  serial.log     UART/USB-CDC console of the node, line-prefixed with the
                 host wall clock (the firmware's own [uptime] stamps stay).
                 Buffered raw mode so a USB-CDC freeze leaves the file at
                 the freeze instant, not silently truncated.

  sniffer.pcap   802.15.4 frames seen by the sniffer dongle on COM62,
                 encoded as DLT_IEEE802_15_4_TAP (open in Wireshark).
                 Filter on PAN 0x41ae in Wireshark to see only our mesh.

  tbedge.jsonl   One JSON object per poll of the TB Edge REST API for the
                 target device: active flag, lastActivityTime, telemetry
                 latest values (voltage, temperature, uptime_s, total_resets,
                 watchdog_count, recover_count, reg_attempts, reg_success).

  otbr.jsonl     One JSON object per poll of the OTBR (via SSH): role,
                 child table (RLOC16 / age / LQ_In), MAC counters
                 (Tx/Rx totals + errors), MLE counters.

  events.jsonl   Single timeline of derived events the analyzer cares about:
                 RESET (uptime jump backward), DROP (active → inactive),
                 RECOVER (inactive → active), CHILD_LOST/CHILD_GAINED on
                 the OTBR table, SERIAL_SILENT (>N s no output), HEAP_OOM
                 markers if we ever see them. Lets you skim the failure
                 window without grepping the raw logs.

Usage:
    python tools/soak_harness.py --com COM17 --endpoint ami-esp32c6-1494
    python tools/soak_harness.py --duration 24h
    python tools/soak_harness.py --skip-sniffer       # if no dongle present
    python tools/soak_harness.py --skip-otbr          # if R1000 ssh down

Ctrl-C cleanly closes all files and prints a one-line summary.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# ── bootstrap project venv (gives us requests, pyserial, paramiko) ──
TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))
import fleet_common as fc           # noqa: E402
fc.bootstrap_venv()
import requests                     # noqa: E402
import serial                       # noqa: E402
import paramiko                     # noqa: E402
import sniffer_capture as sn        # noqa: E402  reuses pcap encoding

# ─────────────────────────── defaults ───────────────────────────
DEFAULT_COM_NODE = "COM17"
DEFAULT_ENDPOINT = "ami-esp32c6-1494"
DEFAULT_COM_SNIFFER = "COM62"
DEFAULT_SNIFFER_BAUD = 1_000_000
DEFAULT_SNIFFER_CHANNEL = 21
DEFAULT_EDGE_URL = "http://192.168.8.111:8090"
DEFAULT_EDGE_USER = fc.EDGE_TENANT_USER
DEFAULT_EDGE_PASS = fc.EDGE_TENANT_PASS
DEFAULT_OTBR_HOST = "192.168.8.111"
DEFAULT_OTBR_USER = "root"
DEFAULT_OTBR_PASS = "root"

POLL_EDGE_S = 30
POLL_OTBR_S = 60
SERIAL_SILENT_THRESHOLD_S = 90      # mark SILENT event if no bytes >N s
ROTATE_SERIAL_BYTES = 50 * 1024 * 1024


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_duration(s: str) -> float:
    """'12h', '30m', '90s', '3d' -> seconds. Bare int is seconds."""
    s = s.strip().lower()
    if s.isdigit():
        return float(s)
    unit = s[-1]
    val = float(s[:-1])
    return val * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


# ─────────────────────────── shared state ──────────────────────────
class Soak:
    def __init__(self, run_dir: Path, args):
        self.run_dir = run_dir
        self.args = args
        self.stop = threading.Event()
        self.events_q: queue.Queue = queue.Queue()
        # Files (opened lazily by each collector to keep them owned by the
        # thread that does the writes).
        self.serial_path = run_dir / "serial.log"
        self.sniffer_path = run_dir / "sniffer.pcap"
        self.tbedge_path = run_dir / "tbedge.jsonl"
        self.otbr_path = run_dir / "otbr.jsonl"
        self.events_path = run_dir / "events.jsonl"
        # State tracked across polls (for event derivation)
        self.last_uptime_s: int | None = None
        self.last_active: bool | None = None
        self.last_child_rlocs: set[str] = set()
        self.last_serial_byte_ts: float = time.time()
        self.serial_silent_emitted = False

    def emit_event(self, kind: str, **payload):
        evt = {"ts": now_iso(), "kind": kind, **payload}
        self.events_q.put(evt)


# ─────────────────────────── collectors ────────────────────────────
def collect_serial(soak: Soak):
    """Append USB-CDC output to serial.log, line-prefixed with wall clock."""
    com = soak.args.com
    baud = soak.args.baud
    out = soak.serial_path.open("ab", buffering=0)
    out.write(f"\n=== soak harness started {now_iso()} on {com} @ {baud} ===\n".encode())
    sys.stderr.write(f"[serial] open {com} @ {baud}\n")
    backoff = 1.0
    while not soak.stop.is_set():
        try:
            ser = serial.Serial(com, baud, timeout=0.5)
            ser.dtr = True
            ser.rts = True
            backoff = 1.0  # reset on successful open
            partial = b""
            while not soak.stop.is_set():
                chunk = ser.read(4096)
                if not chunk:
                    # Heartbeat: check serial-silent threshold
                    if (time.time() - soak.last_serial_byte_ts > SERIAL_SILENT_THRESHOLD_S
                            and not soak.serial_silent_emitted):
                        soak.emit_event("SERIAL_SILENT",
                                        seconds=int(time.time() - soak.last_serial_byte_ts))
                        soak.serial_silent_emitted = True
                    continue
                soak.last_serial_byte_ts = time.time()
                if soak.serial_silent_emitted:
                    soak.emit_event("SERIAL_RESUMED")
                    soak.serial_silent_emitted = False
                # Line-prefix every line with wall clock; preserve partial
                # lines across reads.
                data = partial + chunk
                lines = data.split(b"\n")
                partial = lines[-1]
                stamp = now_iso().encode()
                for ln in lines[:-1]:
                    out.write(b"[" + stamp + b"] " + ln + b"\n")
            ser.close()
        except serial.SerialException as e:
            soak.emit_event("SERIAL_PORT_ERROR", error=str(e))
            sys.stderr.write(f"[serial] {com} error: {e}; retry in {backoff:.0f}s\n")
            for _ in range(int(backoff * 2)):
                if soak.stop.is_set():
                    break
                time.sleep(0.5)
            backoff = min(backoff * 2, 30.0)
        except Exception as e:
            soak.emit_event("SERIAL_EXCEPTION", error=repr(e))
            time.sleep(5)
    out.close()
    sys.stderr.write("[serial] stopped\n")


def collect_sniffer(soak: Soak):
    """Append 802.15.4 frames to sniffer.pcap (DLT_IEEE802_15_4_TAP)."""
    com = soak.args.sniffer_com
    baud = soak.args.sniffer_baud
    chan = soak.args.sniffer_channel
    # Open with PCAP global header only if file is new/empty.
    new_file = not soak.sniffer_path.exists() or soak.sniffer_path.stat().st_size == 0
    out = soak.sniffer_path.open("ab")
    if new_file:
        out.write(sn.pcap_global_header())
        out.flush()
    sys.stderr.write(f"[sniffer] open {com} @ {baud} (ch {chan})\n")
    n_frames = 0
    backoff = 1.0
    while not soak.stop.is_set():
        try:
            ser = serial.Serial(com, baud, timeout=0.5)
            backoff = 1.0
            while not soak.stop.is_set():
                raw = ser.readline()
                if not raw:
                    continue
                line = raw.decode("ascii", errors="replace").strip()
                if not line.startswith("$"):
                    continue
                try:
                    body, rssi, lqi = line[1:].split("|")
                    frame = bytes.fromhex(body)[:-2]  # firmware appends RSSI/LQI
                    rssi_i = int(rssi)
                    lqi_i = int(lqi)
                except (ValueError, IndexError):
                    continue
                payload = sn.tap_header(rssi_i, lqi_i, chan) + frame
                out.write(sn.pcap_record(payload))
                n_frames += 1
                if n_frames % 500 == 0:
                    out.flush()
            ser.close()
        except serial.SerialException as e:
            soak.emit_event("SNIFFER_PORT_ERROR", error=str(e))
            sys.stderr.write(f"[sniffer] {com} error: {e}; retry in {backoff:.0f}s\n")
            for _ in range(int(backoff * 2)):
                if soak.stop.is_set():
                    break
                time.sleep(0.5)
            backoff = min(backoff * 2, 30.0)
        except Exception as e:
            soak.emit_event("SNIFFER_EXCEPTION", error=repr(e))
            time.sleep(5)
    out.flush()
    out.close()
    sys.stderr.write(f"[sniffer] stopped — {n_frames} frames\n")


def collect_tbedge(soak: Soak):
    """Poll TB Edge every POLL_EDGE_S, append snapshot + derive events."""
    out = soak.tbedge_path.open("a", buffering=1)
    s = requests.Session()
    token = None
    token_exp = 0.0
    keys = ["voltage", "temperature", "uptime_s", "total_resets",
            "watchdog_count", "recover_count", "reg_attempts", "reg_success",
            "last_error_code", "last_reset_reason", "thread_role"]

    def auth():
        nonlocal token, token_exp
        r = s.post(f"{soak.args.edge_url}/api/auth/login",
                   json={"username": soak.args.edge_user,
                         "password": soak.args.edge_pass}, timeout=10)
        r.raise_for_status()
        token = r.json()["token"]
        token_exp = time.time() + 8 * 60
        s.headers.update({"X-Authorization": f"Bearer {token}"})

    sys.stderr.write(f"[tbedge] polling {soak.args.edge_url} every {POLL_EDGE_S}s\n")
    while not soak.stop.is_set():
        try:
            if token is None or time.time() > token_exp:
                auth()
            r = s.get(f"{soak.args.edge_url}/api/tenant/deviceInfos",
                      params={"pageSize": 100, "page": 0,
                              "textSearch": soak.args.endpoint}, timeout=15)
            r.raise_for_status()
            dev = next((d for d in r.json().get("data", [])
                        if d["name"] == soak.args.endpoint), None)
            snap = {"ts": now_iso(), "endpoint": soak.args.endpoint}
            if not dev:
                snap["error"] = "device_not_found"
            else:
                snap["active"] = dev.get("active")
                snap["lastActivityTime"] = dev.get("lastActivityTime")
                snap["lastConnectTime"] = dev.get("lastConnectTime")
                snap["lastDisconnectTime"] = dev.get("lastDisconnectTime")
                did = dev["id"]["id"]
                vs = s.get(f"{soak.args.edge_url}/api/plugins/telemetry/DEVICE/{did}/values/timeseries",
                           params={"keys": ",".join(keys)}, timeout=15).json()
                snap["values"] = {k: (vs[k][0]["value"] if vs.get(k) else None)
                                  for k in keys}
                # Event derivation
                if soak.last_active is True and snap["active"] is False:
                    soak.emit_event("DROP", endpoint=soak.args.endpoint)
                elif soak.last_active is False and snap["active"] is True:
                    soak.emit_event("RECOVER", endpoint=soak.args.endpoint)
                soak.last_active = snap["active"]
                up = snap["values"].get("uptime_s")
                if isinstance(up, (int, float)):
                    up = int(up)
                    if (soak.last_uptime_s is not None
                            and up < soak.last_uptime_s):
                        soak.emit_event(
                            "RESET", endpoint=soak.args.endpoint,
                            from_uptime=soak.last_uptime_s, to_uptime=up,
                            total_resets=snap["values"].get("total_resets"))
                    soak.last_uptime_s = up
            out.write(json.dumps(snap) + "\n")
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 401:
                token = None  # force re-auth next loop
            soak.emit_event("TBEDGE_HTTP_ERROR",
                            status=getattr(e.response, "status_code", None),
                            msg=str(e)[:200])
        except Exception as e:
            soak.emit_event("TBEDGE_EXCEPTION", error=repr(e)[:200])
        # Cancellable sleep
        for _ in range(POLL_EDGE_S * 2):
            if soak.stop.is_set():
                break
            time.sleep(0.5)
    out.close()
    sys.stderr.write("[tbedge] stopped\n")


def collect_otbr(soak: Soak):
    """Poll OTBR (ot-ctl) every POLL_OTBR_S, append + derive CHILD events."""
    out = soak.otbr_path.open("a", buffering=1)
    sys.stderr.write(f"[otbr] polling {soak.args.otbr_host} every {POLL_OTBR_S}s\n")

    def ssh_run(client, cmd: str) -> str:
        _, o, _ = client.exec_command(cmd, timeout=15)
        return o.read().decode(errors="replace")

    def parse_children(out_text: str) -> set[str]:
        """Extract RLOC16 hex tokens (0xNNNN) from `ot-ctl child table`."""
        rlocs = set()
        for ln in out_text.splitlines():
            parts = ln.split("|")
            if len(parts) >= 3:
                rloc = parts[2].strip()
                if rloc.startswith("0x"):
                    rlocs.add(rloc)
        return rlocs

    while not soak.stop.is_set():
        client = None
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(soak.args.otbr_host, username=soak.args.otbr_user,
                           password=soak.args.otbr_pass, timeout=10)
            state = ssh_run(client, "ot-ctl state").strip().split("\n")[0]
            children_txt = ssh_run(client, "ot-ctl child table")
            mac_txt = ssh_run(client, "ot-ctl counters mac")
            mle_txt = ssh_run(client, "ot-ctl counters mle")
            snap = {
                "ts": now_iso(),
                "state": state,
                "children_raw": children_txt,
                "mac_counters": mac_txt,
                "mle_counters": mle_txt,
            }
            out.write(json.dumps(snap) + "\n")
            # Derive child gain/loss
            now_rlocs = parse_children(children_txt)
            if soak.last_child_rlocs:
                gained = now_rlocs - soak.last_child_rlocs
                lost = soak.last_child_rlocs - now_rlocs
                for r in gained:
                    soak.emit_event("CHILD_GAINED", rloc16=r)
                for r in lost:
                    soak.emit_event("CHILD_LOST", rloc16=r)
            soak.last_child_rlocs = now_rlocs
        except Exception as e:
            soak.emit_event("OTBR_EXCEPTION", error=repr(e)[:200])
        finally:
            if client:
                try:
                    client.close()
                except Exception:
                    pass
        for _ in range(POLL_OTBR_S * 2):
            if soak.stop.is_set():
                break
            time.sleep(0.5)
    out.close()
    sys.stderr.write("[otbr] stopped\n")


def collect_events(soak: Soak):
    """Drain events_q to events.jsonl (single writer)."""
    out = soak.events_path.open("a", buffering=1)
    while not soak.stop.is_set() or not soak.events_q.empty():
        try:
            evt = soak.events_q.get(timeout=0.5)
        except queue.Empty:
            continue
        out.write(json.dumps(evt) + "\n")
        sys.stderr.write(f"[event] {evt['kind']} {json.dumps({k:v for k,v in evt.items() if k!='kind'})}\n")
    out.close()


# ────────────────────────────── main ───────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--com", default=DEFAULT_COM_NODE,
                    help="Node USB-CDC COM port (default %(default)s).")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT,
                    help="LwM2M endpoint name to poll in TB Edge.")
    ap.add_argument("--sniffer-com", default=DEFAULT_COM_SNIFFER)
    ap.add_argument("--sniffer-baud", type=int, default=DEFAULT_SNIFFER_BAUD)
    ap.add_argument("--sniffer-channel", type=int, default=DEFAULT_SNIFFER_CHANNEL)
    ap.add_argument("--edge-url", default=DEFAULT_EDGE_URL)
    ap.add_argument("--edge-user", default=DEFAULT_EDGE_USER)
    ap.add_argument("--edge-pass", default=DEFAULT_EDGE_PASS)
    ap.add_argument("--otbr-host", default=DEFAULT_OTBR_HOST)
    ap.add_argument("--otbr-user", default=DEFAULT_OTBR_USER)
    ap.add_argument("--otbr-pass", default=DEFAULT_OTBR_PASS)
    ap.add_argument("--duration", default="24h",
                    help="Soak duration: '12h', '90m', '3d', or seconds. Default 24h.")
    ap.add_argument("--out", default="soak_logs",
                    help="Root output directory (a per-run subdir is created).")
    ap.add_argument("--skip-sniffer", action="store_true")
    ap.add_argument("--skip-otbr", action="store_true")
    ap.add_argument("--skip-serial", action="store_true")
    ap.add_argument("--skip-tbedge", action="store_true")
    args = ap.parse_args()

    duration_s = parse_duration(args.duration)
    tag = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = Path(args.out) / f"{tag}_{args.endpoint}"
    run_dir.mkdir(parents=True, exist_ok=True)
    sys.stderr.write(f"[soak] run dir = {run_dir}\n")
    sys.stderr.write(f"[soak] duration = {duration_s/3600:.1f} h\n")

    soak = Soak(run_dir, args)

    # Write manifest so analyzer/operator knows what was captured
    (run_dir / "manifest.json").write_text(json.dumps({
        "started_iso": now_iso(),
        "host": socket.gethostname(),
        "args": {k: getattr(args, k) for k in vars(args) if not k.startswith("_")
                 and k != "edge_pass" and k != "otbr_pass"},
        "duration_s": duration_s,
    }, indent=2))

    threads = []
    threads.append(threading.Thread(target=collect_events, args=(soak,),
                                    name="events", daemon=True))
    if not args.skip_serial:
        threads.append(threading.Thread(target=collect_serial, args=(soak,),
                                        name="serial", daemon=True))
    if not args.skip_sniffer:
        threads.append(threading.Thread(target=collect_sniffer, args=(soak,),
                                        name="sniffer", daemon=True))
    if not args.skip_tbedge:
        threads.append(threading.Thread(target=collect_tbedge, args=(soak,),
                                        name="tbedge", daemon=True))
    if not args.skip_otbr:
        threads.append(threading.Thread(target=collect_otbr, args=(soak,),
                                        name="otbr", daemon=True))

    for t in threads:
        t.start()

    def handle_sigint(signum, frame):
        sys.stderr.write("\n[soak] stopping (Ctrl-C)...\n")
        soak.stop.set()
    signal.signal(signal.SIGINT, handle_sigint)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, handle_sigint)

    soak.emit_event("HARNESS_START", duration_s=duration_s, threads=[t.name for t in threads])

    end = time.time() + duration_s
    try:
        while time.time() < end and not soak.stop.is_set():
            time.sleep(2)
    except KeyboardInterrupt:
        soak.stop.set()

    sys.stderr.write("[soak] signaling stop, waiting for collectors...\n")
    soak.stop.set()
    for t in threads:
        t.join(timeout=15)

    soak.emit_event("HARNESS_STOP", ran_s=int(duration_s))
    # Drain residual events
    while not soak.events_q.empty():
        time.sleep(0.1)

    sys.stderr.write(f"\n[soak] done. logs in: {run_dir}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
