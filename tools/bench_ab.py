#!/usr/bin/env python3
"""Fase 1 - A/B benchmark MTD vs FTD (15+15 nodes, PSU sin USB, 1h soak).

PIVOT: v0.6.32 no compila con Zephyr 4.3 (CONFIG_IMG_BLOCK_BUF_SIZE removed),
asi que el A/B es single-knob sobre HEAD: cambio CONFIG_OPENTHREAD_FTD/MTD via
overlays/med.conf vs overlays/ftd.conf. Aisla quirurgicamente la hipotesis de
que 'all-FTD default' (introducido en v0.6.38) satura la mesh y causa la caida
de active% de ~26/30 a 8-15/30.

Diseno:
  - Cohorte A (15 nodos, labels impares 1..29): HEAD + MTD (MED, child-only)
  - Cohorte B (15 nodos, labels pares 2..30):   HEAD + FTD (router-eligible)
  - Misma Zephyr, misma SDK, mismo source. Solo cambia OPENTHREAD_FTD vs MTD.
  - Misma PSU, misma topologia fisica, ambos quemados con dout/20m
  - Soak 1 h, sample cada 60 s
  - Metricas por cohorte: active%, panic_rate, reset_rate, notify_rate
  - Veredicto: si A > B en active% por >15% sostenido -> all-FTD es la causa.
    Si delta <5% -> all-FTD NO es la regresion, hay que mirar otra cosa.

Fases (idempotentes, resumibles):
  --phase=assign   Genera tools/bench_assignment.csv (no flashea, no toca nodes)
  --phase=verify   Confirma que los 30 nodos respondan en TB tras el flash manual
  --phase=soak     Loop 1h, telemetria a tools/bench_ab_timeseries.csv
  --phase=score    Lee timeseries, emite tools/bench_ab_summary.json + verdicto

Workflow tipico:
  1. python tools/bench_ab.py --phase=assign
  2. # Mover nodos al USB hub, flashear segun assignment (manual o con bulk_flash)
  3. # Mover nodos a la PSU (sin USB)
  4. python tools/bench_ab.py --phase=verify          # espera registracion
  5. python tools/bench_ab.py --phase=soak --duration=3600
  6. python tools/bench_ab.py --phase=score

Notas:
  - Flash mode SIEMPRE dout/20m (per memory: dio/40m soft-brickea BOYA flash)
  - El endpoint LwM2M depende del MAC, NO del firmware. Re-flashear con
    v0.6.32 reusa el mismo endpoint registrado en TB. No hay deprovisioning.
  - v0.6.32 NO tiene Object 33001 ni los counters NVS de v0.6.24+ con los
    mismos nombres -- la registracion en TB sigue funcionando, pero algunas
    telemetrias estaran ausentes en cohorte A. Eso es ESPERADO; el score
    primario es active% que no depende de telemetria fina.
"""
from __future__ import annotations
import argparse, csv, json, sys, time
from pathlib import Path
from datetime import datetime

import requests

REPO = Path(__file__).resolve().parent.parent
ASSIGN_CSV = REPO / "tools" / "bench_assignment.csv"
TIMESERIES_CSV = REPO / "tools" / "bench_ab_timeseries.csv"
SUMMARY_JSON = REPO / "tools" / "bench_ab_summary.json"
FLEET_MAP = REPO / "tools" / "fleet_map.csv"

EDGE_HOST, EDGE_PORT = "192.168.8.111", 8090
USER, PASS = "tenant@thingsboard.org", "tenant"

BIN_MTD = "C:/Users/jsgir/Documents/ESP32/zephyrproject/build_mtd/ami-lwm2m-node/zephyr/zephyr.bin"
BIN_FTD = "C:/Users/jsgir/Documents/ESP32/zephyrproject/build_ota_ftd/ami-lwm2m-node/zephyr/zephyr.bin"

PREFIX = "ami-esp32c6-"


class Edge:
    def __init__(self):
        self.base = f"http://{EDGE_HOST}:{EDGE_PORT}"
        self.s = requests.Session()
        r = self.s.post(f"{self.base}/api/auth/login",
                        json={"username": USER, "password": PASS}, timeout=15)
        r.raise_for_status()
        self.s.headers.update({"X-Authorization": f"Bearer {r.json()['token']}"})

    def list_devices(self):
        """deviceInfos response has 'active' boolean (true=last seen within
        device profile's inactivityTimeout, default 600s) but no
        lastActivityTime. Pull it from attributes endpoint per-device. To
        avoid 30 round trips per sample, return the boolean only and let the
        soak loop fetch lastActivityTime selectively if needed."""
        out, page = [], 0
        while True:
            d = self.s.get(f"{self.base}/api/tenant/deviceInfos",
                           params={"pageSize": 100, "page": page}, timeout=20).json()
            for x in d.get("data", []):
                n = x.get("name", "")
                if n.startswith(PREFIX):
                    out.append({"name": n, "did": x["id"]["id"],
                                "active": x.get("active", False)})
            if not d.get("hasNext"): break
            page += 1
        return out

    def telem(self, did, keys):
        try:
            r = self.s.get(f"{self.base}/api/plugins/telemetry/DEVICE/{did}/values/timeseries",
                           params={"keys": ",".join(keys)}, timeout=10).json()
            return {k: (v[0].get("value") if v else None) for k, v in r.items()}
        except Exception:
            return {}


