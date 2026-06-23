# AMI LwM2M Node — ESP32-C6 Thread + LwM2M smart-metering firmware

Production firmware + fleet tooling for an AMI (Advanced Metering Infrastructure)
mesh: ESP32-C6 SuperMini nodes running **LwM2M over OpenThread (FTD)**, reporting
DLMS/COSEM-style telemetry to a **ThingsBoard Edge** server behind a **Raspberry-Pi
OpenThread Border Router (OTBR)**.

**Validated release: `0.7.14-otacfm`.** A 30-node fleet on this build passed a 2-hour
soak rock-steady (eidcache 5% retry flat, channel CCA flat, 0 watchdog reboots) →
**scaling to 60 nodes is validated**. See `docs/` and the project memory for the full
engineering story (delivery-liveness gate, robust OTA, the power/USB findings).

---

## Repo layout

| Path | What |
|---|---|
| `src/` | firmware (main.c, LwM2M objects, hw_watchdog, dlms_meter, thread_conn_monitor, …) |
| `tools/` | build / flash / OTA / deploy / diagnostics (Python) + the out-of-tree Zephyr patch |
| `docs/` | `DEPLOY_RUNBOOK.md`, `OTA_ANALISIS.md`, `ARQUITECTURA_main.md`, `ONBOARDING_COMMANDS.md` |
| `overlays/` | build overlays (ftd, prod_fat, sim, …) |
| `models/` | LwM2M object-model XMLs uploaded to TB Edge (Object 33000, 3, 5, 10242, 3303) |
| `tests/` | host unit tests (DLMS logic, OBIS Group 1) |
| `west.yml` | pins the exact Zephyr revision (4.3.99 / `6159cb3`) |
| `requirements.txt` | Python tool deps |

## Reproduce on a fresh machine

### 0. Prerequisites
- **Zephyr SDK 0.17.0** — install from <https://github.com/zephyrproject-rtos/sdk-ng/releases>.
- Python 3.11+, git, and (for flashing) the ESP32-C6 USB driver (native USB-Serial-JTAG, no extra driver on Win11).

### 1. Workspace + Zephyr (pinned)
```bash
mkdir ami-ws && cd ami-ws
git clone https://github.com/jsebgiraldo/ami-lwm2m-node.git
python -m venv .venv && . .venv/Scripts/activate     # (Linux: .venv/bin/activate)
pip install -r ami-lwm2m-node/requirements.txt
west init -l ami-lwm2m-node
west update                                          # pulls Zephyr @ 6159cb3 + modules
west zephyr-export
pip install -r zephyr/scripts/requirements.txt
```

### 2. Apply the out-of-tree Zephyr hook (exact TX-byte telemetry, Obj 33000 RID 38)
```bash
cd zephyr && git apply ../ami-lwm2m-node/tools/zephyr_lwm2m_txbytes.patch && cd ..
```
Details + re-apply notes: `ami-lwm2m-node/tools/ZEPHYR_PATCHES.md`. Re-apply after any `west update`.

### 3. Build the validated production firmware
```bash
python ami-lwm2m-node/tools/build_prod.py
# -> build_prod/ami-lwm2m-node/zephyr/zephyr.signed.bin  (0.7.14-otacfm, board xiao_esp32c6/esp32c6/hpcore)
```

### 4. Flash + deploy + diagnose
Full step-by-step (pinned config, USB-flash vs staged-OTA, validation, scaling discipline):
**`docs/DEPLOY_RUNBOOK.md`**. Quick reference:
```bash
PY=python; BIN=build_prod/ami-lwm2m-node/zephyr/zephyr.signed.bin
$PY tools/flash_fleet_seq.py --coms COM19,COM20,... --build-dir build_prod   # factory USB flash
$PY tools/deploy_fleet_staged.py --version 0.7.14-otacfm --bin "$BIN" --all   # staged OTA updates
$PY tools/net_capacity.py --mins 30        # per-node delivery + stability + mesh
$PY tools/net_soak.py --hours 2 --scale-to 60   # soak + scaling verdict
$PY tools/grafana_setup.py                  # 'AMI Comms' dashboard
```

## Infrastructure (the network side — NOT in this repo)
The deploy/diagnostic tools talk to fixed infra (defaults baked into the tools):
- **OTBR + ThingsBoard Edge @ `192.168.8.111`** (SSH root:root; TB tenant@thingsboard.org / tenant; TB Edge :8090; Grafana :3000).
- The OTBR holds the active Thread dataset (PAN `0xEFEB`, channel 25). To target different infra, edit the host constants in `tools/fleet_common.py` and the tool headers.

## Key learnings (baked into the tooling)
- **OTA confirms on Thread-attach** (not after REGISTER) → no rollback on a busy mesh.
- **Flash recipe**: spread boards across USB ports / direct-to-PC / good cables / small batches (~100%) — never cluster many on one hub (~29%). Then update via **OTA** (wedge-free).
- **Operate on clean power** (PSU/mains, no USB hub) — the chronic "brownout"/flapping was USB-hub power/connection, not firmware.
- **Stable firmware keeps the eidcache clean** → the address-resolution collapse was churn, not node count.
