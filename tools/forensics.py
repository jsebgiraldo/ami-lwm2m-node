#!/usr/bin/env python3
"""Fleet forensics: pull per-node diagnostics + session history and rank by health.

For every ami-esp32c6-* device it gathers:
  - session: active, last connect/disconnect, current session age, # of recent
    disconnects (from server attrs)
  - stability: uptime_s, total_resets, watchdog_count, recover_count,
    last_reset_reason, reg_attempts vs reg_success, restart_success,
    storm_backoff_applied
  - RF/MAC: thread_role, mac_tx_total, mac_tx_err_abort (TX abort ratio),
    mac_rx_total, mac_rx_err_no_frame
A node is flagged when any of: inactive, short session (<1 lifetime), high
TX-abort ratio, reg_attempts>>reg_success, watchdog/recover/reset churn.

Usage: python tools/forensics.py [--lifetime 300]
"""
from __future__ import annotations
import argparse, sys, datetime as dt
import fleet_common as fc
fc.bootstrap_venv(); sys.path.insert(0, str(fc.TOOLS_DIR))
from ota_push_direct import Edge

PREFIX = "ami-esp32c6-"
TKEYS = ["uptime_s", "total_resets", "watchdog_count", "recover_count",
         "last_reset_reason", "reg_attempts", "reg_success", "restart_success",
         "storm_backoff_applied", "thread_role", "mac_tx_total", "mac_tx_err_abort",
         "mac_rx_total", "mac_rx_err_no_frame", "notify_emitted", "notify_throttled",
         "thread_partition_id"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lifetime", type=int, default=300)
    a = ap.parse_args()
    h, p = fc.edge_for_mesh(fc.DEFAULT_MESH)
    e = Edge(h, p, fc.EDGE_TENANT_USER, fc.EDGE_TENANT_PASS)
    now = dt.datetime.now(dt.timezone.utc)

    devs, page = [], 0
    while True:
        d = e.s.get(f"{e.base}/api/tenant/deviceInfos",
                    params={"pageSize": 100, "page": page}, timeout=20).json()
        devs += [x for x in d.get("data", []) if x.get("name", "").startswith(PREFIX)]
        if not d.get("hasNext"):
            break
        page += 1
    devs.sort(key=lambda x: x["name"])

    rows = []
    for d in devs:
        nm, did, act = d["name"][len(PREFIX):], d["id"]["id"], d.get("active")
        sa = {x["key"]: x["value"] for x in e.s.get(
            f"{e.base}/api/plugins/telemetry/DEVICE/{did}/values/attributes/SERVER_SCOPE",
            timeout=15).json()}
        tel = e.s.get(f"{e.base}/api/plugins/telemetry/DEVICE/{did}/values/timeseries",
                      params={"keys": ",".join(TKEYS)}, timeout=15).json()
        t = {k: (tel[k][0]["value"] if k in tel and tel[k] else None) for k in TKEYS}

        def age_h(k):
            v = sa.get(k)
            return (now - dt.datetime.fromtimestamp(v/1000, dt.timezone.utc)).total_seconds()/3600 if v else None
        lc, ld = sa.get("lastConnectTime"), sa.get("lastDisconnectTime")
        sess_s = (ld - lc)/1000 if (lc and ld and ld >= lc) else None  # last closed session len
        rows.append((nm, act, age_h("lastActivityTime"), sess_s, t))

    # telemetry arrives as strings -> coerce
    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0
    def tx_abort_ratio(t):
        tot, ab = num(t.get("mac_tx_total")), num(t.get("mac_tx_err_abort"))
        return (ab/tot*100) if tot else 0.0

    print(f"{'node':<6}{'act':<5}{'lastAct':>8}{'sess_s':>8}{'up_s':>7}{'rst':>4}"
          f"{'wd':>3}{'rcv':>4}{'rega':>5}{'regs':>5}{'role':>9}{'txtot':>7}{'txab':>6}{'txab%':>7} flags")
    print("-" * 104)
    troubled = []
    for nm, act, la, sess, t in rows:
        rega, regs = num(t.get("reg_attempts")), num(t.get("reg_success"))
        txr = tx_abort_ratio(t)
        flags = []
        if not act: flags.append("INACTIVE")
        if sess is not None and sess < a.lifetime * 1.2: flags.append(f"short-sess({sess:.0f}s)")
        if txr >= 5: flags.append(f"tx-abort{txr:.0f}%")
        if rega and regs and rega - regs >= 3: flags.append(f"reg{rega:.0f}>{regs:.0f}")
        if num(t.get("watchdog_count")) > 0: flags.append(f"wd{num(t.get('watchdog_count')):.0f}")
        if num(t.get("recover_count")) >= 2: flags.append(f"rcv{num(t.get('recover_count')):.0f}")
        if num(t.get("total_resets")) >= 10: flags.append(f"rst{num(t.get('total_resets')):.0f}")
        if flags: troubled.append(nm)
        print(f"{nm:<6}{str(act):<5}{(f'{la:.1f}h' if la is not None else '-'):>8}"
              f"{(f'{sess:.0f}' if sess is not None else '-'):>8}"
              f"{str(t.get('uptime_s') or '-'):>7}{str(t.get('total_resets') or 0):>4}"
              f"{str(t.get('watchdog_count') or 0):>3}{str(t.get('recover_count') or 0):>4}"
              f"{str(rega or '-'):>5}{str(regs or '-'):>5}{str(t.get('thread_role') or '-'):>9}"
              f"{str(t.get('mac_tx_total') or '-'):>7}{str(t.get('mac_tx_err_abort') or '-'):>6}"
              f"{txr:>6.1f} {' '.join(flags)}")
    print("-" * 104)
    print(f"Total {len(rows)} | flagged {len(troubled)}: {', '.join(troubled)}")


if __name__ == "__main__":
    main()