def cmd_assign(_args):
    """Stratified 15/15 split by label parity (odd=MTD A, even=FTD B)."""
    rows = []
    with open(FLEET_MAP, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            lab = int(r["label"]) if r["label"].isdigit() else None
            if lab is None: continue
            cohort = "A" if (lab % 2 == 1) else "B"
            binp = BIN_MTD if cohort == "A" else BIN_FTD
            variant = "MTD" if cohort == "A" else "FTD"
            rows.append({"label": lab, "com": r["com"], "mac": r["mac"],
                         "endpoint": r["endpoint"], "cohort": cohort,
                         "variant": variant, "binary": binp})
    rows.sort(key=lambda x: x["label"])
    with open(ASSIGN_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["label","com","mac","endpoint","cohort","variant","binary"])
        w.writeheader(); w.writerows(rows)

    a = [r for r in rows if r["cohort"] == "A"]
    b = [r for r in rows if r["cohort"] == "B"]
    print(f"[assign] {len(a)} -> A (HEAD-MTD), {len(b)} -> B (HEAD-FTD)")
    print(f"[assign] A labels: {[r['label'] for r in a]}")
    print(f"[assign] B labels: {[r['label'] for r in b]}")
    print(f"[assign] saved -> {ASSIGN_CSV}")
    missing = [b_ for b_ in (BIN_MTD, BIN_FTD) if not Path(b_).exists()]
    if missing:
        print(f"[assign] WARN: missing binaries: {missing}")
    return 0


def _load_assign():
    if not ASSIGN_CSV.exists():
        print(f"[err] {ASSIGN_CSV} missing -- run --phase=assign first"); sys.exit(2)
    with open(ASSIGN_CSV, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def cmd_verify(args):
    """Poll TB Edge until 28/30 endpoints are 'active' (TB inactivityTimeout default 600s)."""
    assign = _load_assign()
    e = Edge()
    deadline = time.time() + args.wait
    while time.time() < deadline:
        devs = {d["name"]: d for d in e.list_devices()}
        seen, active = [], []
        for r in assign:
            d = devs.get(r["endpoint"])
            if d:
                seen.append(r["endpoint"])
                if d["active"]: active.append(r["endpoint"])
        a_act = [r for r in assign if r["cohort"]=="A" and r["endpoint"] in active]
        b_act = [r for r in assign if r["cohort"]=="B" and r["endpoint"] in active]
        print(f"[verify] seen={len(seen)}/30  active={len(active)}/30  "
              f"A={len(a_act)}/15  B={len(b_act)}/15")
        if len(active) >= 28:
            print(f"[verify] OK -- proceeding"); return 0
        time.sleep(30)
    print("[verify] timeout -- proceed anyway?"); return 0


def cmd_soak(args):
    assign = _load_assign()
    by_ep = {r["endpoint"]: r for r in assign}
    e = Edge()

    keys = ["total_resets","last_reset_reason","uptime_s","notify_emitted",
            "reg_attempts","reg_success","recover_count","watchdog_count"]

    # Pre-snapshot per node (baseline for delta-counters)
    devs = {d["name"]: d for d in e.list_devices() if d["name"] in by_ep}
    baseline = {}
    for ep, d in devs.items():
        t = e.telem(d["did"], keys)
        baseline[ep] = t
    print(f"[soak] baseline captured for {len(baseline)} devices")

    new_csv = not TIMESERIES_CSV.exists()
    f = open(TIMESERIES_CSV, "a", newline="", encoding="utf-8")
    w = csv.writer(f)
    if new_csv:
        w.writerow(["ts","elapsed_s","endpoint","cohort","label","active",
                    "total_resets","rr","uptime_s","notify_emitted","reg_attempts",
                    "reg_success","recover_count","watchdog_count"])

    t_start = time.time()
    sample_every = args.interval
    while time.time() - t_start < args.duration:
        loop_t = time.time()
        devs = {d["name"]: d for d in e.list_devices() if d["name"] in by_ep}
        elapsed = int(time.time() - t_start)
        agg = {"A": {"act":0,"tr":0,"ne":0}, "B": {"act":0,"tr":0,"ne":0}}
        for ep, d in devs.items():
            t = e.telem(d["did"], keys)
            active = 1 if d["active"] else 0
            row = [datetime.now().isoformat(timespec="seconds"), elapsed, ep,
                   by_ep[ep]["cohort"], by_ep[ep]["label"], active,
                   t.get("total_resets"), t.get("last_reset_reason"),
                   t.get("uptime_s"), t.get("notify_emitted"),
                   t.get("reg_attempts"), t.get("reg_success"),
                   t.get("recover_count"), t.get("watchdog_count")]
            w.writerow(row); f.flush()
            c = by_ep[ep]["cohort"]
            agg[c]["act"] += active
            try: agg[c]["tr"] += int(t.get("total_resets") or 0)
            except: pass
            try: agg[c]["ne"] += int(t.get("notify_emitted") or 0)
            except: pass
        print(f"[t+{elapsed:5d}s] A act={agg['A']['act']:2d}/15 tr={agg['A']['tr']:4d} ne={agg['A']['ne']:6d}"
              f"   B act={agg['B']['act']:2d}/15 tr={agg['B']['tr']:4d} ne={agg['B']['ne']:6d}")
        sleep = sample_every - (time.time() - loop_t)
        if sleep > 0: time.sleep(sleep)
    f.close()
    print(f"[soak] timeseries -> {TIMESERIES_CSV}")
    return 0


def cmd_score(_args):
    if not TIMESERIES_CSV.exists():
        print(f"[err] no timeseries -- run --phase=soak first"); return 2
    rows = list(csv.DictReader(open(TIMESERIES_CSV, encoding="utf-8")))
    if not rows:
        print("[score] empty timeseries"); return 2

    # Group by elapsed_s
    samples = {}
    for r in rows:
        t = int(r["elapsed_s"])
        samples.setdefault(t, []).append(r)
    times = sorted(samples)

    def cohort_stats(cohort):
        per_t = []
        for t in times:
            xs = [r for r in samples[t] if r["cohort"] == cohort]
            act = sum(int(r["active"]) for r in xs)
            n = len(xs) or 1
            per_t.append({"t": t, "active": act, "n": n, "active_pct": 100*act/n})
        return per_t

    sa, sb = cohort_stats("A"), cohort_stats("B")
    avg_act_a = sum(x["active_pct"] for x in sa)/len(sa) if sa else 0
    avg_act_b = sum(x["active_pct"] for x in sb)/len(sb) if sb else 0

    # Reset rate: max(total_resets) - min(total_resets) summed per cohort, per hour
    def reset_rate(cohort):
        per_ep_min, per_ep_max = {}, {}
        for r in rows:
            if r["cohort"] != cohort: continue
            ep = r["endpoint"]
            tr = int(r["total_resets"]) if r["total_resets"] and r["total_resets"].isdigit() else None
            if tr is None: continue
            per_ep_min.setdefault(ep, tr); per_ep_min[ep] = min(per_ep_min[ep], tr)
            per_ep_max.setdefault(ep, tr); per_ep_max[ep] = max(per_ep_max[ep], tr)
        deltas = sum(per_ep_max[ep] - per_ep_min[ep] for ep in per_ep_min)
        hours = (times[-1] - times[0]) / 3600.0 if len(times) > 1 else 1
        return {"total_delta": deltas, "rate_per_hour": deltas/hours}

    rra, rrb = reset_rate("A"), reset_rate("B")
    delta_pp = avg_act_a - avg_act_b
    verdict = (
        "REGRESSION_CONFIRMED" if delta_pp > 15 else
        "NO_CLEAR_REGRESSION" if abs(delta_pp) <= 5 else
        "B_BETTER" if delta_pp < -15 else
        "INCONCLUSIVE"
    )

    summary = {
        "samples": len(times), "duration_s": times[-1]-times[0] if times else 0,
        "cohort_A_MTD": {"avg_active_pct": round(avg_act_a, 1),
                         "reset_total": rra["total_delta"],
                         "reset_per_hour": round(rra["rate_per_hour"], 1)},
        "cohort_B_FTD": {"avg_active_pct": round(avg_act_b, 1),
                         "reset_total": rrb["total_delta"],
                         "reset_per_hour": round(rrb["rate_per_hour"], 1)},
        "delta_active_pct_A_minus_B": round(delta_pp, 1),
        "verdict": verdict,
    }
    SUMMARY_JSON.write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    print(f"\n[score] saved -> {SUMMARY_JSON}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["assign","verify","soak","score"])
    ap.add_argument("--duration", type=int, default=3600, help="soak duration s (default 3600)")
    ap.add_argument("--interval", type=int, default=60, help="soak sample interval s")
    ap.add_argument("--wait", type=int, default=600, help="verify max wait s")
    args = ap.parse_args()
    return {"assign": cmd_assign, "verify": cmd_verify,
            "soak": cmd_soak, "score": cmd_score}[args.phase](args)


if __name__ == "__main__":
    sys.exit(main())
