#!/usr/bin/env python3
"""Provision the 'AMI Comms' Grafana dashboard on the Pi4 (192.168.8.111).

Tier 1 of the message-metrics observability: how many messages arrive at TB Edge,
how often, and per node — read straight from TB Edge's Postgres ts_kv (each row =
one telemetry value received). Grafana + the TB-Edge-Postgres datasource already
run on the host; this fixes the datasource DB (-> thingsboard_edge) and POSTs the
dashboard via the Grafana HTTP API (curl on the host's localhost:3000).

Key_ids (key_dictionary): activePower=73, voltage=70, frequency=86, activeEnergy=85,
uptime_s=69, notify_emitted=93. Queries filter k.key=<id> DIRECTLY (no key_dictionary
join) — that turned a 27s scan into 0.3s by hitting the (entity_id,key,ts) index.

Usage:  python tools/grafana_setup.py
"""
from __future__ import annotations
import json
import sys

import fleet_common as fc
fc.bootstrap_venv()
import paramiko  # noqa: E402

HOST, USER, PW = "192.168.8.111", "root", "root"
GRAFANA = "http://localhost:3000"
DS_UID = "tb-edge-postgres"
LIKE = "ami-esp32c6-%"

def ds(): return {"type": "grafana-postgresql-datasource", "uid": DS_UID}

def panel(pid, title, x, y, w, h, ptype, sql, fmt, extra=None):
    p = {"id": pid, "title": title, "type": ptype,
         "gridPos": {"x": x, "y": y, "w": w, "h": h},
         "datasource": ds(),
         "targets": [{"refId": "A", "format": fmt, "rawSql": sql,
                      "datasource": ds()}]}
    if extra:
        p.update(extra)
    return p

# ---- panel SQL ----
# NOTE: $__unixEpochFrom() expands to a bare integer literal (~1.75e9). Postgres
# types it int4, and int4*1000 OVERFLOWS (max 2.1e9) -> "integer out of range" ->
# every panel shows No Data. Cast to bigint BEFORE multiplying. k.ts stays raw so
# the (entity_id,key,ts) index is still used.
TIMEFILT = "k.ts BETWEEN ($__unixEpochFrom()::bigint*1000) AND ($__unixEpochTo()::bigint*1000)"
# Node selector: the $node template variable holds the selected device name(s);
# 'All' expands to every ami node. Replaces the static LIKE scope.
NODEFILT = "d.name IN ($node)"

P1 = (f"SELECT date_trunc('minute', to_timestamp(k.ts/1000)) AS \"time\", "
      f"d.name AS metric, count(*)::int AS value "
      f"FROM ts_kv k JOIN device d ON d.id=k.entity_id "
      f"WHERE k.key=73 AND {NODEFILT} AND {TIMEFILT} "
      f"GROUP BY 1,2 ORDER BY 1")

P2 = (f"SELECT date_trunc('minute', to_timestamp(k.ts/1000)) AS \"time\", "
      f"d.name AS metric, count(*)::int AS value "
      f"FROM ts_kv k JOIN device d ON d.id=k.entity_id "
      f"WHERE {NODEFILT} AND k.key >= 60 AND {TIMEFILT} "
      f"GROUP BY 1,2 ORDER BY 1")

P3 = (f"SELECT d.name AS \"Nodo\", to_timestamp(max(l.ts)/1000) AS \"Ultima recepcion\", "
      f"round(extract(epoch from now()) - max(l.ts)/1000.0)::int AS \"Hace (s)\" "
      f"FROM ts_kv_latest l JOIN device d ON d.id=l.entity_id "
      f"WHERE {NODEFILT} GROUP BY d.name ORDER BY 3")

P4 = (f"SELECT kd.key AS \"Recurso\", count(*)::int AS \"Mensajes (1h)\" "
      f"FROM ts_kv k JOIN key_dictionary kd ON kd.key_id=k.key "
      f"JOIN device d ON d.id=k.entity_id "
      f"WHERE {NODEFILT} AND k.ts > (extract(epoch from now())*1000 - 3600000) "
      f"GROUP BY kd.key ORDER BY 2 DESC LIMIT 20")

# Tier 2: estimated bandwidth. Each AMI LwM2M observe-notify is ~45 bytes on the
# wire (CoAP 4B header + ~8B token + observe/content-format options + ~11B TLV
# value). Notifies are uniform, so bytes/min = (telemetry rows/min) * 45 is within
# ~10% of the true payload. For EXACT bytes a firmware net-layer counter is needed.
BYTES_PER_NOTIFY = 45
P5 = (f"SELECT date_trunc('minute', to_timestamp(k.ts/1000)) AS \"time\", "
      f"d.name AS metric, (count(*)*{BYTES_PER_NOTIFY})::int AS value "
      f"FROM ts_kv k JOIN device d ON d.id=k.entity_id "
      f"WHERE {NODEFILT} AND k.key >= 60 AND {TIMEFILT} "
      f"GROUP BY 1,2 ORDER BY 1")

