# `tools/` — what to run, and what is only history

167 scripts accumulated over ~18 months of fleet incidents. Most of them you
will never run. This page names the ~35 that are current, so nobody has to
guess which of eleven `build_*.py` is the real one.

**Naming convention that tells you what you are looking at:** a script with a
**version or node id in its name** (`deep_diag_v069.py`, `flash_1494_kafix.py`,
`soak_7_v0673.py`, `check_v23_rids.py`, `build_resprobe_kafix.py`) is **frozen
incident tooling**. It was written to answer one question about one release,
it is kept because it documents how that answer was obtained, and it is almost
certainly wrong for today's firmware. Do not extend them — write a new one.

Scripts prefixed `_` are experimental and deliberately untracked.

---

## Build

| Script | Use |
|---|---|
| **`build_prod.py`** | **The fleet image.** sysbuild + MCUboot → `zephyr.signed.bin`, which is the only artefact OTA accepts |
| `build_firmware.py` | Bench/variant matrix: `--variant {med,ftd} --mesh {pi4,r1000,lab}`, one build dir per combination |
| **`archive_build.py`** | Archive the ELF per version, and `--resolve <mepc> <ra>` to turn a crash address back into a source line. **Run it on every build you flash** — see `docs/PENDIENTES.md` §2.2 |

The other `build_*.py` are per-experiment forks (40 MHz flash, aggressive
liveness, keepalive-90, audit, resprobe). History.

## Flash

| Script | Use |
|---|---|
| `flash_one.py` | One board over USB |
| `flash_fleet_prod.py` / `flash_fleet_seq.py` | Many boards, parallel / sequential |
| `bulk_flash_jtag.py`, `flash_jtag.py` | Via ESP-Prog JTAG (iSerial must be UPPERCASE — OpenOCD compares case-sensitively) |
| `recover_device.py` | A board that will not take a normal flash |

The bench recipe for a wedged USB-Serial-JTAG is in
`docs/BENCH_FINDINGS_2026-08.md` §4: **native USB triggers download mode, UART0
carries the write.** Native USB cannot sustain a bulk transfer.

## OTA

| Script | Use |
|---|---|
| `tb_edge_upload_firmware.py` | Upload the signed image to TB Edge |
| `ota_fleet.py` | Staged rollout across the fleet |
| `ota_push_direct.py` | One node, bypassing the Edge OTA engine (the engine does not push on its own — see project memory) |

## Provisioning and ThingsBoard

| Script | Use |
|---|---|
| `tb_edge_provision.py`, `tb_central_provision.py` | Create devices |
| **`tb_edge_upload_models.py`** | Upload the LwM2M object models. **Models are immutable — this deletes before creating** |
| **`tb_edge_monitoring_setup.py`** | The observe list. "Frozen telemetry in TB" is usually a path missing here, not a firmware bug |
| `onboard_node.py`, `batch_onboard.py`, `provision_node.py` | End-to-end onboarding |

## Fleet operations

| Script | Use |
|---|---|
| **`fleet_common.py`** | Shared library: mesh → Edge resolution, auth, device lookup. Import it rather than re-implementing |
| `fleet_status.py`, `fleet_active_count.py`, `verify_fleet.py` | Census and health |
| `fleet_audit.py`, `fw_audit.py` | Which firmware is actually running where |
| `deploy_fleet_staged.py` | Staged deployment |
| `net_soak.py`, `overnight_soak.py` | Mesh-level soak (eidcache, CCA, watchdog counters) |
| `topology_optimizer.py`, `dynamic_role_audit.py` | Router/child balance |

## Node diagnostics

| Script | Use |
|---|---|
| `node_doctor.py` | First stop for a misbehaving node |
| `node_monitor.py` | Periodic health, writes history/report into `captures/` |
| `diag_get.py` | CoAP `/diag` + `/ami` over IPv6 — queries a node with no console attached |
| `forensics.py`, `reboot_codes.py` | Post-mortem: reset cause, reboot tags, panic site |
| `serial_diag.py`, `shell_cmd.py` | Drive the node shell over serial |

## Bench / laboratory

The bench is a self-contained OTBR + ThingsBoard on this PC — see
`docs/LAB_OTBR_BRINGUP.md` and `docs/LAB_THINGSBOARD.md`.

| Script | Use |
|---|---|
| **`lab_restore.py`** | One command to bring the bench back: usbipd → docker → OTBR → SRP → service publish → TB |
| **`lab_ppk2_hold.py`** | Hold PPK2 output on. **Required**: the PPK2 drops DUT power whenever a new session claims it, silently power-cycling the node |
| `lab_ppk2_capture.py` | PPK2 at 100 kHz — use this for peaks and inrush |
| `lab_voltage_sweep.py` | Supply sweep; found the ~3.15 V brownout death voltage |
| `fnb_power_logger.py` | FNB-C2 continuous logging (~100 Hz). Emits rare garbage samples — never quote a raw max |
| `lab_burst_capture.py` / `lab_burst_analyze.py` | Power and console on one clock, phase-tagged |
| `ad2_brownout_capture.py` | Analog Discovery 2 via `dwf.dll` |
| `lab_soak.py`, `lab_e2e_monitor.py` | Bench soak and live dashboard |
| `lab_thread_creds.py` | Regenerate `overlays/lab.conf` from the OTBR dataset |
| `lab_tb/` | Bench ThingsBoard: compose, SRP advertise, provision, check |
| **`lab_paths.py`** | Where captures go. Import `captures_dir()` in any new tool that writes data |

## Where output goes

Everything measured lands in **`captures/`** (git-ignored, override with
`AMI_CAPTURES_DIR`). Nothing writes beside its own source any more — that is
what turned `tools/` into 1.9 GB of CSV mixed with code.
