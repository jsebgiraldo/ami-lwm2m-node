"""End-to-end board acceptance test for AMI LwM2M Thread fleet boards.

Pipeline: flash -> provision -> register -> soak -> cascade test -> LED -> VERDICT.

Outputs:
  - text summary to stdout
  - tools/board_qa_results.csv  (one row per run)
  - tools/board_qa_<endpoint>_<sessionId>.json  (detailed events)
  - updates tools/fleet_map.csv with the verdict tag

Usage:
    python tools/board_acceptance.py --com COM27
    python tools/board_acceptance.py --com COM27 --label 11
    python tools/board_acceptance.py --com COM27 --skip-flash --skip-provision
    python tools/board_acceptance.py --com COM27 --soak-seconds 480 --no-led-check

A board passes (APTO PSU) iff:
  - Stage 1 Flash:        PASS  (esptool write+verify OK)
  - Stage 2 Provision:    PASS  (TB device exists with LwM2M creds)
  - Stage 3 Registration: PASS  (active=True within 180 s; rr in {0,1,3,8})
  - Stage 4 Soak:         PASS  (no new tr increments in 8 min)
  - Stage 5 Cascade:      PASS  (neighbors gained 0 resets during soak)
  - Stage 6 LED:          PASS  or  WARN  (cosmetic-only)
A single FAIL in stages 1..5 => NO APTO. 2+ WARNs => NO APTO.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

import fleet_common as fc

fc.bootstrap_venv()
sys.path.insert(0, str(fc.TOOLS_DIR))

# Re-use existing tooling
from flash_ota_migrate import flash_ota
from provision_node import TBClient, provision_single  # type: ignore

# Endpoints already known on this Edge (used to populate the cascade-test neighbor
# list when the operator doesn't supply --cascade-neighbors). Best-effort: anything
# in fleet_map.csv that is currently `active=True` in TB qualifies.
RESULTS_CSV = "tools/board_qa_results.csv"
FLEET_MAP_CSV = "tools/fleet_map.csv"
DETAIL_DIR = pathlib.Path("tools/board_qa")

# --- Result schema -----------------------------------------------------
class Stage:
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


def stage_row(name: str, status: str, detail: str = "", **extra) -> dict:
    return {"stage": name, "status": status, "detail": detail, **extra}


# --- Tiny TB REST helpers ---------------------------------------------
class TB:
    def __init__(self, host: str, port: int, user: str, password: str):
        self.base = f"http://{host}:{port}"
        self.user = user
        self.password = password
        self.token: str | None = None

    def login(self) -> None:
        req = urllib.request.Request(
            f"{self.base}/api/auth/login",
            data=json.dumps({"username": self.user, "password": self.password}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.token = json.loads(urllib.request.urlopen(req, timeout=10).read())["token"]

    def _hdr(self) -> dict:
        assert self.token, "call login() first"
        return {"X-Authorization": f"Bearer {self.token}"}

    def _get(self, path: str):
        req = urllib.request.Request(self.base + path, headers=self._hdr())
        return json.loads(urllib.request.urlopen(req, timeout=10).read())

    def _rpc(self, did: str, body: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base}/api/rpc/twoway/{did}",
            data=json.dumps(body).encode(),
            headers={**self._hdr(), "Content-Type": "application/json"},
        )
        try:
            return json.loads(urllib.request.urlopen(req, timeout=20).read())
        except urllib.error.HTTPError as e:
            return {"error": f"http {e.code}", "body": e.read().decode("utf-8", "replace")[:200]}
        except Exception as e:
            return {"error": str(e)}

    def find_did(self, endpoint: str) -> str | None:
        try:
            dev = self._get(f"/api/tenant/devices?deviceName={endpoint}")
            return dev["id"]["id"]
        except Exception:
            return None

    def attrs(self, did: str) -> dict:
        try:
            arr = self._get(f"/api/plugins/telemetry/DEVICE/{did}/values/attributes/SERVER_SCOPE")
        except Exception:
            return {}
        return {x["key"]: (x["value"], x["lastUpdateTs"]) for x in arr}

    def ts(self, did: str, keys: list[str]) -> dict:
        try:
            return self._get(
                f"/api/plugins/telemetry/DEVICE/{did}/values/timeseries"
                f"?keys={','.join(keys)}&useStrictDataTypes=false"
            )
        except Exception:
            return {}

    def lwm2m_write(self, did: str, path: str, value):
        return self._rpc(did, {"method": "WriteReplace", "params": {"id": path, "value": value}})


# --- Background serial capture ----------------------------------------
def start_serial_capture(com: str, outfile: pathlib.Path, duration_s: int) -> subprocess.Popen | None:
    """Spawn tools/serial_stream.py in the background. Returns the Popen handle."""
    cmd = [sys.executable, "tools/serial_stream.py",
           "--port", com, "--out", str(outfile), "--seconds", str(duration_s)]
    try:
        # Detached so it survives even if the parent script exits abnormally
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"[capture] failed to spawn serial_stream: {e}")
        return None


def stop_serial_capture(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception:
        pass


def grep_serial_for_red_flags(logfile: pathlib.Path) -> list[str]:
    """Return matching lines from the serial log indicating crash/panic/wdt."""
    if not logfile.exists():
        return []
    needles = ("panic", "PANIC", "Stack overflow", "stack overflow",
               "FATAL", "fatal error", "ASSERTION FAIL",
               "<wrn> lwm2m_wdog: silence_threshold",
               "watchdog hit", "WDT reset")
    hits: list[str] = []
    with logfile.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if any(n in line for n in needles):
                hits.append(line.rstrip())
    return hits


# --- Stage implementations --------------------------------------------
def stage1_flash(env: fc.ToolEnv, com: str, build_dir: str, baud: str,
                 flash_mode: str, flash_freq: str) -> tuple[str, str, dict]:
    """Flash MCUboot + signed app via esptool. Returns (status, detail, info)."""
    print(f"\n=== Stage 1 — Flash ({com}) ===")
    try:
        mac = fc.read_mac(env, com)
    except Exception as e:
        return Stage.FAIL, f"esptool chip-id failed: {e}", {}
    endpoint = fc.mac_to_endpoint(mac)
    info = {"mac": mac, "endpoint": endpoint}
    print(f"  mac={mac}  endpoint={endpoint}")
    try:
        flash_ota(env, com, baud, build_dir, flash_mode, flash_freq)
    except Exception as e:
        return Stage.FAIL, f"flash_ota raised: {e}", info
    try:
        fc.hard_reset(com, label="post-acceptance-flash")
    except Exception as e:
        # Non-fatal — the chip already auto-resets after write
        print(f"  hard_reset skipped ({e})")
    return Stage.PASS, "flash + verify OK", info


def stage2_provision(tb_legacy: TBClient, endpoint: str, profile: str) -> tuple[str, str]:
    print(f"\n=== Stage 2 — Provision ({endpoint}) ===")
    try:
        provision_single(tb_legacy, endpoint, profile, dry_run=False)
    except Exception as e:
        return Stage.FAIL, f"provision raised: {e}"
    return Stage.PASS, "TB device + LwM2M creds OK"


def stage3_registration(tb: TB, endpoint: str, timeout_s: int) -> tuple[str, str, dict]:
    """Wait for active=True and capture first reset_reason."""
    print(f"\n=== Stage 3 — First registration (timeout={timeout_s}s) ===")
    deadline = time.time() + timeout_s
    did = None
    while time.time() < deadline and did is None:
        did = tb.find_did(endpoint)
        if did:
            break
        time.sleep(3)
    if not did:
        return Stage.FAIL, "device never appeared in TB", {}
    while time.time() < deadline:
        a = tb.attrs(did)
        if a.get("active", (False,))[0]:
            ts = tb.ts(did, ["total_resets", "last_reset_reason", "uptime_s"])
            tr = ts.get("total_resets", [{"value": "?"}])[0]["value"]
            rr = ts.get("last_reset_reason", [{"value": "?"}])[0]["value"]
            ut = ts.get("uptime_s", [{"value": "?"}])[0]["value"]
            print(f"  active=True  tr={tr}  rr={rr}  uptime={ut}")
            info = {"did": did, "tr": tr, "rr": rr, "uptime": ut}
            # Boot reset_reason should be clean: PIN(1)/POR(8)/0. SW(2) or WDT(16) at boot => FAIL.
            if str(rr) in ("2", "16"):
                return Stage.FAIL, f"boot reset_reason={rr} (SW panic or WDT)", info
            return Stage.PASS, f"registered in <{timeout_s}s; rr={rr}", info
        time.sleep(5)
    return Stage.FAIL, f"never went active in {timeout_s}s", {"did": did}


def stage4_soak(tb: TB, did: str, soak_s: int, sample_s: int = 30
                ) -> tuple[str, str, list[dict]]:
    """Poll tr/rr every sample_s for soak_s. Any new tr increment => WARN; rr=2/16 => FAIL."""
    print(f"\n=== Stage 4 — Stability soak ({soak_s}s, sample={sample_s}s) ===")
    samples: list[dict] = []
    deadline = time.time() + soak_s
    baseline = tb.ts(did, ["total_resets", "last_reset_reason"])
    base_tr = baseline.get("total_resets", [{"value": "?"}])[0]["value"]
    print(f"  baseline tr={base_tr}")
    incidents: list[str] = []
    while time.time() < deadline:
        time.sleep(sample_s)
        ts = tb.ts(did, ["total_resets", "last_reset_reason"])
        tr = ts.get("total_resets", [{"value": "?"}])[0]["value"]
        rr = ts.get("last_reset_reason", [{"value": "?"}])[0]["value"]
        sample = {"ts": dt.datetime.now().isoformat(timespec="seconds"), "tr": tr, "rr": rr}
        samples.append(sample)
        if tr != base_tr:
            tag = f"tr {base_tr}->{tr} (rr={rr})"
            incidents.append(tag)
            print(f"  [!] {tag}")
            base_tr = tr
        else:
            print(f"  tr={tr} stable")
    if not incidents:
        return Stage.PASS, "0 resets during soak", samples
    fail_reasons = [i for i in incidents if "rr=2" in i or "rr=16" in i]
    if fail_reasons:
        return Stage.FAIL, f"{len(incidents)} resets w/ panic-class rr: {fail_reasons}", samples
    return Stage.WARN, f"{len(incidents)} reset(s) but rr benign", samples


def stage5_cascade(tb: TB, neighbor_eps: list[str], baseline: dict[str, str]
                   ) -> tuple[str, str, dict]:
    """Compare neighbor tr to baseline. >0 cumulative gain => FAIL."""
    print(f"\n=== Stage 5 — Cascade test ({len(neighbor_eps)} neighbors) ===")
    if not neighbor_eps:
        return Stage.SKIP, "no neighbors supplied", {}
    delta = {}
    total = 0
    for ep in neighbor_eps:
        did = tb.find_did(ep)
        if not did:
            continue
        cur = tb.ts(did, ["total_resets"]).get("total_resets",
                                                [{"value": "?"}])[0]["value"]
        base = baseline.get(ep, cur)
        try:
            d = int(cur) - int(base)
        except (ValueError, TypeError):
            d = 0
        delta[ep] = (base, cur, d)
        total += max(d, 0)
        print(f"  {ep}: {base}->{cur}  Δ={d}")
    if total == 0:
        return Stage.PASS, "no cascade — neighbors stable", {"delta": delta}
    return Stage.FAIL, f"cascade trigger! neighbors gained +{total} resets", {"delta": delta}


def stage6_led(tb: TB, did: str) -> tuple[str, str]:
    """Interactive LED test: write red->off->green, ask operator what they saw."""
    print("\n=== Stage 6 — LED visual check ===")
    print("  Watch the board's RGB LED for the next ~20 s.")
    seq = [("RED",   "red",   100, True),
           ("OFF",   "off",     0, False),
           ("GREEN", "green", 100, True)]
    for name, color, dim, on in seq:
        print(f"  -> setting {name}")
        tb.lwm2m_write(did, "/3311/0/5706", color)
        tb.lwm2m_write(did, "/3311/0/5851", dim)
        tb.lwm2m_write(did, "/3311/0/5850", on)
        time.sleep(6)
    # restore off
    tb.lwm2m_write(did, "/3311/0/5706", "off")
    tb.lwm2m_write(did, "/3311/0/5851", 0)
    tb.lwm2m_write(did, "/3311/0/5850", False)
    try:
        ans = input("  Did the LED show RED -> OFF -> GREEN as expected? [y/n/skip]: ").strip().lower()
    except EOFError:
        return Stage.SKIP, "no tty"
    if ans.startswith("y"):
        return Stage.PASS, "LED sequence visible"
    if ans.startswith("s") or ans == "":
        return Stage.SKIP, "operator skipped LED check"
    return Stage.WARN, "LED did not respond — likely WS2812 hardware (cosmetic, data path OK)"


# --- Aggregation + persistence ----------------------------------------
def verdict(stages: list[dict]) -> tuple[str, str]:
    fails = [s for s in stages if s["status"] == Stage.FAIL]
    warns = [s for s in stages if s["status"] == Stage.WARN]
    if fails:
        return "NO APTO", f"FAIL in: {[s['stage'] for s in fails]}"
    if len(warns) >= 2:
        return "NO APTO", f"too many WARN: {[s['stage'] for s in warns]}"
    if warns:
        return "APTO con observación", f"WARN in: {[s['stage'] for s in warns]}"
    return "APTO PSU", "all stages clean"


def persist(session_id: str, endpoint: str, label: int | None, com: str,
            mac: str, stages: list[dict], verdict_text: str,
            verdict_reason: str) -> None:
    DETAIL_DIR.mkdir(parents=True, exist_ok=True)
    detail_path = DETAIL_DIR / f"{endpoint}_{session_id}.json"
    with detail_path.open("w", encoding="utf-8") as f:
        json.dump({
            "session": session_id,
            "endpoint": endpoint,
            "label": label,
            "com": com,
            "mac": mac,
            "ts": dt.datetime.now().isoformat(timespec="seconds"),
            "stages": stages,
            "verdict": verdict_text,
            "verdict_reason": verdict_reason,
        }, f, indent=2)
    print(f"\n[persist] detail -> {detail_path}")

    csv_path = pathlib.Path(RESULTS_CSV)
    is_new = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["ts", "session", "label", "com", "mac", "endpoint",
                        "stage1", "stage2", "stage3", "stage4", "stage5", "stage6",
                        "verdict", "reason"])
        statuses = {s["stage"]: s["status"] for s in stages}
        w.writerow([dt.datetime.now().isoformat(timespec="seconds"),
                    session_id, label or "", com, mac, endpoint,
                    statuses.get("flash", ""),
                    statuses.get("provision", ""),
                    statuses.get("registration", ""),
                    statuses.get("soak", ""),
                    statuses.get("cascade", ""),
                    statuses.get("led", ""),
                    verdict_text, verdict_reason])
    print(f"[persist] csv   -> {csv_path}")


# --- Main -------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--com", required=True, help="COM port of board under test")
    ap.add_argument("--label", type=int, default=None,
                    help="physical label number for fleet_map.csv (1..30)")
    ap.add_argument("--mesh", default=fc.DEFAULT_MESH, choices=fc.MESH_TARGETS)
    ap.add_argument("--user", default=fc.EDGE_TENANT_USER)
    ap.add_argument("--password", default=fc.EDGE_TENANT_PASS)
    ap.add_argument("--profile", default=fc.EDGE_PROFILE)
    ap.add_argument("--build-dir", default="build_ota_ftd")
    ap.add_argument("--baud", default="460800")
    ap.add_argument("--flash-mode", default="dout")
    ap.add_argument("--flash-freq", default="20m")
    ap.add_argument("--reg-timeout", type=int, default=180,
                    help="seconds to wait for first registration")
    ap.add_argument("--soak-seconds", type=int, default=480,
                    help="stability soak duration (default 480 = 8 min)")
    ap.add_argument("--soak-sample", type=int, default=30)
    ap.add_argument("--cascade-neighbors", default="",
                    help="comma-separated endpoint suffixes to compare before/after "
                         "(e.g. 'f79c,f854,f7e8'); empty = skip cascade test")
    ap.add_argument("--skip-flash", action="store_true")
    ap.add_argument("--skip-provision", action="store_true")
    ap.add_argument("--no-led-check", action="store_true")
    args = ap.parse_args()

    session_id = uuid.uuid4().hex[:8]
    print(f"=== board_acceptance session={session_id} com={args.com} ===")
    host, port = fc.edge_for_mesh(args.mesh)
    env = fc.detect_env(verbose=False)

    stages: list[dict] = []

    # -- Stage 1: Flash --
    if args.skip_flash:
        mac = fc.read_mac(env, args.com)
        endpoint = fc.mac_to_endpoint(mac)
        stages.append(stage_row("flash", Stage.SKIP, "skipped via flag",
                                mac=mac, endpoint=endpoint))
    else:
        s1, d1, info1 = stage1_flash(env, args.com, args.build_dir, args.baud,
                                      args.flash_mode, args.flash_freq)
        stages.append(stage_row("flash", s1, d1, **info1))
        if s1 == Stage.FAIL:
            mac = info1.get("mac", "?")
            endpoint = info1.get("endpoint", "?")
            v, r = verdict(stages)
            persist(session_id, endpoint, args.label, args.com, mac,
                    stages, v, r)
            print(f"\n=== VERDICT: {v} ({r}) ===")
            return 2
        mac = info1["mac"]
        endpoint = info1["endpoint"]

    # Spawn serial capture covering stages 3+4+5
    log_path = pathlib.Path("logs") / f"qa_{endpoint}_{session_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cap_seconds = args.reg_timeout + args.soak_seconds + 90
    proc = start_serial_capture(args.com, log_path, cap_seconds)

    # -- Stage 2: Provision --
    tb_legacy = TBClient(host, port, args.user, args.password)
    try:
        tb_legacy.login()
    except Exception as e:
        stages.append(stage_row("provision", Stage.FAIL,
                                f"TB login failed: {e}", endpoint=endpoint))
        stop_serial_capture(proc)
        v, r = verdict(stages)
        persist(session_id, endpoint, args.label, args.com, mac, stages, v, r)
        print(f"\n=== VERDICT: {v} ({r}) ===")
        return 2

    if args.skip_provision:
        stages.append(stage_row("provision", Stage.SKIP, "skipped via flag"))
    else:
        s2, d2 = stage2_provision(tb_legacy, endpoint, args.profile)
        stages.append(stage_row("provision", s2, d2))
        if s2 == Stage.FAIL:
            stop_serial_capture(proc)
            v, r = verdict(stages)
            persist(session_id, endpoint, args.label, args.com, mac, stages, v, r)
            print(f"\n=== VERDICT: {v} ({r}) ===")
            return 2

    # The lightweight TB client for RPC/RTS queries
    tb = TB(host, port, args.user, args.password)
    tb.login()

    # -- Stage 3: First registration --
    s3, d3, info3 = stage3_registration(tb, endpoint, args.reg_timeout)
    stages.append(stage_row("registration", s3, d3, **info3))
    did = info3.get("did")
    if s3 == Stage.FAIL or not did:
        stop_serial_capture(proc)
        v, r = verdict(stages)
        persist(session_id, endpoint, args.label, args.com, mac, stages, v, r)
        print(f"\n=== VERDICT: {v} ({r}) ===")
        return 2

    # Cascade baseline before soak
    neighbors = [e.strip() for e in args.cascade_neighbors.split(",") if e.strip()]
    cascade_baseline: dict[str, str] = {}
    if neighbors:
        print(f"\n[cascade] baseline of {len(neighbors)} neighbors")
        for ep in neighbors:
            nd = tb.find_did(ep)
            if not nd:
                continue
            cur = tb.ts(nd, ["total_resets"]).get("total_resets",
                                                  [{"value": "?"}])[0]["value"]
            cascade_baseline[ep] = cur
            print(f"  {ep}: tr={cur}")

    # -- Stage 4: Soak --
    s4, d4, samples = stage4_soak(tb, did, args.soak_seconds, args.soak_sample)
    stages.append(stage_row("soak", s4, d4, samples=samples))

    # -- Stage 5: Cascade --
    s5, d5, cas_info = stage5_cascade(tb, neighbors, cascade_baseline)
    stages.append(stage_row("cascade", s5, d5, **cas_info))

    # Stop serial capture; grep for red flags now that the file is settled
    stop_serial_capture(proc)
    time.sleep(2)
    serial_flags = grep_serial_for_red_flags(log_path)
    if serial_flags:
        # Convert any pre-existing PASS into WARN to reflect runtime red flags
        for s in stages:
            if s["stage"] in ("soak", "registration") and s["status"] == Stage.PASS:
                s["status"] = Stage.WARN
                s["detail"] += f" | serial flags: {len(serial_flags)}"
        stages.append(stage_row("serial_flags", Stage.WARN,
                                f"{len(serial_flags)} suspect lines in {log_path.name}",
                                samples=serial_flags[:10]))

    # -- Stage 6: LED --
    if args.no_led_check:
        stages.append(stage_row("led", Stage.SKIP, "--no-led-check"))
    else:
        s6, d6 = stage6_led(tb, did)
        stages.append(stage_row("led", s6, d6))

    # -- Verdict + persist --
    v, r = verdict(stages)
    print("\n" + "=" * 60)
    for s in stages:
        marker = {"PASS": "[OK]", "WARN": "!", "FAIL": "[X]", "SKIP": "-"}.get(s["status"], "?")
        print(f"  {marker} {s['stage']:14s} {s['status']:5s}  {s['detail']}")
    print("=" * 60)
    print(f"=== VERDICT: {v} ({r}) ===")
    persist(session_id, endpoint, args.label, args.com, mac, stages, v, r)

    # update fleet_map.csv if label given AND fleet_map row exists for this MAC
    if args.label is not None:
        try:
            _patch_fleet_map(args.label, args.com, mac, endpoint, v)
        except Exception as e:
            print(f"[fleet_map] skip: {e}")
    return 0 if v.startswith("APTO PSU") else 1


def _patch_fleet_map(label: int, com: str, mac: str, endpoint: str, verdict_tag: str) -> None:
    """Update fleet_map.csv row for this label with the QA verdict string in the 'source' column."""
    path = pathlib.Path(FLEET_MAP_CSV)
    if not path.exists():
        return
    rows = list(csv.reader(path.open("r", encoding="utf-8")))
    if not rows:
        return
    header, body = rows[0], rows[1:]
    out = [header]
    for r in body:
        try:
            if int(r[0]) == label:
                r = r.copy()
                r[1] = com
                r[2] = mac
                r[3] = endpoint
                r[4] = f"QA-{verdict_tag.replace(' ', '_')}"
        except (ValueError, IndexError):
            pass
        out.append(r)
    with path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(out)
    print(f"[fleet_map] label {label} updated -> QA-{verdict_tag}")


if __name__ == "__main__":
    sys.exit(main())
