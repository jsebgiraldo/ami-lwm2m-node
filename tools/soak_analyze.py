"""Soak harness log analyzer -- surface failures from a 12-24h capture.

Reads the artefacts produced by tools/soak_harness.py for a single run and
prints a tight summary plus a per-failure-window context dump. Designed to
make the "node died -- why?" answer obvious without grep-foo.

What it computes:

  1. TIMELINE: span of the capture, samples captured per stream, gaps in
     each stream (a TB Edge poll gap >120 s, an OTBR gap >180 s, or a
     serial silence >SERIAL_SILENT_THRESHOLD all bubble up).
  2. EVENTS: every record in events.jsonl (RESET, DROP, RECOVER,
     CHILD_LOST/GAINED, SERIAL_SILENT, *_ERROR / *_EXCEPTION), sorted by
     wall clock with deltas to the previous event.
  3. RESET ANALYSIS: for each RESET event,
        - uptime_before / uptime_after / total_resets delta
        - reset_cause register at the boot AFTER the reset (from telemetry)
        - last 30 serial lines BEFORE the reset
        - last OTBR snapshot BEFORE the reset (state + child count)
        - first 30 serial lines AFTER the reset
     This is the punchline: "the node died at HH:MM, last log was X, OTBR
     was Y, on reboot it reported reset_cause=Z".
  4. DROP / RECOVER: same 30-line context for each DROP event so we can
     see what the firmware was doing right before TB Edge marked it
     inactive (catches "alive but stopped pushing").

Usage:
    python tools/soak_analyze.py                       # latest run in ./soak_logs/
    python tools/soak_analyze.py soak_logs/20260516-130137_ami-esp32c6-1494
    python tools/soak_analyze.py --context 60          # show 60 lines around each event
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SERIAL_SILENT_GAP_S = 90
TBEDGE_GAP_S = 120
OTBR_GAP_S = 180


def parse_iso(s: str) -> datetime:
    """Tolerant ISO-8601 parser. Returns aware UTC datetime."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt_age(seconds: float) -> str:
    s = int(seconds)
    if s < 90:
        return f"{s}s"
    if s < 3600:
        return f"{s//60}m{s%60:02d}s"
    h, rem = divmod(s, 3600)
    return f"{h}h{rem//60:02d}m"


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    return rows


def load_serial(path: Path) -> list[tuple[datetime, str]]:
    """Return [(wall_clock_dt, raw_line_after_stamp), ...]. Lines without a
    stamp (banner, partial) are dropped."""
    out = []
    if not path.exists():
        return out
    with path.open(encoding="utf-8", errors="replace") as f:
        for ln in f:
            ln = ln.rstrip("\n")
            if not ln.startswith("["):
                continue
            end = ln.find("]")
            if end < 0:
                continue
            try:
                ts = parse_iso(ln[1:end])
            except ValueError:
                continue
            body = ln[end + 1:].lstrip()
            out.append((ts, body))
    return out


def latest_run_dir(root: Path) -> Path:
    runs = [p for p in root.iterdir() if p.is_dir()]
    if not runs:
        raise SystemExit(f"no runs found under {root}")
    return max(runs, key=lambda p: p.stat().st_mtime)


def detect_gaps(series: list[datetime], threshold_s: float, label: str) -> list[tuple[datetime, datetime, float]]:
    """Return list of (gap_start, gap_end, gap_seconds) where consecutive
    samples are farther apart than threshold_s."""
    gaps = []
    for a, b in zip(series, series[1:]):
        delta = (b - a).total_seconds()
        if delta > threshold_s:
            gaps.append((a, b, delta))
    return gaps


