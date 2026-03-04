#!/usr/bin/env python3
"""
Update Emsitech C2000 Dashboard — v4.2 Layout
==============================================
Fixes v4.1:
- value_card: dataKey label usa nombre descriptivo (no raw key)
- value_card: dataKey incluye campos completos (funcBody, aggregationType, etc.)
- Reduce alturas de cards para menos densidad vertical
- Incrementa margin de la grilla (10) para más espacio entre widgets
- showDate: true en todos los value_cards (Last Update dentro del card)
- _hash único por dataKey

Dashboard: f6c19720-1690-11f1-b4b8-830e99f551cd
"""

import json
import uuid
import random
import urllib.request

TB_URL = "http://192.168.1.111:8090"
USERNAME = "tenant@thingsboard.org"
PASSWORD = "tenant"
DEVICE_NAME = "ami-esp32c6-2434"
DASHBOARD_ID = "f6c19720-1690-11f1-b4b8-830e99f551cd"


def login():
    data = json.dumps({"username": USERNAME, "password": PASSWORD}).encode()
    req = urllib.request.Request(f"{TB_URL}/api/auth/login", data=data,
                                headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())["token"]


def api_get(token, path):
    req = urllib.request.Request(f"{TB_URL}{path}",
                                headers={"X-Authorization": f"Bearer {token}"})
    return json.loads(urllib.request.urlopen(req).read())


