#!/usr/bin/env python3
"""Fase 1 flash orchestrator -- itera tools/bench_assignment.csv y flashea cada
nodo con el build de su cohorte via flash_one.py. Idempotente y resumible.

Workflow:
  1. Coloca los 30 nodos en el USB hub (o serial-by-serial, no importa)
  2. python tools/bench_flash.py --cohort=A   # flashea v0.6.32 a los 15 impares
  3. python tools/bench_flash.py --cohort=B   # flashea v0.6.46 a los 15 pares
  4. python tools/bench_flash.py              # flashea ambos en orden

Notas:
  - Skip automatico de nodos ya flasheados con la version correcta (lee /3/0/3
    de TB para confirmar).
  - Flash mode dout/20m hardcoded (per memory).
  - Si flash_one.py falla en un nodo, registra fallo y continua al siguiente.
  - Output: tools/bench_flash_results.csv con label,com,cohort,version,status,error.
"""
from __future__ import annotations
import argparse, csv, subprocess, sys, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ASSIGN = REPO / "tools" / "bench_assignment.csv"
RESULTS = REPO / "tools" / "bench_flash_results.csv"

BUILD_A = "build_mtd"        # MTD overlay (cohort A: labels impares)
BUILD_B = "build_ota_ftd"    # FTD overlay (cohort B: labels pares)


def already_flashed(endpoint: str, target_ver: str) -> bool:
    """Best-effort skip: MTD vs FTD ambos report 0.6.46 en /3/0/3 (sin distincion).
    Para MTD/FTD A/B no podemos skip por version, asi que devuelve siempre False."""
    return False
    # legacy version-based skip (no usado en MTD/FTD A/B):
    """
    try:
        import requests
        s = requests.Session()
        r = s.post("http://192.168.8.111:8090/api/auth/login",
                   json={"username": "tenant@thingsboard.org", "password": "tenant"},
                   timeout=10)
        s.headers.update({"X-Authorization": f"Bearer {r.json()['token']}"})
        d = s.get("http://192.168.8.111:8090/api/tenant/devices",
                  params={"pageSize": 1, "page": 0, "textSearch": endpoint},
                  timeout=10).json().get("data", [])
        if not d: return False
        did = d[0]["id"]["id"]
        ts = s.get(f"http://192.168.8.111:8090/api/plugins/telemetry/DEVICE/{did}"
                   f"/values/timeseries?keys=firmware_version", timeout=8).json()
        v = (ts.get("firmware_version") or [{}])[0].get("value", "")
        return v == target_ver
    except Exception:
        return False
    """


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cohort", choices=["A","B"], help="solo flashea una cohorte")
    ap.add_argument("--force", action="store_true", help="re-flash aun si version coincide")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--start-label", type=int, default=1)
    ap.add_argument("--end-label", type=int, default=30, help="ultimo label inclusive")
    ap.add_argument("--labels", default=None,
                    help="comma-separated label list, e.g. 1,2,3,4,5,29,30 (overrides start/end)")
    a = ap.parse_args()

    rows = list(csv.DictReader(open(ASSIGN, encoding="utf-8")))
    if a.cohort:
        rows = [r for r in rows if r["cohort"] == a.cohort]
    if a.labels:
        want = {int(x) for x in a.labels.split(",") if x.strip().isdigit()}
        rows = [r for r in rows if int(r["label"]) in want]
    else:
        rows = [r for r in rows if a.start_label <= int(r["label"]) <= a.end_label]
    rows.sort(key=lambda r: int(r["label"]))
    print(f"[flash] {len(rows)} nodos en plan")

    new_csv = not RESULTS.exists()
    out = open(RESULTS, "a", newline="", encoding="utf-8")
    w = csv.writer(out)
    if new_csv:
        w.writerow(["ts","label","com","endpoint","cohort","variant","status","note"])

    for r in rows:
        bdir = BUILD_A if r["cohort"] == "A" else BUILD_B
        variant = r.get("variant") or ("MTD" if r["cohort"] == "A" else "FTD")
        print(f"\n[flash] lab={r['label']} cohort={r['cohort']} variant={variant} "
              f"com={r['com']} endpoint={r['endpoint']}")
        if a.dry_run:
            print(f"  DRY-RUN: flash_one.py --com {r['com']} --build-dir {bdir}")
            continue

        cmd = ["python", "tools/flash_one.py", "--com", r["com"],
               "--build-dir", bdir, "--no-wait-tb", "--force"]
        t0 = time.time()
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        dt = time.time() - t0
        ok = proc.returncode == 0
        note = ("OK" if ok else (proc.stderr or proc.stdout).strip().splitlines()[-1] if (proc.stderr or proc.stdout) else "fail")
        w.writerow([time.strftime("%FT%T"), r["label"], r["com"], r["endpoint"],
                    r["cohort"], variant, "ok" if ok else "fail", note[:200]])
        out.flush()
        print(f"  -> {'OK' if ok else 'FAIL'} in {dt:.0f}s  {note[:120]}")
    out.close()
    print(f"\n[flash] results -> {RESULTS}")


if __name__ == "__main__":
    sys.exit(main() or 0)
