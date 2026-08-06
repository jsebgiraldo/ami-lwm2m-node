# tools/lab_tb — bench ThingsBoard for the LAB Thread mesh

Stands up a **real LwM2M server** on this PC (Windows 11 + WSL2 Ubuntu-24.04 +
native `otbr-agent` + docker-in-WSL) so the lab node can actually **REGISTER**.

> **Start here instead if you are bringing the bench up:**
> [`docs/LAB_THINGSBOARD.md`](../../docs/LAB_THINGSBOARD.md) is the single
> ordered runbook (prerequisites -> TB -> transport bound -> DNS-SD published ->
> node resolves -> provision -> registration/telemetry -> teardown) with the
> troubleshooting matrix. This README is the per-tool reference behind it.

Why it matters: the node discovers its server only via DNS-SD over Thread
(`src/lwm2m_discover.c`) and then registers. With no server, `overlays/lab.conf`
has to disable `CONFIG_AMI_BOOT_REGISTER_DEADLINE_S` and stretch the HW-watchdog
boot grace to 3600 s — so the bench validates a configuration we would never
ship, and every registration / observe / RPC / OTA path stays untested. This
stack removes that divergence.

| file | owns |
|---|---|
| `docker-compose.yml`, `.env.example` | the TB CE stack (`tb-lab` + `tb-lab-postgres`, host networking) |
| `lab_tb_up.sh`, `lab_tb.ps1` | bring-up / verify / restart / reset driver |
| `srp_publish_lwm2m.sh`, `otbr-srp-lwm2m.service` | publishing the DNS-SD record the node resolves |
| `lab_tb_provision.py` | object models + device profile + device + NoSec credentials |
| `lab_tb_check.py` | end-to-end PASS/FAIL of the whole chain |

Ports: **8080** web UI + REST, **5683** LwM2M NoSec (pinned — the firmware's
Strategy-2 discovery hardcodes 5683), CoAP moved to 5690 so it cannot steal it.

---

## The chain, in order

Each step fails **silently** if skipped, so run them in this order.

```powershell
# 1. stack up (first run: preflight -> pull -> install -> up -> verify)
./tools/lab_tb/lab_tb.ps1 -Action bootstrap
./tools/lab_tb/lab_tb.ps1 -Action verify

# 2. publish the SRP/DNS-SD record the node looks for
python tools/lab_tb/lab_tb_srp.py install
./tools/lab_tb/lab_tb.ps1 -Action srpinfo

# 3. provision models + profile + device (this restarts TB when a model changed)
C:/Users/jsgir/Documents/ESP32/.venv/Scripts/python.exe tools/lab_tb/lab_tb_provision.py

# 4. power-cycle the node, then gate the bench
C:/Users/jsgir/Documents/ESP32/.venv/Scripts/python.exe tools/lab_tb/lab_tb_check.py --strict
```

A profile change reaches a device only on REGISTER — after any profile edit,
power-cycle the node or send it a Reboot RPC.

---

## 3. Provision (`lab_tb_provision.py`)

```powershell
$PY = "C:/Users/jsgir/Documents/ESP32/.venv/Scripts/python.exe"

& $PY tools/lab_tb/lab_tb_provision.py                      # canonical
& $PY tools/lab_tb/lab_tb_provision.py --dry-run            # plan only
& $PY tools/lab_tb/lab_tb_provision.py --uptime-pmax 60 --full-33000
& $PY tools/lab_tb/lab_tb_provision.py --host 172.x.x.x --port 8080
```

Nothing about "the production shape" is re-authored here — it is imported:

| imported from | what it provides |
|---|---|
| `tools/tb_edge_provision.py` | `build_profile_body()`, `apply_profile()`, `provision_device()` (LWM2M / NO_SEC) |
| `tools/tb_edge_upload_models.py` | `xml_3/5/10242/33000/3303()` generators + `upload_model()` |
| `tools/tb_edge_monitoring_setup.py` | `OBSERVE_ADDITIONS` — authoritative observe + pmin/pmax superset |
| `tools/fleet_common.py` | endpoint derivation, tenant creds, profile name, venv bootstrap |

Steps:

1. **wait for TB** — a cold stack answers the TCP port 60–180 s before it
   answers `/api/auth/login`. Three consecutive 401s abort with the real cause
   (an install without demo data has no `tenant@thingsboard.org`).
2. **upload 6 object models** — 3, 5, 10242, 33000, 3303, 33001, read from
   `models/` and regenerated from the production generators if a file is
   missing. Byte-identical models are skipped.
3. **restart `tb-lab`** — Leshan loads models into `LwM2mModelProvider` at
   **startup only**; models uploaded after a device registered do not
   retroactively fix dropped observes. `--restart auto` (default) restarts only
   when a model actually changed; `--restart always|never` to force.
4. **device profile `AMI_LwM2M_Node`** — production body, then
   `OBSERVE_ADDITIONS`, then the bench pmax policy below.