def api_post(token, path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{TB_URL}{path}", data=data, method="POST",
                                headers={"X-Authorization": f"Bearer {token}",
                                         "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())


def find_device(token):
    devs = api_get(token, "/api/tenant/devices?pageSize=50&page=0")
    for d in devs["data"]:
        if "2434" in d["name"]:
            return d
    raise RuntimeError("Device not found")


# ── Helpers ───────────────────────────────────────────────────────────
def gen_id():
    return str(uuid.uuid4())


def device_alias(device_id):
    alias_id = gen_id()
    return alias_id, {
        "id": alias_id,
        "alias": DEVICE_NAME,
        "filter": {
            "type": "singleEntity",
            "singleEntity": {"entityType": "DEVICE", "id": device_id},
            "resolveMultiple": False
        }
    }


COLORS = {
    "voltage": "#FFC107", "current": "#2196F3",
    "activePower": "#FF5722", "reactivePower": "#9C27B0",
    "apparentPower": "#FF9800", "powerFactor": "#4CAF50",
    "totalActivePower": "#F44336", "totalReactivePower": "#673AB7",
    "totalApparentPower": "#E65100", "totalPowerFactor": "#009688",
    "activeEnergy": "#E91E63", "reactiveEnergy": "#3F51B5",
    "apparentEnergy": "#795548", "frequency": "#00BCD4",
    "radioSignalStrength": "#FF6F00", "linkQuality": "#43A047",
}

ICONS = {
    "voltage": "flash_on",
    "current": "electric_bolt",
    "frequency": "speed",
    "activePower": "power",
    "reactivePower": "offline_bolt",
    "apparentPower": "bolt",
    "powerFactor": "tune",
    "totalActivePower": "power",
    "totalReactivePower": "offline_bolt",
    "totalApparentPower": "bolt",
    "totalPowerFactor": "tune",
    "activeEnergy": "battery_charging_full",
    "reactiveEnergy": "battery_alert",
    "apparentEnergy": "battery_std",
    "radioSignalStrength": "signal_cellular_alt",
    "linkQuality": "network_check",
}

# Descriptive labels for each telemetry key
LABELS = {
    "voltage": "Voltaje",
    "current": "Corriente",
    "frequency": "Frecuencia",
    "activePower": "P. Activa",
    "reactivePower": "P. Reactiva",
    "apparentPower": "P. Aparente",
    "powerFactor": "Factor de Potencia",
    "totalActivePower": "P. Activa Total",
    "totalReactivePower": "P. Reactiva Total",
    "totalApparentPower": "P. Aparente Total",
    "totalPowerFactor": "FP Total",
    "activeEnergy": "Energía Activa",
    "reactiveEnergy": "Energía Reactiva",
    "apparentEnergy": "Energía Aparente",
    "radioSignalStrength": "RSSI (Señal)",
    "linkQuality": "Link Quality (LQI)",
}

UNITS = {
    "voltage": "V", "current": "A", "frequency": "Hz",
    "activePower": "kW", "reactivePower": "kvar",
    "apparentPower": "kVA", "powerFactor": "",
    "totalActivePower": "kW", "totalReactivePower": "kvar",
    "totalApparentPower": "kVA", "totalPowerFactor": "",
    "activeEnergy": "kWh", "reactiveEnergy": "kvarh",
    "apparentEnergy": "kVAh",
    "radioSignalStrength": "dBm", "linkQuality": "%",
}

DECIMALS = {
    "voltage": 1, "current": 2, "frequency": 2,
    "activePower": 2, "reactivePower": 2,
    "apparentPower": 2, "powerFactor": 3,
    "totalActivePower": 2, "totalReactivePower": 2,
    "totalApparentPower": 2, "totalPowerFactor": 3,
    "activeEnergy": 1, "reactiveEnergy": 1, "apparentEnergy": 1,
    "radioSignalStrength": 0, "linkQuality": 0,
}


def make_datakey(key, label=None, key_type="timeseries", units="", decimals=0):
    """Create a dataKey with ALL fields that TB v4.2 expects."""
    return {
        "name": key,
        "type": key_type,
        "label": label or LABELS.get(key, key),
        "color": COLORS.get(key, "#2196F3"),
        "settings": {},
        "_hash": round(random.random(), 10),
        "units": units,
        "decimals": decimals,
        "funcBody": "",
        "aggregationType": None,
        "usePostProcessing": False,
        "postFuncBody": ""
    }


def _color_obj(color):
    """Color object matching TB v4.2 schema."""
    return {
        "type": "constant",
        "color": color,
        "colorFunction": ""
    }


# ── Widget constructors ──────────────────────────────────────────────

def html_card(wid, html_content, sizeX=24, sizeY=2):
    return {
        "typeFullFqn": "system.cards.html_card2",
        "type": "static",
        "sizeX": sizeX, "sizeY": sizeY,
        "row": 0, "col": 0,
        "id": wid,
        "config": {
            "datasources": [],
            "timewindow": {"realtime": {"timewindowMs": 60000}},
            "showTitle": False,
            "backgroundColor": "rgba(0,0,0,0)",
            "color": "rgba(0, 0, 0, 0.87)",
            "padding": "0px",
            "settings": {"html": html_content, "cardCss": ""},
            "dropShadow": False, "enableFullscreen": False,
            "widgetStyle": {}, "actions": {}
        }
    }


def section_header(wid, text, icon_html="", sizeX=24, sizeY=1):
    """Section divider with blue bottom border."""
    html = (
        f'<div style="display:flex;align-items:center;padding:4px 16px;'
        f'border-bottom:2px solid #1976D2;margin-bottom:0;">'
        f'<span style="font-size:18px;margin-right:8px;">{icon_html}</span>'
        f'<span style="font-size:15px;font-weight:600;color:#1976D2;'
        f'text-transform:uppercase;letter-spacing:1px;">{text}</span>'
        f'</div>'
    )
    return html_card(wid, html, sizeX=sizeX, sizeY=sizeY)


def gauge_widget(wid, alias_id, key, title, min_val, max_val, units, decimals=1):
    return {
        "typeFullFqn": "system.analogue_gauges.speed_gauge_canvas_gauges",
        "type": "latest",
        "sizeX": 8, "sizeY": 5,
        "row": 0, "col": 0,
        "id": wid,
        "config": {
            "datasources": [{"type": "entity", "entityAliasId": alias_id,
                             "dataKeys": [make_datakey(key, title,
                                                       units=units,
                                                       decimals=decimals)]}],
            "timewindow": {"realtime": {"timewindowMs": 60000}},
            "showTitle": True, "title": title,
            "backgroundColor": "rgb(255, 255, 255)",
            "color": "rgba(0, 0, 0, 0.87)",
            "padding": "0px",
            "settings": {
                "minValue": min_val, "maxValue": max_val,
                "showUnitTitle": True, "unitTitle": units,
                "majorTicksCount": 6, "minorTicks": 4,
                "valueBox": True, "valueInt": 3,
                "defaultColor": COLORS.get(key, "#2196F3"),
                "colorPlate": "#fff",
                "colorMajorTicks": "#444", "colorMinorTicks": "#666",
                "colorNeedle": "#F44336", "colorNeedleEnd": "",
                "colorValueBoxRect": "#888",
                "colorValueBoxBackground": "#EBEBEB",
                "colorValueBoxRectEnd": "#666",
                "highlights": [],
                "showBorder": True, "animation": True,
                "animationDuration": 500, "animationRule": "linear",
            },
            "dropShadow": True, "enableFullscreen": False,
            "useDashboardTimewindow": True, "showLegend": False,
            "units": units, "decimals": decimals,
            "titleStyle": {"fontSize": "14px", "fontWeight": 600},
            "widgetStyle": {}, "actions": {}
        }
    }


def value_card(wid, alias_id, key,
               label=None, units=None, decimals=None,
               icon=None, icon_color=None, value_color=None,
               show_date=True, layout="square", value_size=52,
               label_size=16, icon_size=40, bg_color="#fff"):
    """
    Value card with FULL settings schema for TB Edge v4.2.
    show_date=True shows 'Last Update: X seconds ago' inside the card.
    """
    _label = label or LABELS.get(key, key)
    _units = units if units is not None else UNITS.get(key, "")
    _decimals = decimals if decimals is not None else DECIMALS.get(key, 1)
    _icon = icon or ICONS.get(key, "info")
    _icon_color = icon_color or COLORS.get(key, "#5469FF")
    _value_color = value_color or "rgba(0, 0, 0, 0.87)"

    return {
        "typeFullFqn": "system.cards.value_card",
        "type": "latest",
        "sizeX": 6, "sizeY": 3,
        "row": 0, "col": 0,
        "id": wid,
        "config": {
            "datasources": [{
                "type": "entity",
                "entityAliasId": alias_id,
                "dataKeys": [make_datakey(key, _label,
                                          units=_units,
                                          decimals=_decimals)]
            }],
            "timewindow": {
                "displayValue": "",
                "selectedTab": 0,
                "realtime": {
                    "realtimeType": 1,
                    "interval": 1000,
                    "timewindowMs": 60000,
                    "quickInterval": "CURRENT_DAY",
                    "hideInterval": False,
                    "hideLastInterval": False,
                    "hideQuickInterval": False
                },
                "history": {
                    "historyType": 0,
                    "interval": 1000,
                    "timewindowMs": 60000,
                    "fixedTimewindow": {
                        "startTimeMs": 1772562046663,
                        "endTimeMs": 1772648446663
                    },
                    "quickInterval": "CURRENT_DAY",
                    "hideInterval": False,
                    "hideLastInterval": False,
                    "hideFixedInterval": False,
                    "hideQuickInterval": False
                },
                "aggregation": {
                    "type": "AVG",
                    "limit": 25000
                }
            },
            "showTitle": False,
            "backgroundColor": "rgba(0, 0, 0, 0)",
            "color": "rgba(0, 0, 0, 0.87)",
            "padding": "0px",
            "settings": {
                "labelPosition": "top",
                "layout": layout,
                "showLabel": True,
                "labelFont": {
                    "family": "Roboto", "size": label_size,
                    "sizeUnit": "px", "style": "normal", "weight": "500"
                },
                "labelColor": _color_obj("rgba(0, 0, 0, 0.87)"),
                "showIcon": True,
                "iconSize": icon_size,
                "iconSizeUnit": "px",
                "icon": _icon,
                "iconColor": _color_obj(_icon_color),
                "valueFont": {
                    "family": "Roboto", "size": value_size,
                    "sizeUnit": "px", "style": "normal", "weight": "500"
                },
                "valueColor": _color_obj(_value_color),
                "showDate": show_date,
                "dateFormat": {
                    "format": None,
                    "lastUpdateAgo": True,
                    "custom": False
                },
                "dateFont": {
                    "family": "Roboto", "size": 12,
                    "sizeUnit": "px", "style": "normal", "weight": "500"
                },
                "dateColor": _color_obj("rgba(0, 0, 0, 0.38)"),
                "background": {
                    "type": "color",
                    "color": bg_color,
                    "overlay": {
                        "enabled": False,
                        "color": "rgba(255,255,255,0.72)",
                        "blur": 3
                    }
                }
            },
            "units": _units, "decimals": _decimals,
            "dropShadow": True, "enableFullscreen": False,
            "useDashboardTimewindow": True, "showLegend": False,
            "widgetStyle": {}, "actions": {}
        }
    }


def timeseries_chart(wid, alias_id, keys, title, sizeX=12, sizeY=5):
    dk_list = []
    for k in keys:
        dk_list.append(make_datakey(k, LABELS.get(k, k),
                                    units=UNITS.get(k, ""),
                                    decimals=DECIMALS.get(k, 1)))
    return {
        "typeFullFqn": "system.charts.basic_timeseries",
        "type": "timeseries",
        "sizeX": sizeX, "sizeY": sizeY,
        "row": 0, "col": 0,
        "id": wid,
        "config": {
            "datasources": [{"type": "entity", "entityAliasId": alias_id,
                             "dataKeys": dk_list}],
            "timewindow": {"realtime": {"timewindowMs": 3600000}},
            "showTitle": True, "title": title,
            "backgroundColor": "rgb(255, 255, 255)",
            "color": "rgba(0, 0, 0, 0.87)",
            "padding": "8px",
            "settings": {
                "smoothLines": False, "showPoints": False,
                "lineWidth": 2, "tooltipIndividual": True,
            },
            "dropShadow": True, "enableFullscreen": True,
            "useDashboardTimewindow": True,
            "showLegend": True,
            "legendConfig": {
                "direction": "column", "position": "bottom",
                "sortDataKeys": True, "showMin": True,
                "showMax": True, "showAvg": True,
                "showTotal": False, "showLatest": True
            },
            "titleStyle": {"fontSize": "14px", "fontWeight": 600},
            "widgetStyle": {}, "actions": {}
        }
    }


def attributes_card(wid, alias_id, attr_keys, title):
    return {
        "typeFullFqn": "system.cards.attributes_card",
        "type": "latest",
        "sizeX": 12, "sizeY": 4,
        "row": 0, "col": 0,
        "id": wid,
        "config": {
            "datasources": [{"type": "entity", "entityAliasId": alias_id,
                             "dataKeys": [make_datakey(k, k, "attribute")
                                          for k in attr_keys]}],
            "timewindow": {"realtime": {"timewindowMs": 60000}},
            "showTitle": True, "title": title,
            "backgroundColor": "rgb(255, 255, 255)",
            "color": "rgba(0, 0, 0, 0.87)",
            "padding": "0px", "settings": {},
            "dropShadow": True, "enableFullscreen": False,
            "useDashboardTimewindow": True, "showLegend": False,
            "titleStyle": {"fontSize": "15px", "fontWeight": 600},
            "widgetStyle": {}, "actions": {}
        }
    }


# ── Main ──────────────────────────────────────────────────────────────
def main():
    token = login()
    print("  Autenticado en ThingsBoard Edge")

    device = find_device(token)
    device_id = device["id"]["id"]
    print(f"  Device: {device['name']} ({device_id})")

    # Fetch existing dashboard to preserve id/version
    existing = api_get(token, f"/api/dashboard/{DASHBOARD_ID}")
    print(f"  Dashboard actual: {existing['title']}")

    alias_id, alias_def = device_alias(device_id)

    widgets = {}
    layout_widgets = {}
    mobile_order = [0]

    def add(widget_fn, row, col, sx, sy, *args, **kwargs):
        _id = gen_id()
        mobile_order[0] += 1
        w = widget_fn(_id, *args, **kwargs)
        widgets[_id] = w
        layout_widgets[_id] = {
            "sizeX": sx, "sizeY": sy,
            "row": row, "col": col,
            "mobileOrder": mobile_order[0],
        }
        return _id

    # ══════════════════════════════════════════════════════════════
    # ROW 0: HEADER BANNER (24 x 2)
    # ══════════════════════════════════════════════════════════════
    add(html_card, 0, 0, 24, 2,
        html_content=(
            '<div style="display:flex;align-items:center;justify-content:space-between;'
            'padding:10px 24px;background:linear-gradient(135deg,#1565C0 0%,#0D47A1 100%);'
            'border-radius:8px;color:white;">'
            '<div style="display:flex;align-items:center;gap:14px;">'
            '<div style="font-size:36px;">&#9889;</div>'
            '<div>'
            '<h2 style="margin:0;font-size:20px;font-weight:700;letter-spacing:0.5px;">'
            'Emsitech C2000 &mdash; Medidor Monof&aacute;sico</h2>'
            '<p style="margin:2px 0 0;font-size:12px;opacity:0.85;">'
            'AMI &bull; ESP32-C6 &bull; Thread 1.3 &bull; LwM2M &bull; ThingsBoard Edge v4.2</p>'
            '</div></div>'
            '<div style="text-align:right;font-size:11px;opacity:0.7;">'
            '<div>Firmware v0.15.1</div>'
            '<div>Smart Threshold Notification</div>'
            '</div></div>'
        ), sizeX=24, sizeY=2)

    # ══════════════════════════════════════════════════════════════
    # ROW 2: Section — Mediciones Principales
    # ══════════════════════════════════════════════════════════════
    add(section_header, 2, 0, 24, 1,
        text="Mediciones Principales", icon_html="&#128200;")

    # ══════════════════════════════════════════════════════════════
    # ROW 3: GAUGES (V / I / Hz)  8 x 4 each (reduced from 5)
    # ══════════════════════════════════════════════════════════════
    add(gauge_widget, 3, 0, 8, 4,
        alias_id=alias_id, key="voltage", title="Voltaje",
        min_val=100, max_val=150, units="V", decimals=1)
    add(gauge_widget, 3, 8, 8, 4,
        alias_id=alias_id, key="current", title="Corriente",
        min_val=0, max_val=30, units="A", decimals=2)
    add(gauge_widget, 3, 16, 8, 4,
        alias_id=alias_id, key="frequency", title="Frecuencia",
        min_val=59, max_val=61, units="Hz", decimals=2)

    # ══════════════════════════════════════════════════════════════
    # ROW 7: Value cards V/I/Hz  8 x 2 each (reduced from 3)
    # ══════════════════════════════════════════════════════════════
    add(value_card, 7, 0, 8, 2,
        alias_id=alias_id, key="voltage",
        show_date=True, layout="horizontal",
        value_size=28, label_size=13, icon_size=32)
    add(value_card, 7, 8, 8, 2,
        alias_id=alias_id, key="current",
        show_date=True, layout="horizontal",
        value_size=28, label_size=13, icon_size=32)
    add(value_card, 7, 16, 8, 2,
        alias_id=alias_id, key="frequency",
        show_date=True, layout="horizontal",
        value_size=28, label_size=13, icon_size=32)

    # ══════════════════════════════════════════════════════════════
    # ROW 9: Section — Calidad de Enlace RF
    # ══════════════════════════════════════════════════════════════
    add(section_header, 9, 0, 24, 1,
        text="Calidad de Enlace RF (Thread 802.15.4)", icon_html="&#128225;")

    # ══════════════════════════════════════════════════════════════
    # ROW 10: RSSI + LQI cards (6 x 3) + timeseries (12 x 5)
    # ══════════════════════════════════════════════════════════════
    add(value_card, 10, 0, 6, 3,
        alias_id=alias_id, key="radioSignalStrength",
        show_date=True, layout="square",
        value_size=36, icon_size=36)
    add(value_card, 10, 6, 6, 3,
        alias_id=alias_id, key="linkQuality",
        show_date=True, layout="square",
        value_size=36, icon_size=36)
    add(timeseries_chart, 10, 12, 12, 5,
        alias_id=alias_id,
        keys=["radioSignalStrength", "linkQuality"],
        title="RSSI y Link Quality en el Tiempo")

    # ══════════════════════════════════════════════════════════════
    # ROW 15: Section — Potencias
    # ══════════════════════════════════════════════════════════════
    add(section_header, 15, 0, 24, 1,
        text="Potencias", icon_html="&#9889;")

    # ══════════════════════════════════════════════════════════════
    # ROW 16: Power cards (4 x 6 x 2 each)
    # ══════════════════════════════════════════════════════════════
    add(value_card, 16, 0, 6, 2,
        alias_id=alias_id, key="activePower",
        show_date=True, layout="horizontal",
        value_size=24, label_size=12, icon_size=28)
    add(value_card, 16, 6, 6, 2,
        alias_id=alias_id, key="reactivePower",
        show_date=True, layout="horizontal",
        value_size=24, label_size=12, icon_size=28)
    add(value_card, 16, 12, 6, 2,
        alias_id=alias_id, key="apparentPower",
        show_date=True, layout="horizontal",
        value_size=24, label_size=12, icon_size=28)
    add(value_card, 16, 18, 6, 2,
        alias_id=alias_id, key="powerFactor",
        show_date=True, layout="horizontal",
        value_size=24, label_size=12, icon_size=28)

    # ══════════════════════════════════════════════════════════════
    # ROW 18: Total power cards (4 x 6 x 2 each)
    # ══════════════════════════════════════════════════════════════
    add(value_card, 18, 0, 6, 2,
        alias_id=alias_id, key="totalActivePower",
        show_date=True, layout="horizontal",
        value_size=24, label_size=12, icon_size=28)
    add(value_card, 18, 6, 6, 2,
        alias_id=alias_id, key="totalReactivePower",
        show_date=True, layout="horizontal",
        value_size=24, label_size=12, icon_size=28)
    add(value_card, 18, 12, 6, 2,
        alias_id=alias_id, key="totalApparentPower",
        show_date=True, layout="horizontal",
        value_size=24, label_size=12, icon_size=28)
    add(value_card, 18, 18, 6, 2,
        alias_id=alias_id, key="totalPowerFactor",
        show_date=True, layout="horizontal",
        value_size=24, label_size=12, icon_size=28)

    # ══════════════════════════════════════════════════════════════
    # ROW 20: Power timeseries (2 x 12 x 5)
    # ══════════════════════════════════════════════════════════════
    add(timeseries_chart, 20, 0, 12, 5,
        alias_id=alias_id,
        keys=["activePower", "reactivePower", "apparentPower"],
        title="Potencias en el Tiempo")
    add(timeseries_chart, 20, 12, 12, 5,
        alias_id=alias_id,
        keys=["powerFactor", "totalPowerFactor"],
        title="Factor de Potencia en el Tiempo")

    # ══════════════════════════════════════════════════════════════
    # ROW 25: Section — Energía Acumulada
    # ══════════════════════════════════════════════════════════════
    add(section_header, 25, 0, 24, 1,
        text="Energia Acumulada", icon_html="&#128267;")

    # ══════════════════════════════════════════════════════════════
    # ROW 26: Energy cards (3 x 8 x 2)
    # ══════════════════════════════════════════════════════════════
    add(value_card, 26, 0, 8, 2,
        alias_id=alias_id, key="activeEnergy",
        show_date=True, layout="horizontal",
        value_size=24, label_size=12, icon_size=28)
    add(value_card, 26, 8, 8, 2,
        alias_id=alias_id, key="reactiveEnergy",
        show_date=True, layout="horizontal",
        value_size=24, label_size=12, icon_size=28)
    add(value_card, 26, 16, 8, 2,
        alias_id=alias_id, key="apparentEnergy",
        show_date=True, layout="horizontal",
        value_size=24, label_size=12, icon_size=28)

    # ══════════════════════════════════════════════════════════════
    # ROW 28: Energy + Frequency timeseries (2 x 12 x 5)
    # ══════════════════════════════════════════════════════════════
    add(timeseries_chart, 28, 0, 12, 5,
        alias_id=alias_id, keys=["activeEnergy"],
        title="Energía Activa Acumulada")
    add(timeseries_chart, 28, 12, 12, 5,
        alias_id=alias_id, keys=["frequency"],
        title="Frecuencia en el Tiempo")

    # ══════════════════════════════════════════════════════════════
    # ROW 33: Section — Históricos V/I
    # ══════════════════════════════════════════════════════════════
    add(section_header, 33, 0, 24, 1,
        text="Historicos V / I", icon_html="&#128200;")

    add(timeseries_chart, 34, 0, 12, 5,
        alias_id=alias_id, keys=["voltage"],
        title="Voltaje en el Tiempo")
    add(timeseries_chart, 34, 12, 12, 5,
        alias_id=alias_id, keys=["current"],
        title="Corriente en el Tiempo")

    # ══════════════════════════════════════════════════════════════
    # ROW 39: Section — Info Dispositivo
    # ══════════════════════════════════════════════════════════════
    add(section_header, 39, 0, 24, 1,
        text="Informacion del Dispositivo", icon_html="&#128736;")

    add(attributes_card, 40, 0, 12, 3,
        alias_id=alias_id,
        attr_keys=["Manufacturer", "ModelNumber", "SerialNumber", "Description"],
        title="Datos del Medidor")

    # Firmware info card
    add(html_card, 40, 12, 12, 3,
        html_content=(
            '<div style="padding:12px;font-family:monospace;font-size:12px;'
            'line-height:1.7;color:#333;">'
            '<div><b>Firmware:</b> v0.15.1 (Smart Threshold Notification)</div>'
            '<div><b>Board:</b> XIAO ESP32-C6</div>'
            '<div><b>SoC:</b> ESP32-C6 RISC-V @ 160 MHz</div>'
            '<div><b>Radio:</b> IEEE 802.15.4 (Thread 1.3)</div>'
            '<div><b>Protocolo:</b> LwM2M 1.1 / CoAP / DTLS</div>'
            '<div><b>DLMS Poll:</b> 15s | <b>Notif.:</b> Por umbral</div>'
            '</div>'
        ), sizeX=12, sizeY=3)

    # ── Assemble dashboard ────────────────────────────────────────
    dashboard = {
        "id": existing["id"],
        "createdTime": existing.get("createdTime"),
        "tenantId": existing.get("tenantId"),
        "title": "Emsitech C2000 - Monofasico",
        "configuration": {
            "description": "Dashboard AMI - Medidor Emsitech C2000 via Thread/LwM2M",
            "widgets": widgets,
            "states": {
                "default": {
                    "name": "Emsitech C2000 - Monofasico",
                    "root": True,
                    "layouts": {
                        "main": {
                            "widgets": layout_widgets,
                            "gridSettings": {
                                "backgroundColor": "#ECEFF1",
                                "columns": 24,
                                "margin": 10,
                                "backgroundSizeMode": "100%",
                                "autoFillHeight": False,
                                "mobileAutoFillHeight": False,
                                "mobileRowHeight": 70,
                                "outerMargin": True,
                                "layoutType": "default"
                            }
                        }
                    }
                }
            },
            "entityAliases": {
                alias_id: alias_def
            },
            "filters": {},
            "timewindow": {
                "selectedTab": 0,
                "realtime": {
                    "realtimeType": 0,
                    "interval": 1000,
                    "timewindowMs": 3600000
                },
                "aggregation": {
                    "type": "NONE"
                }
            },
            "settings": {
                "stateControllerId": "default",
                "showTitle": False,
                "showDashboardsSelect": False,
                "showEntitiesSelect": False,
                "showDashboardTimewindow": True,
                "showDashboardExport": True,
                "toolbarAlwaysOpen": True,
                "hideToolbar": False,
                "showFilters": False,
                "showUpdateDashboardImage": False,
            }
        }
    }

    # ── Update existing dashboard ─────────────────────────────────
    result = api_post(token, "/api/dashboard", dashboard)
    dash_id = result["id"]["id"]
    print(f"\n{'='*60}")
    print(f"  Dashboard actualizado v4.2!")
    print(f"  ID:      {dash_id}")
    print(f"  Title:   {result['title']}")
    print(f"  URL:     {TB_URL}/dashboards/{dash_id}")
    print(f"  Widgets: {len(widgets)}")
    print(f"{'='*60}")

    # Layout summary
    print("\nLayout:")
    for wid_key, lay in sorted(layout_widgets.items(),
                               key=lambda x: (x[1]["row"], x[1]["col"])):
        w = widgets[wid_key]
        t = w.get("config", {}).get("title") or ""
        settings = w.get("config", {}).get("settings", {})
        # For value_cards show the label from dataKey
        if not t:
            ds = w["config"].get("datasources", [])
            if ds and ds[0].get("dataKeys"):
                t = ds[0]["dataKeys"][0].get("label", "?")
        tp = w.get("typeFullFqn", "?").split(".")[-1]
        r, c = lay["row"], lay["col"]
        sx, sy = lay["sizeX"], lay["sizeY"]
        date_flag = " [+LastUpdate]" if settings.get("showDate") else ""
        print(f"  r={r:>2} c={c:>2} {sx:>2}x{sy}  {tp:>30} | {t}{date_flag}")


if __name__ == "__main__":
    main()
