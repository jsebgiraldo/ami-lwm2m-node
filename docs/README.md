# `docs/` — index

Start here rather than guessing from filenames. Documents are grouped by what
you are trying to do; the date is the last substantive update.

**Two documents are living and always current** — read these first if you have
been away:

| | |
|---|---|
| [`PENDIENTES.md`](PENDIENTES.md) | What is still open in the fleet and why it matters. Measured numbers, with the method to repeat them |
| [`BENCH_FINDINGS_2026-08.md`](BENCH_FINDINGS_2026-08.md) | What the bench proved: the USB cliff, the brownout death voltage, the firmware bugs that killed nodes, the infrastructure traps |

---

## Deploying

| Doc | When |
|---|---|
| [`DEPLOY_RUNBOOK.md`](DEPLOY_RUNBOOK.md) | **The runbook.** Flashing a fleet over USB, verification, rollback |
| [`ROLLOUT_0.7.17-ami.md`](ROLLOUT_0.7.17-ami.md) | Worked example of a staged rollout (2026-08-01) |
| [`OTA_ANALISIS.md`](OTA_ANALISIS.md) | How OTA actually works here — including why the TB Edge OTA engine does not push on its own |
| [`PROVISIONING.md`](PROVISIONING.md) | Factory provisioning of a new board |
| [`NODE_ONBOARDING_PLAYBOOK.md`](NODE_ONBOARDING_PLAYBOOK.md) · [`ONBOARDING_COMMANDS.md`](ONBOARDING_COMMANDS.md) | Bringing a node into an existing fleet |
| [`MESH_SWITCHING.md`](MESH_SWITCHING.md) | Pointing a build at a different OTBR (pi4 / r1000 / lab) |

## Operating and diagnosing

| Doc | When |
|---|---|
| [`GUIA_OPERACION_ESCALAMIENTO.md`](GUIA_OPERACION_ESCALAMIENTO.md) | Day-to-day operation and scaling the mesh |
| [`DIAGNOSTICO_NODOS_INACTIVOS_v0.7.15.md`](DIAGNOSTICO_NODOS_INACTIVOS_v0.7.15.md) | The inactive-node problem: root cause and diagnostic toolchain |
| [`COMANDOS.md`](COMANDOS.md) | Command cheat-sheet |
| [`DYNAMIC_DISCOVERY.md`](DYNAMIC_DISCOVERY.md) | How a board finds TB Edge (SRP / DNS-SD) — read before debugging "node never registers" |
| [`TB_EDGE_HARDWARE_CONSTRAINTS.md`](TB_EDGE_HARDWARE_CONSTRAINTS.md) | What the Pi CM4 running TB Edge can and cannot take |

## The bench (this PC — sandbox, not production)

| Doc | When |
|---|---|
| [`LAB_OTBR_BRINGUP.md`](LAB_OTBR_BRINGUP.md) | Local OTBR on Windows 11 + WSL2 with the SONOFF dongle |
| [`LAB_THINGSBOARD.md`](LAB_THINGSBOARD.md) | Bench ThingsBoard LwM2M server |
| [`LAB_LWM2M_DISCOVERY.md`](LAB_LWM2M_DISCOVERY.md) | Making the bench node discover that server |
| [`LAB_BOM.md`](LAB_BOM.md) | Bill of materials — what we own, what to buy, what each purchase unblocks |

`python tools/lab_restore.py` rebuilds the whole bench in one command.

## Design and reference

| Doc | What |
|---|---|
| [`ARQUITECTURA_main.md`](ARQUITECTURA_main.md) | Visual guide to `main.c` |
| [`STACK_SPEC_OSI_v0.7.5-prod.md`](STACK_SPEC_OSI_v0.7.5-prod.md) | The stack layer by layer, OSI-style |
| [`dlms_rs485_architecture.md`](dlms_rs485_architecture.md) | DLMS/COSEM over RS485 meter integration |
| [`architecture/`](architecture) · [`schematics/`](schematics) | Diagrams and board schematics |

## Historical

Kept for the record; superseded by the documents above.

- [`BRIEFING_2026-06-10.md`](BRIEFING_2026-06-10.md) — overnight briefing
- [`DEPLOY_V070_MINIMAL.md`](DEPLOY_V070_MINIMAL.md) — 30-node v0.7.0 runbook
- [`E2E_PROCEDURE.md`](E2E_PROCEDURE.md) — early end-to-end procedure

---

Deeper engineering history — root causes, dead ends, what each fix cost — lives
in the agent's project memory rather than here, so these documents stay
operational.
