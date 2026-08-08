# AMI LwM2M Node — ESP32-C6 Thread + LwM2M smart-metering firmware

Production firmware + fleet tooling for an AMI (Advanced Metering Infrastructure)
mesh: ESP32-C6 SuperMini nodes running **LwM2M over OpenThread (FTD)**, reporting
DLMS/COSEM-style telemetry to a **ThingsBoard Edge** server behind a **Raspberry-Pi
OpenThread Border Router (OTBR)**.

**Fleet running: `0.7.17-ami`.  Ready to deploy: `0.7.21-ami`** (bench-validated
2026-08-07, not yet on the fleet). `0.7.14-otacfm` was the last release with a
30-node 2-hour soak — rock-steady, eidcache 5% retry flat, 0 watchdog reboots —
which is what validated scaling to 60 nodes.

What `0.7.21` carries over `0.7.17`, all of it fixing nodes that die silently:

- **`0.7.18`** — reboot-cause tags, crash-PC capture (Obj 33000 RIDs 39-41), coredump to flash
- **`0.7.19`** — the boot-burst throttle no longer starves the boot watchdog (that
  combination was a *permanent* reboot loop OTA could never fix); OT bring-up
  verifies its state instead of swallowing `OT_ERROR_INVALID_STATE` (which left
  nodes running with the radio disabled, logging "Thread started")
- **`0.7.20`** — the fatal handler stamps the crash site *before* logging. It used
  to call `LOG_PANIC()` first, which hangs under deferred logging, so the panic
  forensics never produced a single record

Start with [`docs/README.md`](docs/README.md); the two living documents are
[`docs/PENDIENTES.md`](docs/PENDIENTES.md) (fleet, open) and
[`docs/BENCH_FINDINGS_2026-08.md`](docs/BENCH_FINDINGS_2026-08.md) (bench, measured).

---

## Repo layout

| Path | What |
|---|---|
| `src/` | firmware (main.c, LwM2M objects, hw_watchdog, dlms_meter, thread_conn_monitor, …) |
| `tools/` | build / flash / OTA / deploy / diagnostics — **code only**, see [`tools/README.md`](tools/README.md) |
| `docs/` | operational documentation — see [`docs/README.md`](docs/README.md) |
| `overlays/` | build overlays: variant (`med`, `ftd`), mesh target (`pi4`, `r1000`, `lab`), experiments |
| `models/` | LwM2M object-model XMLs uploaded to TB Edge (Object 33000, 3, 5, 10242, 3303) |
| `tests/` | host unit tests (DLMS logic, OBIS Group 1) |
| `captures/` | **all measured data** (git-ignored). Tools resolve it via `tools/lab_paths.py`; override with `AMI_CAPTURES_DIR` |
| `build_archive/` | one ELF per flashed version. `build_info.txt` is tracked, the binaries are not — a crash address is meaningless without the exact ELF |
| `west.yml` | pins the exact Zephyr revision (4.3.99 / `6159cb3`) |
| `requirements.txt` | Python tool deps |

**Code and data are kept apart on purpose.** Captures used to be written next to
the scripts that produced them, which grew `tools/` to 1.9 GB of CSV — including
one 1.34 GB PPK2 stream — and made `.gitignore` accrete one pattern per filename
shape. A new tool that writes data should call `lab_paths.captures_dir()` and
needs no `.gitignore` entry.

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
# -> build_prod/ami-lwm2m-node/zephyr/zephyr.signed.bin  (0.7.21-ami, board xiao_esp32c6/esp32c6/hpcore)

# Archive the ELF for every build you flash — a crash address cannot be resolved without it
python ami-lwm2m-node/tools/archive_build.py --build-dir build_prod/ami-lwm2m-node
```

**Check before shipping any image**: `CONFIG_AMI_TEST_FAULT` must be absent, and
the string `BENCH ONLY` must not appear in the binary. That option compiles in
`ami test panic`, which crashes the node on command — bench only, never a fleet
image. It defaults to `n`; the check exists because the cost of being wrong is a
remote node you can crash.

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

## Two environments — know which one you are on

| | Sandbox (this workstation) | Production (the fleet) |
|---|---|---|
| Mesh | `--mesh lab` — SONOFF dongle + OTBR in WSL2 | `--mesh pi4` (active) / `--mesh r1000` (legacy) |
| Server | ThingsBoard in docker, `tools/lab_tb/` | ThingsBoard Edge on the Pi |
| Nodes | 1-2 on the bench, instrumented (PPK2 / FNB-C2) | ~60 in the field |
| Build | `build_firmware.py --mesh lab` — plain `zephyr.bin`, `dio/80m @0x0` | `build_prod.py` — **signed** image, MCUboot |
| Bring-up | `python tools/lab_restore.py` | already running |

A bench build carries `CONFIG_AMI_MESH_LAB` and its own credentials, so it
cannot join the production mesh by accident. Fault-injection builds additionally
carry their own version string (`0.7.21-fault`) — two binaries sharing a version
would make `addr2line` resolve crash addresses against the wrong ELF.

### Production infrastructure (NOT in this repo)
- **OTBR + ThingsBoard Edge @ `192.168.1.111`** — active fleet, channel 25
  (SSH root:root; TB tenant@thingsboard.org / tenant; TB Edge :8090; Grafana :3000)
- `192.168.8.111` (R1000) is **legacy**
- Tools take `--mesh`; host constants live in `tools/fleet_common.py`

## Key learnings (baked into the tooling)
- **OTA confirms on Thread-attach** (not after REGISTER) → no rollback on a busy mesh.
- **Flash recipe**: spread boards across USB ports / direct-to-PC / good cables / small batches (~100%) — never cluster many on one hub (~29%). Then update via **OTA** (wedge-free).
- **Operate on clean power** (PSU/mains, no USB hub) — the chronic "brownout"/flapping was USB-hub power/connection, not firmware.
- **Stable firmware keeps the eidcache clean** → the address-resolution collapse was churn, not node count.
