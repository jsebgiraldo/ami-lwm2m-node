#!/usr/bin/env python3
"""v0.6.24 post-powercycle soak collector.

Every SAMPLE_INTERVAL_S seconds, snapshot per-device LwM2M state +
Pi4-side mesh/firewall counters into tools/soak_v0624.csv. Runs until
killed.

Per-device columns (one row per (timestamp, endpoint) pair):
  ts                    Unix seconds at snapshot
  endpoint              ami-esp32c6-XXXX
  active                True/False (TB Edge SERVER_SCOPE/active)
  last_activity_age_s   now - lastActivityTime (seconds)
  watchdog_count        Object 33000 RID 19 — persisted in NVS
  recover_count         Object 33000 RID 15 — persisted in NVS
  reg_attempts          Object 33000 RID 11 — persisted in NVS
  reg_success           Object 33000 RID 12 — persisted in NVS
  total_resets          Object 33000 RID 22 — persisted in NVS
  uptime_s              Object 33000 RID 10
  last_reset_reason     Object 33000 RID 21 — hwinfo bitmap
  thread_role           Object 33000 RID 0 — Disabled/Detached/Child/Router/Leader

Per-snapshot global columns (same row repeated for every device):
  mesh_children         Pi4 OTBR child table count
  nft_pkts              accept_from_thread total packets
  pi4_edge_cpu_pct      docker stats CPU %

Save with append. Re-running picks up the same CSV.
"""
import sys
import time
import csv
import datetime
import requests
import paramiko

SAMPLE_INTERVAL_S = 90
CSV_PATH = "tools/soak_v0624.csv"

PI4_HOST = "192.168.8.111"
PI4_USER = "root"
PI4_PASS = "root"
TB_BASE  = f"http://{PI4_HOST}:8090"
TB_USER  = "tenant@thingsboard.org"
TB_PASS  = "tenant"

KEYS = [
    "watchdog_count","recover_count","reg_attempts","reg_success",
    "total_resets","uptime_s","last_reset_reason","thread_role",
]


def open_pi4():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(PI4_HOST, username=PI4_USER, password=PI4_PASS, timeout=8)
    return c


def pi4_run(c, cmd, timeout=15):
    _, o, _ = c.exec_command(cmd, timeout=timeout)
    return o.read().decode(errors="replace").strip()


def tb_login(s):
    r = s.post(f"{TB_BASE}/api/auth/login",
               json={"username": TB_USER, "password": TB_PASS}, timeout=10)
    r.raise_for_status()
    s.headers.update({"X-Authorization": f"Bearer {r.json()['token']}"})


def main():
    print(f"[soak] writing {CSV_PATH}, interval={SAMPLE_INTERVAL_S}s", flush=True)
    pi4 = open_pi4()
    s = requests.Session()
    tb_login(s)

    cols = (["ts","endpoint","active","last_activity_age_s"]
            + KEYS
            + ["mesh_children","nft_pkts","pi4_edge_cpu_pct"])
    try:
        with open(CSV_PATH, "x", newline="") as f:
            csv.writer(f).writerow(cols)
        print(f"[soak] new CSV, wrote header ({len(cols)} cols)", flush=True)
    except FileExistsError:
        print(f"[soak] CSV exists, appending", flush=True)

    while True:
        t0 = time.time()
        ts_iso = datetime.datetime.now().isoformat(timespec='seconds')
        try:
            # Pi4-side globals
            mesh = pi4_run(pi4, "ot-ctl child table 2>&1 | grep -c '^|'")
            try:
                mesh_n = int(mesh)
            except Exception:
                mesh_n = -1
            nft_line = pi4_run(pi4, "nft list chain inet fw4 accept_from_thread | grep counter")
            nft_pkts = -1
            for tok in nft_line.split():
                if tok.isdigit():
                    nft_pkts = int(tok); break
            cpu_line = pi4_run(pi4, "docker stats --no-stream pi4-edge-v2 --format '{{.CPUPerc}}'")
            cpu_pct = cpu_line.rstrip("%") if cpu_line.endswith("%") else cpu_line

            # TB Edge per-device
            try:
                tb_login(s)  # refresh token each cycle
                r = s.get(f"{TB_BASE}/api/tenant/devices?pageSize=50&page=0&textSearch=ami-esp32c6-", timeout=10)
                devices = sorted(r.json()["data"], key=lambda x: x["name"])
            except Exception as e:
                print(f"[soak] {ts_iso} TB Edge fetch failed: {e}", flush=True)
                time.sleep(SAMPLE_INTERVAL_S)
                continue
            now = time.time()

            rows = []
            active_n = 0
            for d in devices:
                did = d["id"]["id"]
                try:
                    rr = s.get(
                        f"{TB_BASE}/api/plugins/telemetry/DEVICE/{did}/values/attributes/SERVER_SCOPE",
                        timeout=10)
                    attrs = {a["key"]: a["value"] for a in rr.json()}
                    la = attrs.get("lastActivityTime", 0) / 1000
                    age = int(now - la) if la else -1
                    is_active = bool(attrs.get("active", False))
                    if is_active: active_n += 1

                    rr2 = s.get(
                        f"{TB_BASE}/api/plugins/telemetry/DEVICE/{did}/values/timeseries?keys="
                        + ",".join(KEYS), timeout=10)
                    vals = {k: v[0]["value"] for k, v in rr2.json().items()}
                except Exception as e:
                    print(f"[soak] {ts_iso} device {d['name']} fetch failed: {e}", flush=True)
                    continue

                row = [int(t0), d["name"], is_active, age]
                for k in KEYS:
                    row.append(vals.get(k, ""))
                row += [mesh_n, nft_pkts, cpu_pct]
                rows.append(row)

            with open(CSV_PATH, "a", newline="") as f:
                csv.writer(f).writerows(rows)

            elapsed = time.time() - t0
            print(f"[soak] {ts_iso}  active={active_n}/{len(devices)}  mesh={mesh_n}  "
                  f"nft={nft_pkts}  cpu={cpu_pct}%  rows+={len(rows)}  ({elapsed:.1f}s)",
                  flush=True)
        except Exception as e:
            print(f"[soak] {ts_iso} ERROR: {e}", flush=True)

        sleep_for = max(5, SAMPLE_INTERVAL_S - (time.time() - t0))
        time.sleep(sleep_for)


if __name__ == "__main__":
    sys.exit(main())