5. **device + credentials** — `LWM2M_CREDENTIALS` / `NO_SEC`,
   `credentialsId == ami-esp32c6-3bb0` (derived from MAC `98:a3:16:61:3b:b0`,
   same rule as the firmware's `build_endpoint_name()`). `provisionType` is
   `DISABLED`, so an unprovisioned endpoint is rejected with
   `LwM2MAuthException` and the node reboot-loops.
6. **per-device `inactivityTimeout` = 20 min** — overrides the stack-wide
   `DEFAULT_INACTIVITY_TIMEOUT`; the *Node Offline* alarm keys straight off the
   `active` flag it drives.

### Bench deviations (all printed on every run, all opt-out)

| deviation | why | flag |
|---|---|---|
| every observed path gets `pmax >= 900 s` | production leaves `/10242_1.0/0/4,5,6` at `pmax=0` — no heartbeat, so a steady load is indistinguishable from a dead node | `--pmax-floor 0` |
| `uptime_s` pmax knob | 300 s = production; 60 s makes the checker converge in ~2 min | `--uptime-pmax` |
| device `inactivityTimeout` 20 min | one node on sparse LwM2M traffic flaps `active` | `--inactivity-min` |
| 33000 model extended to RIDs 23..37 | the firmware implements them and `OBSERVE_ADDITIONS` observes 23..36, but the shipped model defines only 0..22 + 38, so they can never be typed | `--full-33000` (**off** by default) |

`ObjectVersion` stays **1.0** in every case. Bumping it is the trap that made TB
silently drop the whole object: experimental IDs ≥ 32768 need an exact model
match, and the firmware's LwM2M-1.0 REGISTER emits a bare `</33000>` with no
`;ver=`, so TB falls back to `defaultObjectIDVer` = `"1.0"`.

The run also prints a **coverage report** — observed 33000 RIDs that the
uploaded model does not define (those keys can never appear) — and warns when
the observe list exceeds `CONFIG_LWM2M_ENGINE_MAX_OBSERVER=36`, the client-side
table size past which the node silently refuses observes.

---

## 4. Check (`lab_tb_check.py`)

```powershell
& $PY tools/lab_tb/lab_tb_check.py            # quick status
& $PY tools/lab_tb/lab_tb_check.py --strict   # gate before a soak
& $PY tools/lab_tb/lab_tb_check.py --strict --json
```

Exit code **0** all passed, **1** at least one FAIL, **2** TB unreachable
(nothing else could be evaluated). `--strict` sets `--delta-wait 120` and
promotes every WARN to a FAIL. `--json` appends one machine-readable line.

### Pass criteria (quantitative)

| # | check | PASS requires |
|---|---|---|
| 1 | `thingsboard` | `POST /api/auth/login` returns a token, typically < 2 s |
| 2 | `lwm2m transport` | UDP **5683** bound in the WSL netns (`/proc/net/udp6`) |
| 2b | `lwm2m bind log` | `tb-lab` log has `Started endpoint at coap://[0:0:0:0:0:0:0:0]:5683` and **no** `BindException` |
| 3 | `srp server` | `ot-ctl srp server state` = `running` |
| 3b | `srp host` | `thingsboard-edge.default.service.arpa.` present, `deleted: false` |
| 3c | `srp service` | `ThingsBoard-Edge._lwm2m._udp.default.service.arpa.`, `deleted: false`, **`port:5683`** |
| 3d | `srp address` | ≥ 1 advertised address **outside** the mesh-local /64 (an OMR address such as `fdaf:e549:1751:1:…`) |
| 4 | `device profile` | `transportType=LWM2M`, `/33000_1.0/0/10` in `observe`, its `pmax > 0` |
| 4b | `observer budget` | observed paths ≤ **36** — WARN above |
| 4c | `device` | exists, `LWM2M_CREDENTIALS`, `NO_SEC`, `credentialsId == ami-esp32c6-3bb0` |
| 5 | `registered` | server-scope `lastActivityTime` age ≤ **360 s** (`--max-age`), `active=true` |
| 6 | `telemetry` | ≥ **8** Object-33000 keys (`--min-keys`) and `uptime_s` age ≤ **360 s** |
| 6b | `telemetry live` | `uptime_s` **strictly increases** over a **120 s** window (`--strict`) |
| 7 | `inbound rpc` | two-way `Read /3/0/3` returns a value within 15 s |

*Why 360 s:* `uptime_s` runs `pmin/pmax = 60/300`, so a healthy node notifies at
least every 300 s; 360 s is that plus 20 % slack. Provision with
`--uptime-pmax 60` and check with `--max-age 90` for a tighter gate.

*Why 6b and 7 are separate checks:* a path present in `keyName`/`telemetry` but
missing from `observe` shows its **registration-payload** value forever (this
repo's #1 recurring bug), and a node can keep REGISTERing outbound long after
inbound delivery is black-holed. `lastActivityTime` alone proves neither.

---

## 5. After it passes — close the loop

`overlays/lab.conf` ships these two lines *because* the bench had no server:

```
CONFIG_AMI_BOOT_REGISTER_DEADLINE_S=0
CONFIG_AMI_HW_WATCHDOG_BOOT_GRACE_HARD_S=3600
```

They are exactly what makes the bench unrepresentative. Once
`lab_tb_check.py --strict` passes, remove them so the node inherits the
`prj.conf` production values, and rebuild:

```powershell
python tools/build_firmware.py --variant med --mesh lab
```

## Troubleshooting

* **Node alive but not registered?** Ask the node directly — its CoAP server
  answers independently of ThingsBoard:
  `python tools/diag_get.py --local --addr <node OMR address>`.
  That isolates "server missing" from "node dead" without touching a COM port.
* **SRP record missing?** That is check 3 and it is *the* gate: the firmware has
  had no static server IP since v0.6.65 (`prj.conf:389`).
  `./tools/lab_tb/lab_tb.ps1 -Action srpinfo`
* **Telemetry frozen?** The path is missing from the profile's `observe` list.
  Re-run the provisioner, then power-cycle the node.
* **Other fleet tools against the bench:** `--mesh lab` now resolves to
  `127.0.0.1:8080` (`fleet_common.MESH_TO_EDGE`), so e.g.
  `python tools/tb_edge_monitoring_setup.py --mesh lab` adds the alarm /
  dashboard layer. `lab_tb_provision.py --monitoring` does the same inline.
