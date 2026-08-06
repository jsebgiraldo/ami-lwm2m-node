"""Generate + upload the LwM2M object-model XMLs the AMI fleet needs to
TB Edge's resource library.

Without these, TB Edge logs `LwM2mVersionedModelProvider - Tenant hasn't
such the resource: Object model with id [10242] version [1.0]` and silently
drops every observation notification — so registrations succeed but no
telemetry is ever mapped (0 keys per device). The TB Edge re-install wiped
the resource library; this script restores it.

Objects covered:
  10242 v1.0  — Custom 3-Phase Power Meter (no XML in repo before; built
                from src/lwm2m_obj_power_meter.h)
  33000 v2.2  — Custom Thread + LwM2M Diagnostics (repo had only v2.0;
                rebuilt with all RIDs 0..22 from src/lwm2m_obj_thread_diag.h)
  3303  v1.1  — IPSO Temperature (standard OMA, written from spec)

The XMLs are written to ./models/ (single source of truth, version
controlled) AND uploaded to TB Edge via /api/resource. Idempotent.

Usage:
    python tools/tb_edge_upload_models.py             # generate + upload
    python tools/tb_edge_upload_models.py --dry-run   # generate only
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

import fleet_common as fc

fc.bootstrap_venv()

import requests  # noqa: E402

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"


# ─────────────────────── XML generator ──────────────────────────────────
def _item(rid, name, ops, mandatory, type_, desc, units="", multi="Single",
          range_="") -> str:
    return (f"      <Item ID=\"{rid}\">\n"
            f"        <Name>{name}</Name>\n"
            f"        <Operations>{ops}</Operations>\n"
            f"        <MultipleInstances>{multi}</MultipleInstances>\n"
            f"        <Mandatory>{mandatory}</Mandatory>\n"
            f"        <Type>{type_}</Type>\n"
            f"        <RangeEnumeration>{range_}</RangeEnumeration>\n"
            f"        <Units>{units}</Units>\n"
            f"        <Description>{desc}</Description>\n"
            f"      </Item>\n")


def _xml(obj_id, version, name, description, resources, urn_ns="x"):
    items = "".join(_item(*r) if isinstance(r, tuple) else _item(**r)
                    for r in resources)
    return (f"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            f"<LWM2M xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\"\n"
            f"       xsi:noNamespaceSchemaLocation=\""
            f"http://www.openmobilealliance.org/tech/profiles/LWM2M-v1_1.xsd\">\n"
            f"  <Object ObjectType=\"MODefinition\">\n"
            f"    <Name>{name}</Name>\n"
            f"    <Description1>{description}</Description1>\n"
            f"    <ObjectID>{obj_id}</ObjectID>\n"
            f"    <ObjectURN>urn:oma:lwm2m:{urn_ns}:{obj_id}:{version}</ObjectURN>\n"
            f"    <LWM2MVersion>1.1</LWM2MVersion>\n"
            f"    <ObjectVersion>{version}</ObjectVersion>\n"
            f"    <MultipleInstances>Single</MultipleInstances>\n"
            f"    <Mandatory>Optional</Mandatory>\n"
            f"    <Resources>\n"
            f"{items}    </Resources>\n"
            f"    <Description2/>\n"
            f"  </Object>\n"
            f"</LWM2M>\n")


# ─────────────────────── 10242 v1.0 ─────────────────────────────────────
def xml_10242() -> str:
    # tuple: (rid, name, ops, mandatory, type, desc, units, multi, range)
    res = [
        (0, "Manufacturer", "R", "Optional", "String", "Manufacturer of the meter."),
        (1, "Model Number", "R", "Optional", "String", "Meter model number."),
        (2, "Serial Number", "R", "Optional", "String", "Meter serial number."),
        (3, "Description", "R", "Optional", "String", "Human-readable description."),
        # Phase R (mandatory voltage + current)
        (4,  "Tension Phase R", "R", "Mandatory", "Float",
         "RMS line voltage on phase R.", "V"),
        (5,  "Current Phase R", "R", "Mandatory", "Float",
         "RMS current on phase R.", "A"),
        (6,  "Active Power Phase R",   "R", "Optional", "Float",
         "Active power on phase R.", "kW"),
        (7,  "Reactive Power Phase R", "R", "Optional", "Float",
         "Reactive power on phase R.", "kvar"),
        (10, "Apparent Power Phase R", "R", "Optional", "Float",
         "Apparent power on phase R.", "kVA"),
        (11, "Power Factor Phase R",   "R", "Optional", "Float",
         "Power factor on phase R, -1..1.", ""),
        # Phase S
        (14, "Tension Phase S", "R", "Optional", "Float", "RMS line voltage on phase S.", "V"),
        (15, "Current Phase S", "R", "Optional", "Float", "RMS current on phase S.", "A"),
        (16, "Active Power Phase S",   "R", "Optional", "Float", "Active power on phase S.", "kW"),
        (17, "Reactive Power Phase S", "R", "Optional", "Float", "Reactive power on phase S.", "kvar"),
        (20, "Apparent Power Phase S", "R", "Optional", "Float", "Apparent power on phase S.", "kVA"),
        (21, "Power Factor Phase S",   "R", "Optional", "Float", "Power factor on phase S, -1..1.", ""),
        # Phase T
        (24, "Tension Phase T", "R", "Optional", "Float", "RMS line voltage on phase T.", "V"),
        (25, "Current Phase T", "R", "Optional", "Float", "RMS current on phase T.", "A"),
        (26, "Active Power Phase T",   "R", "Optional", "Float", "Active power on phase T.", "kW"),
        (27, "Reactive Power Phase T", "R", "Optional", "Float", "Reactive power on phase T.", "kvar"),
        (30, "Apparent Power Phase T", "R", "Optional", "Float", "Apparent power on phase T.", "kVA"),
        (31, "Power Factor Phase T",   "R", "Optional", "Float", "Power factor on phase T, -1..1.", ""),
        # 3-phase aggregates
        (34, "3-Phase Active Power",   "R", "Optional", "Float", "Total 3-phase active power.", "kW"),
        (35, "3-Phase Reactive Power", "R", "Optional", "Float", "Total 3-phase reactive power.", "kvar"),
        (38, "3-Phase Apparent Power", "R", "Optional", "Float", "Total 3-phase apparent power.", "kVA"),
        (39, "3-Phase Power Factor",   "R", "Optional", "Float", "Aggregate 3-phase power factor.", ""),
        # Energies
        (41, "Active Energy",   "R", "Optional", "Float", "Cumulative active energy.", "kWh"),
        (42, "Reactive Energy", "R", "Optional", "Float", "Cumulative reactive energy.", "kvarh"),
        (45, "Apparent Energy", "R", "Optional", "Float", "Cumulative apparent energy.", "kVAh"),
        # Frequency
        (49, "Frequency", "R", "Optional", "Float", "Mains frequency.", "Hz"),
    ]
    return _xml(10242, "1.0", "3-Phase Power Meter",
                "Custom AMI object — DLMS/COSEM-backed 3-phase power meter "
                "(voltage, current, power, energy, frequency).",
                res)


# ─────────────────────── 33000 v2.2 ─────────────────────────────────────
def xml_33000() -> str:
    res = [
        # v2.0 — Thread MAC counters (0..9)
        (0, "Thread Role", "R", "Mandatory", "String",
         "Current Thread role: Disabled, Detached, Child, Router, or Leader."),
        (1, "Partition ID", "R", "Mandatory", "Integer", "Thread partition identifier."),
        (2, "TX Total",     "R", "Mandatory", "Integer", "Total MAC frames transmitted."),
        (3, "RX Total",     "R", "Mandatory", "Integer", "Total MAC frames received."),
        (4, "TX Unicast",   "R", "Mandatory", "Integer", "Unicast MAC frames transmitted."),
        (5, "RX Unicast",   "R", "Mandatory", "Integer", "Unicast MAC frames received."),
        (6, "TX Broadcast", "R", "Mandatory", "Integer", "Broadcast MAC frames transmitted."),
        (7, "RX Broadcast", "R", "Mandatory", "Integer", "Broadcast MAC frames received."),
        (8, "TX Error Abort",   "R", "Mandatory", "Integer", "MAC transmissions aborted due to errors."),
        (9, "RX Error No Frame","R", "Mandatory", "Integer", "MAC RX errors with no frame received."),
        # v2.1 — LwM2M client counters (10..16)
        (10, "Uptime",            "R", "Mandatory", "Integer", "Seconds since boot.", "s"),
        (11, "Reg Attempts",      "R", "Mandatory", "Integer", "Total lwm2m_rd_client_start() calls."),
        (12, "Reg Success",       "R", "Mandatory", "Integer", "REGISTRATION_COMPLETE events."),
        (13, "Notify Emitted",    "R", "Mandatory", "Integer", "Cumulative notifications sent."),
        (14, "Notify Throttled",  "R", "Mandatory", "Integer", "Cumulative notifications throttled."),
        (15, "Recover Count",     "R", "Mandatory", "Integer", "lwm2m_recover_work invocations."),
        (16, "Restart Success",   "R", "Mandatory", "Integer", "REGISTRATION_COMPLETE after a recovery."),
        # v2.2 — production hardening (17..20)
        (17, "Last Error Code",         "R", "Mandatory", "Integer",
         "Last errno from rd_client_start, negative or 0."),
        (18, "Last Error Uptime",       "R", "Mandatory", "Integer",
         "Uptime in seconds when the last error was recorded.", "s"),
        (19, "Watchdog Count",          "R", "Mandatory", "Integer",
         "Times the silence watchdog forced a recover."),
        (20, "Storm Backoff Applied",   "R", "Mandatory", "Integer",
         "Times backoff was doubled due to 5.xx/timeout responses."),
        # v2.3 — boot reliability counters (21..22), exposed in v2.2 model
        (21, "Last Reset Reason",       "R", "Mandatory", "Integer",
         "hwinfo_get_reset_cause() bitmap captured at boot; -1 if unavailable."),
        (22, "Total Resets",            "R", "Mandatory", "Integer",
         "Monotonic reset counter, persisted in NVS — un-fakeable proof of "
         "a watchdog-driven reboot."),
        # v2.4 — post-mortem: what the firmware was doing at the last hang (23..28).
        # Populated at boot from the NVS snapshot owned by src/post_mortem.c.
        (23, "Hang Uptime",             "R", "Mandatory", "Integer",
         "Uptime at the last snapshot before the previous reset.", "s"),
        (24, "Hang Heap Free",          "R", "Mandatory", "Integer",
         "Free heap bytes at that snapshot.", "B"),
        (25, "Hang Heap Min Free",      "R", "Mandatory", "Integer",
         "Lifetime minimum free heap up to that snapshot.", "B"),
        (26, "Hang Reg Age",            "R", "Mandatory", "Integer",
         "Seconds since the last REG_UPDATE_COMPLETE at that snapshot.", "s"),
        (27, "Hang LwM2M State",        "R", "Mandatory", "Integer",
         "Bitmap: 0x01 started, 0x02 registered, 0x04 observing, "
         "0x08 recovering, 0x80 last_error set."),
        (28, "Hang Thread Role",        "R", "Mandatory", "Integer",
         "OpenThread role at that snapshot: 0 disabled, 1 detached, 2 child, "
         "3 router, 4 leader."),
        # v2.5 — deadlock observability (29..33). Together these separate
        # "alive but engine-suspended" from "server outage" from "boot loop".
        (29, "Keepalive Emit Count",    "R", "Mandatory", "Integer",
         "Heartbeats emitted; the silence watchdog's liveness signal."),
        (30, "Keepalive Consec Fail",   "R", "Mandatory", "Integer",
         "Consecutive keepalive failures the engine is otherwise hiding."),
        (31, "Last Emit Uptime",        "R", "Mandatory", "Integer",
         "Uptime when the watchdog last saw a sign of life.", "s"),
        (32, "NoReg Boots",             "R", "Mandatory", "Integer",
         "Consecutive boots that never reached a first REGISTER."),
        (33, "In Recovery",             "R", "Mandatory", "Integer",
         "1 while recover_work_fn is running, else 0."),
        # v2.6 — at-risk indicators: a board trending toward brick before it fails.
        (34, "Boot Burst",              "R", "Mandatory", "Integer",
         "Consecutive unstable boots; 0 = healthy. Alarm as it approaches "
         "CONFIG_AMI_BOOT_BURST_MAX."),
        (35, "Detached Total",          "R", "Mandatory", "Integer",
         "Cumulative seconds spent detached from the mesh.", "s"),
        (36, "Heap Min Free Live",      "R", "Mandatory", "Integer",
         "Live lifetime minimum free heap for this boot.", "B"),
        # v0.7.4 — which reboot path caused this boot. See the canonical code
        # map in src/main.c (ami_reboot_reason_to_code + the comment below it).
        (37, "Last Reboot Code",        "R", "Mandatory", "Integer",
         "Reboot path that caused this boot: 1-10 planned recovery paths, "
         "11 kernel panic, 12-15 hardware-watchdog paths, 16 OTA apply, "
         "99 unknown, 0 = did not pass through firmware (power-on, brownout "
         "or external reset)."),
        # v2.7 — exact comms accounting (RID 38; observability Tier 2). Added
        # 2026-06-21, and the precedent that proved a "1.0" model accepts new
        # resources — which is why 23-37 are advertised here as of v0.7.18.
        (38, "LwM2M TX Bytes",          "R", "Mandatory", "Integer",
         "Exact cumulative LwM2M/CoAP bytes put on the wire since boot, counted "
         "at the engine send chokepoint. Server computes bytes/min as a delta "
         "and avg packet size as tx_bytes_delta / notify_emitted_delta.", "B"),
        # v2.8 (v0.7.18) — panic crash site of the PREVIOUS boot. Feed 40 and 41
        # to `riscv64-zephyr-elf-addr2line -f -e zephyr.elf` using the ELF of the
        # exact build that was running, or the addresses are meaningless.
        (39, "Panic Reason",            "R", "Mandatory", "Integer",
         "K_ERR_* code passed to k_sys_fatal_error_handler; 0 = no panic."),
        (40, "Panic MEPC",              "R", "Mandatory", "Integer",
         "Address of the faulting instruction."),
        (41, "Panic RA",                "R", "Mandatory", "Integer",
         "Return address at the fault — one caller frame up from MEPC."),
    ]
    # ObjectVersion="1.0" deliberate: firmware compiles with
    # CONFIG_LWM2M_VERSION_1_0=y so REGISTER payload omits per-object ;ver=
    # attributes. TB Edge falls back to clientLwM2mSettings.defaultObjectIDVer
    # ("1.0") for ALL objects and demands an EXACT model match for experimental
    # object IDs (>=32768). With a 2.2 model uploaded, the firmware's bare
    # `</33000>` advertisement matched nothing and TB silently skipped
    # observes/telemetry for the entire object (no log line). Standard OMA
    # IDs like 3303 use built-in fallback + any uploaded model is augmented,
    # which is why 3303_1.1 worked. We keep our internal object_version=2.2
    # in firmware (thread_conn_monitor.c) for evolution tracking; the wire
    # version stays "1.0" to match the TB profile and uploaded model.
    return _xml(33000, "1.0", "Thread MAC + LwM2M Diagnostics",
                "Custom AMI object combining Thread MAC counters, LwM2M "
                "client observability counters, and boot-reliability state "
                "for un-fakeable watchdog telemetry.",
                res)


# ─────────────────────── 3303 v1.1 (IPSO) ───────────────────────────────
def xml_3303() -> str:
    # IPSO Temperature v1.1 — minimum subset the firmware exposes.
    res = [
        (5601, "Min Measured Value", "R", "Optional", "Float",
         "Minimum value of the sensor since boot or last reset.", "Cel"),
        (5602, "Max Measured Value", "R", "Optional", "Float",
         "Maximum value of the sensor since boot or last reset.", "Cel"),
        (5603, "Min Range Value", "R", "Optional", "Float",
         "Lower bound of the sensor's measurement range.", "Cel"),
        (5604, "Max Range Value", "R", "Optional", "Float",
         "Upper bound of the sensor's measurement range.", "Cel"),
        (5605, "Reset Min and Max Measured Values", "E", "Optional", "",
         "Execute to clear the Min/Max measured-value pair."),
        (5700, "Sensor Value", "R", "Mandatory", "Float",
         "Current measured value of the temperature sensor.", "Cel"),
        (5701, "Sensor Units", "R", "Optional", "String",
         "Measurement unit string (e.g. 'Cel').", ""),
        (5518, "Timestamp", "R", "Optional", "Time",
         "v1.1 — Timestamp of the latest measurement.", ""),
    ]
    return _xml(3303, "1.1", "Temperature",
                "IPSO Object 3303 v1.1 — generic temperature sensor "
                "(used here for ESP32-C6 SoC die temperature).",
                res, urn_ns="ext")


# ─────────────────────── 3 v1.0 (OMA Device) ────────────────────────────
def xml_3() -> str:
    # OMA standard Device object. Wiped by the TB Edge re-install along with
    # every other built-in model; the firmware advertises it at the profile
    # default version 1.0, so TB needs an exact id=3 ver=1.0 model or it logs
    # "Tenant hasn't such the resource: Object model with id [3] version [1.0]"
    # and can't type /3/0/3 (Firmware Version) — which breaks the OTA version
    # comparison.
    res = [
        (0,  "Manufacturer", "R", "Optional", "String", "Manufacturer name."),
        (1,  "Model Number", "R", "Optional", "String", "Model identifier."),
        (2,  "Serial Number", "R", "Optional", "String", "Serial number."),
        (3,  "Firmware Version", "R", "Optional", "String", "Firmware version of the device."),
        (4,  "Reboot", "E", "Mandatory", "", "Reboot the device."),
        (5,  "Factory Reset", "E", "Optional", "", "Perform a factory reset."),
        (6,  "Available Power Sources", "R", "Optional", "Integer", "Available power sources.", "", "Multiple"),
        (7,  "Power Source Voltage", "R", "Optional", "Integer", "Present voltage per power source.", "mV", "Multiple"),
        (8,  "Power Source Current", "R", "Optional", "Integer", "Present current per power source.", "mA", "Multiple"),
        (9,  "Battery Level", "R", "Optional", "Integer", "Battery level percentage.", "%"),
        (10, "Memory Free", "R", "Optional", "Integer", "Free memory.", "KB"),
        (11, "Error Code", "R", "Mandatory", "Integer", "Current error codes.", "", "Multiple"),
        (12, "Reset Error Code", "E", "Optional", "", "Clear the Error Code list."),
        (13, "Current Time", "RW", "Optional", "Time", "Current UNIX time."),
        (14, "UTC Offset", "RW", "Optional", "String", "UTC offset, e.g. +02:00."),
        (15, "Timezone", "RW", "Optional", "String", "IANA timezone name."),
        (16, "Supported Binding and Modes", "R", "Mandatory", "String", "Supported LwM2M bindings."),
        (17, "Device Type", "R", "Optional", "String", "Type of device."),
        (18, "Hardware Version", "R", "Optional", "String", "Hardware version."),
        (19, "Software Version", "R", "Optional", "String", "Software version."),
        (20, "Battery Status", "R", "Optional", "Integer", "Battery status enum."),
        (21, "Memory Total", "R", "Optional", "Integer", "Total memory.", "KB"),
    ]
    return _xml(3, "1.0", "Device",
                "OMA LwM2M Device object (standard) — re-uploaded to restore "
                "the wiped built-in model so TB can type /3/0/3 Firmware Version.",
                res, urn_ns="oma")


# ─────────────────────── 5 v1.0 (OMA Firmware Update) ────────────────────
def xml_5() -> str:
    # OMA standard Firmware Update object. THIS is the one that blocks OTA:
    # with no id=5 ver=1.0 model TB cannot encode the block-write to /5/0/0
    # nor observe /5/0/3 (State) / /5/0/5 (Result), so fw_state jumps straight
    # to FAILED and zero blocks are ever sent.
    res = [
        (0, "Package", "W", "Mandatory", "Opaque", "Firmware package pushed block-wise."),
        (1, "Package URI", "W", "Mandatory", "String", "URI to pull the firmware from."),
        (2, "Update", "E", "Mandatory", "", "Execute to apply the downloaded firmware."),
        (3, "State", "R", "Mandatory", "Integer",
         "FW update state: 0=Idle,1=Downloading,2=Downloaded,3=Updating.", "", "Single", "0..3"),
        (5, "Update Result", "R", "Mandatory", "Integer",
         "Result of the last update: 0=initial,1=success,...", "", "Single", "0..9"),
        (6, "PkgName", "R", "Optional", "String", "Name of the firmware package."),
        (7, "PkgVersion", "R", "Optional", "String", "Version of the firmware package."),
        (8, "Firmware Update Protocol Support", "R", "Optional", "Integer",
         "Supported transport protocols (0=CoAP).", "", "Multiple"),
        (9, "Firmware Update Delivery Method", "R", "Mandatory", "Integer",
         "0=Pull only,1=Push only,2=both."),
    ]
    return _xml(5, "1.0", "Firmware Update",
                "OMA LwM2M Firmware Update object (standard) — re-uploaded to "
                "restore the wiped built-in model so TB Edge can drive the "
                "block-wise OTA push to /5/0/0.",
                res, urn_ns="oma")


# ─────────────────────── upload ─────────────────────────────────────────
def login(host, port, user, password) -> requests.Session:
    s = requests.Session()
    r = s.post(f"http://{host}:{port}/api/auth/login",
               json={"username": user, "password": password}, timeout=15)
    r.raise_for_status()
    s.headers.update({"Authorization": f"Bearer {r.json()['token']}",
                      "Content-Type": "application/json"})
    return s


def upload_model(s: requests.Session, base: str, title: str, filename: str,
                 xml: str) -> None:
    # Look up existing by title; if present, update id-bound. The
    # /api/resource endpoint accepts POST for both create and update.
    # NOTE pageSize: a stock ThingsBoard CE ships ~200 SYSTEM LwM2M models (the
    # whole OMA registry), so a small page can hide our own tenant resources and
    # cause duplicate creates. Ask for a page big enough to hold both sets.
    existing = s.get(f"{base}/api/resource?pageSize=1000&page=0",
                     timeout=15).json()
    # Only ever id-bind (i.e. UPDATE) a resource this TENANT owns. Stock CE
    # already provides system-scope models for the standard objects (3.xml,
    # 5.xml, 3303.xml ...); matching one of those and POSTing with its id is an
    # attempt to modify a system resource and TB answers
    #   403 "You don't have permission to perform this operation!"
    # Ignoring system matches makes this a CREATE instead, producing a
    # tenant-scoped model that shadows the system one for this tenant.
    # (Bench-hit 2026-08-04 on a fresh CE install; the fleet Edge had no system
    # models so the old code never saw it.)
    SYSTEM_TENANT_ID = "13814000-1dd2-11b2-8080-808080808080"

    def _tenant_owned(r: dict) -> bool:
        return (r.get("tenantId") or {}).get("id") != SYSTEM_TENANT_ID

    match = next((r for r in existing.get("data", [])
                  if (r.get("title") == title or r.get("fileName") == filename)
                  and _tenant_owned(r)),
                 None)
    body = {
        "title": title,
        "resourceType": "LWM2M_MODEL",
        "fileName": filename,
        "data": base64.b64encode(xml.encode("utf-8")).decode("ascii"),
    }
    if match:
        body["id"] = match["id"]
        action = "update"
    else:
        action = "create"
    r = s.post(f"{base}/api/resource", data=json.dumps(body), timeout=30)
    if not r.ok:
        raise RuntimeError(f"  upload FAILED {r.status_code}: {r.text[:300]}")
    print(f"  uploaded ({action}): {title}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mesh", default=fc.DEFAULT_MESH, choices=fc.MESH_TARGETS)
    ap.add_argument("--user", default=fc.EDGE_TENANT_USER)
    ap.add_argument("--password", default=fc.EDGE_TENANT_PASS)
    ap.add_argument("--dry-run", action="store_true",
                    help="generate XMLs to ./models/ but skip the upload")
    args = ap.parse_args()

    MODELS_DIR.mkdir(exist_ok=True)
    artefacts = [
        ("Object 3 v1.0 - Device",                  "3.xml",     xml_3()),
        ("Object 5 v1.0 - Firmware Update",         "5.xml",     xml_5()),
        ("Object 10242 v1.0 - 3-Phase Power Meter", "10242.xml", xml_10242()),
        ("Object 33000 v2.2 - Thread + LwM2M Diag", "33000.xml", xml_33000()),
        ("Object 3303 v1.1 - IPSO Temperature",     "3303.xml",  xml_3303()),
    ]
    print("=== generating XML models ===")
    for title, fname, xml in artefacts:
        path = MODELS_DIR / fname
        path.write_text(xml, encoding="utf-8")
        print(f"  wrote {path}  ({len(xml)} bytes)")

    if args.dry_run:
        print("=== dry-run: skipping TB Edge upload ===")
        return 0

    host, port = fc.edge_for_mesh(args.mesh)
    base = f"http://{host}:{port}"
    print(f"=== uploading to TB Edge {base} ===")
    s = login(host, port, args.user, args.password)
    for title, fname, xml in artefacts:
        upload_model(s, base, title, fname, xml)
    print("=== done — RESTART tb-edge-v2 to load the new models into "
          "Leshan's LwM2mModelProvider ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