# Tier 2b: EXACT bytes from firmware (Object 33000 RID 38 = lwm2m_tx_bytes,
# engine-counted, monotonic). Resolve the key_id via a scalar subquery so the
# main scan filters k.key=<id> directly (index-friendly) — and so the panel just
# shows no data until the first tx_bytes telemetry creates the key. P6 = the
# per-minute delta of the cumulative counter (exact bytes/min). P7 = exact avg
# packet size = tx_bytes_delta / notify_emitted_delta over the visible window.
TXB_KEY = "(SELECT key_id FROM key_dictionary WHERE key='lwm2m_tx_bytes' LIMIT 1)"
NE_KEY = "(SELECT key_id FROM key_dictionary WHERE key='notify_emitted' LIMIT 1)"
P6 = (f"SELECT t AS \"time\", metric, GREATEST(value - prev, 0)::int AS value FROM ("
      f"SELECT date_trunc('minute', to_timestamp(k.ts/1000)) AS t, d.name AS metric, "
      f"max(k.long_v) AS value, "
      f"lag(max(k.long_v)) OVER (PARTITION BY d.name ORDER BY "
      f"date_trunc('minute', to_timestamp(k.ts/1000))) AS prev "
      f"FROM ts_kv k JOIN device d ON d.id=k.entity_id "
      f"WHERE k.key={TXB_KEY} AND {NODEFILT} AND {TIMEFILT} "
      f"GROUP BY 1,2) s WHERE prev IS NOT NULL ORDER BY 1")
P7 = (f"SELECT d.name AS \"Nodo\", "
      f"round((max(k.long_v) FILTER (WHERE k.key=tb.id) - min(k.long_v) FILTER (WHERE k.key=tb.id))::numeric "
      f"/ NULLIF(max(k.long_v) FILTER (WHERE k.key=ne.id) - min(k.long_v) FILTER (WHERE k.key=ne.id), 0), 1) "
      f"AS \"Bytes/mensaje (exacto)\" "
      f"FROM ts_kv k JOIN device d ON d.id=k.entity_id "
      f"CROSS JOIN (SELECT {TXB_KEY.strip('()')}) tb(id) "
      f"CROSS JOIN (SELECT {NE_KEY.strip('()')}) ne(id) "
      f"WHERE k.key IN (tb.id, ne.id) AND {NODEFILT} AND {TIMEFILT} "
      f"GROUP BY d.name ORDER BY d.name")

dashboard = {
    "dashboard": {
        "uid": "ami-comms",
        "title": "AMI Comms — recepcion de mensajes",
        "tags": ["ami", "comms"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 0,
        "refresh": "30s",
        "time": {"from": "now-3h", "to": "now"},
        "templating": {"list": [{
            "name": "node", "type": "query", "label": "Nodo",
            "datasource": ds(),
            "query": "SELECT name FROM device WHERE name LIKE 'ami-esp32c6-%' ORDER BY name",
            "refresh": 1, "includeAll": True, "multi": True,
            "current": {"text": ["All"], "value": ["$__all"]},
            "sort": 1,
        }]},
        "panels": [
            panel(1, "Mensajes/min por nodo (activePower = 1 push)", 0, 0, 24, 9,
                  "timeseries", P1, "time_series",
                  {"fieldConfig": {"defaults": {"unit": "cpm", "custom": {"drawStyle": "line", "fillOpacity": 10}}, "overrides": []}}),
            panel(2, "Telemetria total/min por nodo (todas las keys)", 0, 9, 24, 8,
                  "timeseries", P2, "time_series",
                  {"fieldConfig": {"defaults": {"unit": "cpm"}, "overrides": []}}),
            panel(3, "Frescura: ultima recepcion por nodo", 0, 17, 12, 9,
                  "table", P3, "table"),
            panel(4, "Mensajes por recurso (ultima 1h, toda la flota)", 12, 17, 12, 9,
                  "table", P4, "table"),
            panel(5, "Ancho de banda estimado por nodo (~45 B/notify)", 0, 26, 24, 8,
                  "timeseries", P5, "time_series",
                  {"fieldConfig": {"defaults": {"unit": "decbytes", "custom": {"drawStyle": "line", "fillOpacity": 15}}, "overrides": []}}),
            panel(6, "Bytes LwM2M EXACTOS/min por nodo (firmware RID 38)", 0, 34, 24, 8,
                  "timeseries", P6, "time_series",
                  {"fieldConfig": {"defaults": {"unit": "decbytes", "custom": {"drawStyle": "bars", "fillOpacity": 40}}, "overrides": []}}),
            panel(7, "Tamano promedio de paquete EXACTO (B/mensaje)", 0, 42, 12, 8,
                  "table", P7, "table"),
        ],
    },
    "overwrite": True,
}


def main() -> int:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PW, timeout=15)

    def run(cmd, t=40):
        _i, o, e = c.exec_command(cmd, timeout=t)
        return (o.read().decode() + e.read().decode()).strip()

    def put_file(remote, content):
        sftp = c.open_sftp()
        with sftp.open(remote, "w") as f:
            f.write(content)
        sftp.close()

    # 1) fix the datasource DB -> thingsboard_edge (where the AMI telemetry lives)
    cur = json.loads(run(f"curl -s -u admin:admin {GRAFANA}/api/datasources/uid/{DS_UID}"))
    cur["database"] = "thingsboard_edge"
    cur["secureJsonData"] = {"password": "postgres"}
    put_file("/tmp/ds.json", json.dumps(cur))
    r = run(f"curl -s -u admin:admin -H 'Content-Type: application/json' "
            f"-X PUT {GRAFANA}/api/datasources/uid/{DS_UID} -d @/tmp/ds.json")
    print("[ds] update:", "OK" if '"message"' in r and 'updated' in r.lower() or '"datasource"' in r else r[:200])

    # 2) create/overwrite the dashboard
    put_file("/tmp/dash.json", json.dumps(dashboard))
    r = run(f"curl -s -u admin:admin -H 'Content-Type: application/json' "
            f"-X POST {GRAFANA}/api/dashboards/db -d @/tmp/dash.json")
    print("[dash] create:", r[:300])
    try:
        j = json.loads(r)
        if j.get("status") == "success":
            print(f"\n  OK dashboard -> http://{HOST}:3000{j['url']}  (admin/admin)")
    except Exception:
        pass
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