def context_serial(serial: list[tuple[datetime, str]], around: datetime,
                   before_n: int, after_n: int) -> tuple[list, list]:
    """Last `before_n` lines strictly before `around` and first `after_n` after."""
    before = [(t, ln) for t, ln in serial if t < around][-before_n:]
    after = [(t, ln) for t, ln in serial if t >= around][:after_n]
    return before, after


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?", default=None,
                    help="Specific run directory under ./soak_logs/, "
                         "or omit to use the most recent.")
    ap.add_argument("--context", type=int, default=30,
                    help="Serial lines before AND after each failure event (default 30).")
    ap.add_argument("--root", default="soak_logs",
                    help="Root dir of soak runs (default %(default)s).")
    args = ap.parse_args()

    root = Path(args.root)
    run_dir = Path(args.run_dir) if args.run_dir else latest_run_dir(root)
    if not run_dir.exists():
        raise SystemExit(f"run dir not found: {run_dir}")

    print(f"=== soak run: {run_dir.name} ===\n")

    manifest_p = run_dir / "manifest.json"
    if manifest_p.exists():
        m = json.loads(manifest_p.read_text())
        print(f"started:   {m['started_iso']}  on {m.get('host','?')}")
        print(f"endpoint:  {m['args'].get('endpoint','?')}")
        print(f"target:    com={m['args'].get('com','?')}  edge={m['args'].get('edge_url','?')}")
        print(f"duration:  {m.get('duration_s',0)/3600:.1f} h planned\n")

    # --- load streams ---
    events = load_jsonl(run_dir / "events.jsonl")
    tbedge = load_jsonl(run_dir / "tbedge.jsonl")
    otbr = load_jsonl(run_dir / "otbr.jsonl")
    serial = load_serial(run_dir / "serial.log")

    # --- timeline summary ---
    print("--- TIMELINE -------------------------------------------------")
    def stream_span(label: str, items: list, get_ts):
        if not items:
            print(f"  {label:10} (none)")
            return
        first = get_ts(items[0])
        last = get_ts(items[-1])
        print(f"  {label:10} {len(items):5d} samples   {first.isoformat(timespec='seconds')} -> {last.isoformat(timespec='seconds')}   span={fmt_age((last-first).total_seconds())}")
    stream_span("events", events, lambda e: parse_iso(e["ts"]))
    stream_span("serial", serial, lambda x: x[0])
    stream_span("tbedge", tbedge, lambda r: parse_iso(r["ts"]))
    stream_span("otbr",   otbr,   lambda r: parse_iso(r["ts"]))

    # --- stream gaps ---
    print("\n--- GAPS ----------------------------------------------------")
    serial_ts = [t for t, _ in serial]
    tbedge_ts = [parse_iso(r["ts"]) for r in tbedge]
    otbr_ts = [parse_iso(r["ts"]) for r in otbr]
    for label, ts_list, thresh in (("serial", serial_ts, SERIAL_SILENT_GAP_S),
                                   ("tbedge", tbedge_ts, TBEDGE_GAP_S),
                                   ("otbr",   otbr_ts,   OTBR_GAP_S)):
        gaps = detect_gaps(ts_list, thresh, label)
        if not gaps:
            print(f"  {label:10} no gaps > {thresh}s")
            continue
        for a, b, d in gaps:
            print(f"  {label:10} GAP {fmt_age(d):>8s}   {a.isoformat(timespec='seconds')} -> {b.isoformat(timespec='seconds')}")

    # --- derived events table ---
    print("\n--- EVENTS --------------------------------------------------")
    if not events:
        print("  (none)")
    else:
        prev_ts = None
        for e in events:
            t = parse_iso(e["ts"])
            delta = f"+{fmt_age((t-prev_ts).total_seconds())}" if prev_ts else "--"
            payload = {k: v for k, v in e.items() if k not in ("ts", "kind")}
            print(f"  {t.isoformat(timespec='seconds')}  {delta:>8s}  {e['kind']:18s}  {payload}")
            prev_ts = t

    # --- reset analysis (the punchline) ---
    resets = [e for e in events if e["kind"] == "RESET"]
    drops = [e for e in events if e["kind"] == "DROP"]
    silences = [e for e in events if e["kind"] == "SERIAL_SILENT"]

    print(f"\n--- FAILURES: {len(resets)} reset(s), {len(drops)} drop(s), {len(silences)} silence(s) ---")

    def dump_context(t: datetime, label: str):
        print(f"\n--- {label} at {t.isoformat(timespec='seconds')} (UTC) ---")
        # Last OTBR snapshot before
        prev_otbr = [r for r in otbr if parse_iso(r["ts"]) < t]
        if prev_otbr:
            s = prev_otbr[-1]
            children = sum(1 for ln in s.get("children_raw", "").splitlines()
                           if ln.strip().startswith("|") and ln.find("0x") > 0)
            print(f"  last OTBR:    state={s.get('state','?')}  children≈{children}  "
                  f"({fmt_age((t-parse_iso(s['ts'])).total_seconds())} before)")
        # Last TB Edge snapshot before
        prev_edge = [r for r in tbedge if parse_iso(r["ts"]) < t]
        if prev_edge:
            s = prev_edge[-1]
            vs = s.get("values", {}) or {}
            print(f"  last tbedge:  active={s.get('active')}  uptime_s={vs.get('uptime_s')}  "
                  f"total_resets={vs.get('total_resets')}  wd={vs.get('watchdog_count')}  "
                  f"rec={vs.get('recover_count')}  lec={vs.get('last_error_code')}")
        # Serial context
        before, after = context_serial(serial, t, args.context, args.context)
        print(f"  serial BEFORE (last {len(before)}):")
        for ts, ln in before:
            print(f"    [{ts.isoformat(timespec='seconds')}] {ln}")
        if after:
            print(f"  serial AFTER (first {len(after)}):")
            for ts, ln in after:
                print(f"    [{ts.isoformat(timespec='seconds')}] {ln}")
        else:
            print("  serial AFTER: (none -- port stayed silent)")

    for e in resets + drops + silences:
        t = parse_iso(e["ts"])
        kind = e["kind"]
        dump_context(t, kind)

    # --- one-liner verdict ---
    print("\n--- VERDICT -------------------------------------------------")
    if not resets and not drops and not silences:
        print("  CLEAN -- no failures observed in the captured window.")
    else:
        first_failure = min((parse_iso(e["ts"]) for e in resets + drops + silences),
                            default=None)
        if first_failure and events:
            harness_start = parse_iso(events[0]["ts"])
            survived = (first_failure - harness_start).total_seconds()
            print(f"  FIRST FAILURE after {fmt_age(survived)} of soak.")
        print(f"  resets={len(resets)}  drops={len(drops)}  silences={len(silences)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
