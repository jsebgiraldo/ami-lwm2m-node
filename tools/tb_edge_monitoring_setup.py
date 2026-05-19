"""Configure native 24/7 fleet monitoring inside TB Edge (alarms + notifications
+ dashboard) for the AMI LwM2M Thread fleet.

Why TB Edge native instead of an external poller: TB Edge already runs 24/7
on the R1000, already receives every node's LwM2M telemetry, and already
computes the `active` flag. The missing piece was the *alerting + history*
layer — this script adds it, declaratively and idempotently.

What it sets up (all scoped to the AMI_LwM2M_Node device profile):

  Alarms (device-profile alarm rules — evaluated by the root rule chain):
    - "Node Offline"      CRITICAL  active == false        (clears on active)
    - "Self-Recovery"     WARNING   recover_count > 0      (soft watchdog ran)
    - "Watchdog Fired"    WARNING   watchdog_count > 0     (liveness watchdog ran)
    - "LwM2M Error"       WARNING   last_error_code != 0   (engine recorded error)

  Notifications:
    - template "AMI Fleet Alarm" (WEB delivery — shows in the TB Edge bell;
      add EMAIL/SLACK later once a recipient/SMTP is configured)
    - rule "AMI Fleet Alarms" — fires on alarm CREATED/SEVERITY_CHANGED for
      the AMI alarm types, to the "Tenant administrators" target

  Dashboard "AMI Fleet Health":
    - alarms table (active alarms across the fleet)
    - entities table (every AMI node: active, last activity, uptime, counters)

Idempotent: re-running updates the existing objects in place (matched by name)
rather than duplicating them.

Usage:
    python tools/tb_edge_monitoring_setup.py              # apply to r1000 edge
    python tools/tb_edge_monitoring_setup.py --dry-run    # show plan, change nothing
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid

import fleet_common as fc

fc.bootstrap_venv()

import requests  # noqa: E402

PROFILE_NAME = "AMI_LwM2M_Node"
DASHBOARD_NAME = "AMI Fleet Health"
NOTIF_TEMPLATE_NAME = "AMI Fleet Alarm"
NOTIF_RULE_NAME = "AMI Fleet Alarms"
TENANT_ADMINS_TARGET = "Tenant administrators"

# Alarm types this script owns (so re-runs replace only these, leaving any
# manually-added alarm rules on the profile untouched).
AMI_ALARM_TYPES = {"Node Offline", "Self-Recovery", "Watchdog Fired", "LwM2M Error"}


# ───────────────────────────── TB Edge client ───────────────────────────
class TB:
    def __init__(self, base: str, user: str, password: str):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        r = self.s.post(f"{self.base}/api/auth/login",
                        json={"username": user, "password": password}, timeout=15)
        r.raise_for_status()
        self.s.headers.update({"Authorization": f"Bearer {r.json()['token']}",
                               "Content-Type": "application/json"})

    def get(self, path, **kw):
        r = self.s.get(f"{self.base}{path}", timeout=20, **kw)
        r.raise_for_status()
        return r.json()

    def post(self, path, body):
        r = self.s.post(f"{self.base}{path}", data=json.dumps(body), timeout=30)
        if not r.ok:
            raise RuntimeError(f"POST {path} -> {r.status_code}: {r.text[:400]}")
        return r.json() if r.text else {}


# ───────────────────────── alarm-rule builders ──────────────────────────
def _kf(key_type, key, value_type, op, value, *, bool_val=None):
    """One key-filter predicate for an alarm condition."""
    if value_type == "BOOLEAN":
        pred = {"type": "BOOLEAN", "operation": op,
                "value": {"defaultValue": bool_val}}
    else:
        pred = {"type": "NUMERIC", "operation": op,
                "value": {"defaultValue": value}}
    return {"key": {"type": key_type, "key": key},
            "valueType": value_type, "predicate": pred}


def _simple_condition(filters):
    return {"condition": filters, "spec": {"type": "SIMPLE"}}


def _alarm(alarm_type, severity, create_filters, clear_filters=None):
    rule = {
        "id": str(uuid.uuid4()),
        "alarmType": alarm_type,
        "createRules": {
            severity: {
                "condition": _simple_condition(create_filters),
                "schedule": None, "alarmDetails": None, "dashboardId": None,
            }
        },
        "clearRule": None,
        "propagate": False,
        "propagateToOwner": False,
        "propagateToTenant": False,
        "propagateRelationTypes": [],
    }
    if clear_filters is not None:
        rule["clearRule"] = {
            "condition": _simple_condition(clear_filters),
            "schedule": None, "alarmDetails": None, "dashboardId": None,
        }
    return rule


def build_ami_alarms():
    """The four AMI fleet alarm rules."""
    return [
        # Node Offline — TB's own inactivity detection flips the `active`
        # server attribute; alarm on it, auto-clear when it comes back.
        _alarm("Node Offline", "CRITICAL",
               [_kf("ATTRIBUTE", "active", "BOOLEAN", "EQUAL", None, bool_val=False)],
               [_kf("ATTRIBUTE", "active", "BOOLEAN", "EQUAL", None, bool_val=True)]),
        # Self-Recovery — soft LwM2M watchdog ran a recover cycle. recover_count
        # is per-boot (not persisted), so it auto-clears when the node reboots.
        _alarm("Self-Recovery", "WARNING",
               [_kf("TIME_SERIES", "recover_count", "NUMERIC", "GREATER", 0)],
               [_kf("TIME_SERIES", "recover_count", "NUMERIC", "EQUAL", 0)]),
        # Watchdog Fired — liveness watchdog incremented its counter.
        _alarm("Watchdog Fired", "WARNING",
               [_kf("TIME_SERIES", "watchdog_count", "NUMERIC", "GREATER", 0)],
               [_kf("TIME_SERIES", "watchdog_count", "NUMERIC", "EQUAL", 0)]),
        # LwM2M Error — engine recorded a non-zero last_error_code.
        _alarm("LwM2M Error", "WARNING",
               [_kf("TIME_SERIES", "last_error_code", "NUMERIC", "NOT_EQUAL", 0)],
               [_kf("TIME_SERIES", "last_error_code", "NUMERIC", "EQUAL", 0)]),
    ]


# ───────────────────────────── operations ───────────────────────────────
def apply_alarms(tb: TB, dry: bool):
    profiles = tb.get("/api/deviceProfiles?pageSize=100&page=0")["data"]
    prof = next((p for p in profiles if p["name"] == PROFILE_NAME), None)
    if not prof:
        raise SystemExit(f"device profile '{PROFILE_NAME}' not found")
    prof = tb.get(f"/api/deviceProfile/{prof['id']['id']}")

    existing = prof["profileData"].get("alarms") or []
    # keep any alarm rules we don't own; replace ours
    kept = [a for a in existing if a.get("alarmType") not in AMI_ALARM_TYPES]
    new_alarms = kept + build_ami_alarms()
    prof["profileData"]["alarms"] = new_alarms

    print(f"[alarms] profile '{PROFILE_NAME}': "
          f"{len(existing)} existing -> {len(new_alarms)} "
          f"({len(kept)} kept, {len(AMI_ALARM_TYPES)} AMI rules)")
    for a in build_ami_alarms():
        sev = next(iter(a["createRules"]))
        print(f"          + {a['alarmType']:16s} [{sev}]")
    if dry:
        return
    tb.post("/api/deviceProfile", prof)
    print("[alarms] device profile updated OK")


# Resources to ADD to the device profile's LwM2M observe config (Phase 2).
# Each: lwm2m path -> (telemetry key name, pmin seconds, pmax seconds).
# pmin=0 => notify as soon as the value changes; pmax => heartbeat ceiling.
OBSERVE_ADDITIONS = {
    # Object 33000 v2.2 RIDs 21/22 — boot reliability counters. total_resets
    # is the un-fakeable proof the v0.6.17 real-liveness watchdog fired; it
    # only changes on a reboot, so notify immediately, heartbeat hourly.
    "/33000_1.0/0/21": ("last_reset_reason", 0, 3600),
    "/33000_1.0/0/22": ("total_resets", 0, 3600),
    # Object 3303 v1.1 — SoC die temperature. 5700 = current value,
    # 5602 = max measured since boot. Thermal headroom monitoring.
    "/3303_1.1/0/5700": ("temperature", 60, 900),
    "/3303_1.1/0/5602": ("temperature_max", 300, 3600),
}


def apply_observe(tb: TB, dry: bool):
    """Enrich the AMI_LwM2M_Node profile's LwM2M observe config so total_resets,
    last_reset_reason and SoC temperature flow as named telemetry.

    Additive: only inserts the OBSERVE_ADDITIONS paths, leaves every existing
    observed/telemetry resource and keyName mapping untouched. TB Edge pushes
    the new Observe requests to connected devices on profile save.
    """
    profiles = tb.get("/api/deviceProfiles?pageSize=100&page=0")["data"]
    prof = next((p for p in profiles if p["name"] == PROFILE_NAME), None)
    if not prof:
        raise SystemExit(f"device profile '{PROFILE_NAME}' not found")
    prof = tb.get(f"/api/deviceProfile/{prof['id']['id']}")

    oa = prof["profileData"]["transportConfiguration"]["observeAttr"]
    observe = oa.setdefault("observe", [])
    telemetry = oa.setdefault("telemetry", [])
    key_name = oa.setdefault("keyName", {})
    attr_lwm2m = oa.setdefault("attributeLwm2m", {})

    added = []
    for path, (key, pmin, pmax) in OBSERVE_ADDITIONS.items():
        if path in observe and key_name.get(path) == key:
            continue
        if path not in observe:
            observe.append(path)
        if path not in telemetry:
            telemetry.append(path)
        key_name[path] = key
        attr_lwm2m[path] = {"pmin": pmin, "pmax": pmax}
        added.append((path, key))

    print(f"[observe] profile '{PROFILE_NAME}': {len(added)} resource(s) to add")
    for path, key in added:
        print(f"          + {path:22s} -> {key}")
    if not added:
        print("[observe] all additions already present — no change")
        return
    if dry:
        return
    tb.post("/api/deviceProfile", prof)
    print("[observe] device profile observe config updated OK")
    print("[observe] note: TB Edge pushes new Observe requests to connected "
          "devices; allow ~1-2 reg cycles for temperature/total_resets to appear")


def apply_rulechain(tb: TB, dry: bool):
    """Insert a Device Profile node into the Edge Root Rule Chain so the
    device-profile alarm rules are actually evaluated.

    The stock Edge Root Rule Chain saves telemetry/attributes and pushes to
    cloud, but has NO device-profile node — so alarm rules never run. We add
    the node and fan telemetry + attribute + activity/inactivity events into
    it, in ADDITION to the existing paths (purely additive — no existing
    connection is removed, so the save/push behaviour is untouched).
    Idempotent: if the node already exists, nothing changes.
    """
    rcs = tb.get("/api/ruleChains?pageSize=100&page=0")["data"]
    root = next((r for r in rcs if r.get("root")), None)
    if not root:
        raise SystemExit("root rule chain not found")
    rc_id = root["id"]["id"]
    md = tb.get(f"/api/ruleChain/{rc_id}/metadata")
    nodes = md["nodes"]
    conns = md["connections"]

    DP_TYPE = "org.thingsboard.rule.engine.profile.TbDeviceProfileNode"
    if any(n.get("type") == DP_TYPE for n in nodes):
        print("[rulechain] Device Profile node already present — no change")
        return

    switch_idx = next((i for i, n in enumerate(nodes)
                       if n.get("type", "").endswith("TbMsgTypeSwitchNode")), None)
    if switch_idx is None:
        raise SystemExit("Message Type Switch node not found in root rule chain")

    dp_idx = len(nodes)
    nodes.append({
        "type": DP_TYPE,
        "name": "Device Profile",
        "configuration": {
            "persistAlarmRulesState": False,
            "fetchAlarmRulesStateOnStart": False,
        },
        "additionalInfo": {"layoutX": 1000, "layoutY": 500,
                           "description": "Evaluates AMI device-profile alarm "
                                          "rules (added by tb_edge_monitoring_setup.py)"},
        "debugSettings": None,
    })
    # Fan the message types that carry alarm-relevant state into the node.
    for rel in ("Post telemetry", "Post attributes", "Attributes Updated",
                "Activity Event", "Inactivity Event"):
        conns.append({"fromIndex": switch_idx, "toIndex": dp_idx, "type": rel})

    print(f"[rulechain] '{root['name']}': +1 Device Profile node "
          f"(index {dp_idx}), +5 connections from Message Type Switch")
    print("            telemetry/attributes/activity events now feed alarm "
          "evaluation (existing save+push paths untouched)")
    if dry:
        return
    # Save endpoint is POST /api/ruleChain/metadata (no id in path — the
    # metadata body already carries ruleChainId from the GET).
    tb.post("/api/ruleChain/metadata", md)
    print("[rulechain] root rule chain metadata updated OK")


def apply_notifications(tb: TB, dry: bool):
    # resolve the Tenant administrators target
    targets = tb.get("/api/notification/targets?pageSize=100&page=0")["data"]
    admin = next((t for t in targets if t["name"] == TENANT_ADMINS_TARGET), None)
    if not admin:
        raise SystemExit(f"notification target '{TENANT_ADMINS_TARGET}' not found")

    # ── template (WEB delivery) ──
    tmpls = tb.get("/api/notification/templates?pageSize=200&page=0&"
                   "notificationTypes=ALARM")["data"]
    tmpl = next((t for t in tmpls if t["name"] == NOTIF_TEMPLATE_NAME), None)
    tmpl_body = {
        "name": NOTIF_TEMPLATE_NAME,
        "notificationType": "ALARM",
        "configuration": {
            "deliveryMethodsTemplates": {
                "WEB": {
                    "method": "WEB",
                    "enabled": True,
                    "subject": "AMI fleet: ${alarmType} on ${originatorName}",
                    "body": "${alarmSeverity} — ${alarmType} on ${originatorName} "
                            "(status ${alarmStatus}) at ${alarmCreatedTime}",
                    "additionalConfig": {"icon": {"enabled": True, "icon": "warning",
                                                  "color": "#D12730"}},
                }
            }
        },
    }
    if tmpl:
        tmpl_body["id"] = tmpl["id"]
        action = "update"
    else:
        action = "create"
    print(f"[notif] template '{NOTIF_TEMPLATE_NAME}': {action} (WEB delivery)")
    if not dry:
        tmpl = tb.post("/api/notification/template", tmpl_body)

    # ── rule ──
    rules = tb.get("/api/notification/rules?pageSize=200&page=0")["data"]
    rule = next((r for r in rules if r["name"] == NOTIF_RULE_NAME), None)
    rule_body = {
        "name": NOTIF_RULE_NAME,
        "enabled": True,
        "templateId": tmpl["id"] if not dry and tmpl else (
            tmpl["id"] if tmpl else None),
        "triggerType": "ALARM",
        "triggerConfig": {
            "triggerType": "ALARM",
            "alarmTypes": sorted(AMI_ALARM_TYPES),
            "alarmSeverities": None,          # all severities
            "notifyOn": ["CREATED", "SEVERITY_CHANGED", "CLEARED"],
            "clearRule": None,
        },
        "recipientsConfig": {
            "triggerType": "ALARM",
            "escalationTable": {"0": [admin["id"]["id"]]},
        },
        "additionalConfig": {"description":
                             "Alerts for the AMI LwM2M Thread fleet — 30-day "
                             "stability watch. Managed by tb_edge_monitoring_setup.py"},
    }
    if rule:
        rule_body["id"] = rule["id"]
        action = "update"
    else:
        action = "create"
    print(f"[notif] rule '{NOTIF_RULE_NAME}': {action} "
          f"-> target '{TENANT_ADMINS_TARGET}', on CREATED/SEVERITY_CHANGED/CLEARED")
    if not dry:
        tb.post("/api/notification/rule", rule_body)
        print("[notif] notification rule applied OK")


def build_dashboard():
    """Minimal but functional fleet-health dashboard: an alarms table over the
    whole tenant + an entities table listing every AMI node with its key
    health columns. Two widgets, one row each."""
    alarm_wid = str(uuid.uuid4())
    ent_wid = str(uuid.uuid4())
    alarm_ds = str(uuid.uuid4())
    ent_ds = str(uuid.uuid4())

    def col(key, label, ctype="timeseries"):
        return {"name": key, "label": label, "type": ctype,
                "columnWidth": "0px", "useCellStyleFunction": False,
                "useCellContentFunction": False}

    return {
        "title": DASHBOARD_NAME,
        "configuration": {
            "description": "AMI LwM2M Thread fleet — 24/7 health watch "
                           "(managed by tb_edge_monitoring_setup.py)",
            "widgets": {
                alarm_wid: {
                    "typeFullFqn": "system.alarm_widgets.alarms_table",
                    "type": "alarm",
                    "sizeX": 24, "sizeY": 8, "row": 0, "col": 0,
                    "config": {
                        "title": "Active fleet alarms",
                        "showTitle": True,
                        "datasources": [{
                            "type": "alarmCount",
                            "name": "Fleet alarms",
                        }],
                        "alarmSource": {"type": "function", "name": "Alarms"},
                        "alarmFilterConfig": {
                            "statusList": ["ACTIVE"],
                            "severityList": [], "typeList": [],
                            "searchPropagatedAlarms": True,
                        },
                        "settings": {
                            "enableSelection": False,
                            "enableSearch": True,
                            "displayDetails": True,
                            "enableFilter": True,
                            "defaultSortOrder": "-createdTime",
                        },
                    },
                },
                ent_wid: {
                    "typeFullFqn": "system.entity_widgets.entities_table",
                    "type": "latest",
                    "sizeX": 24, "sizeY": 12, "row": 8, "col": 0,
                    "config": {
                        "title": "AMI fleet nodes",
                        "showTitle": True,
                        "datasources": [{
                            "type": "entity",
                            "name": "AMI nodes",
                            "entityAliasId": ent_ds,
                            "dataKeys": [
                                col("active", "Active", "attribute"),
                                col("thread_role", "Thread role"),
                                col("uptime_s", "Uptime (s)"),
                                col("reg_success", "Reg OK"),
                                col("recover_count", "Recoveries"),
                                col("watchdog_count", "Watchdog"),
                                col("last_error_code", "Last err"),
                                col("voltage", "V"),
                                col("frequency", "Hz"),
                            ],
                        }],
                        "settings": {
                            "entitiesTitle": "AMI fleet nodes",
                            "enableSearch": True,
                            "displayEntityName": True,
                            "displayEntityLabel": False,
                            "displayEntityType": False,
                            "defaultSortOrder": "entityName",
                        },
                    },
                },
            },
            "states": {
                "default": {
                    "name": DASHBOARD_NAME,
                    "root": True,
                    "layouts": {
                        "main": {
                            "widgets": {
                                alarm_wid: {"sizeX": 24, "sizeY": 8, "row": 0, "col": 0},
                                ent_wid: {"sizeX": 24, "sizeY": 12, "row": 8, "col": 0},
                            },
                            "gridSettings": {"backgroundColor": "#eeeeee",
                                             "columns": 24, "margin": 10,
                                             "outerMargin": True},
                        }
                    },
                }
            },
            "entityAliases": {
                ent_ds: {
                    "id": ent_ds,
                    "alias": "AMI nodes",
                    "filter": {
                        "type": "deviceType",
                        "resolveMultiple": True,
                        "deviceType": PROFILE_NAME,
                        "deviceNamePattern": "ami-esp32c6",
                    },
                },
                alarm_ds: {
                    "id": alarm_ds,
                    "alias": "All devices",
                    "filter": {"type": "deviceType", "resolveMultiple": True,
                               "deviceType": PROFILE_NAME},
                },
            },
            "timewindow": {
                "realtime": {"timewindowMs": 3600000},  # last 1 h default
            },
            "settings": {"stateControllerId": "entity",
                         "showTitle": True, "showDashboardsSelect": True},
        },
    }


def apply_dashboard(tb: TB, dry: bool):
    dashboards = tb.get("/api/tenant/dashboards?pageSize=200&page=0")["data"]
    existing = next((d for d in dashboards
                     if d.get("title", d.get("name")) == DASHBOARD_NAME), None)
    body = build_dashboard()
    if existing:
        full = tb.get(f"/api/dashboard/{existing['id']['id']}")
        body["id"] = full["id"]
        action = "update"
    else:
        action = "create"
    print(f"[dash] dashboard '{DASHBOARD_NAME}': {action} "
          f"(alarms table + entities table)")
    if dry:
        return
    res = tb.post("/api/dashboard", body)
    print(f"[dash] dashboard applied OK  id={res.get('id',{}).get('id')}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mesh", default=fc.DEFAULT_MESH, choices=fc.MESH_TARGETS)
    ap.add_argument("--user", default=fc.EDGE_TENANT_USER)
    ap.add_argument("--password", default=fc.EDGE_TENANT_PASS)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan, change nothing")
    ap.add_argument("--only", choices=["observe", "alarms", "rulechain",
                                       "notifications", "dashboard"],
                    help="apply just one section")
    args = ap.parse_args()

    host, port = fc.edge_for_mesh(args.mesh)
    base = f"http://{host}:{port}"
    print(f"=== TB Edge monitoring setup -> {base} "
          f"{'(DRY RUN)' if args.dry_run else ''} ===")
    tb = TB(base, args.user, args.password)

    sections = ([args.only] if args.only else
                ["observe", "alarms", "rulechain", "notifications", "dashboard"])
    for sec in sections:
        try:
            if sec == "observe":
                apply_observe(tb, args.dry_run)
            elif sec == "alarms":
                apply_alarms(tb, args.dry_run)
            elif sec == "rulechain":
                apply_rulechain(tb, args.dry_run)
            elif sec == "notifications":
                apply_notifications(tb, args.dry_run)
            elif sec == "dashboard":
                apply_dashboard(tb, args.dry_run)
        except Exception as e:
            print(f"[{sec}] FAILED: {e}")
            return 1
    print("=== done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
