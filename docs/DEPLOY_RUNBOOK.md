# AMI Fleet Deployment Runbook — 30 nodes (→ 60)

**Deploying now: `0.7.21-ami`** (bench-validated 2026-08-07). **Fleet is on
`0.7.17-ami`.** `0.7.14-otacfm` remains the baseline whose 30-node 2-hour soak
validated scaling to 60: delivery-liveness gate, exact TX-byte telemetry
(Obj 33000 RID 38), and **robust OTA (confirm-on-Thread-attach)** so updates
don't roll back on a congested mesh.

### What `0.7.21` fixes, and why it is worth the rollout

Every item below turns a node that dies *silently* into one that reports why.

| Release | Fix |
|---|---|
| `0.7.19` | **Boot-burst throttle no longer starves the boot watchdog.** The two together were a *permanent* reboot loop — the node resets mid-throttle, counts another unstable boot, and repeats. OTA can never reach it; only erase-all clears the NVS counter. Prime suspect for the fleet's permanently-dead nodes |
| `0.7.19` | **OT bring-up verifies its state** instead of swallowing `OT_ERROR_INVALID_STATE`, which left nodes with the radio disabled forever while logging "Thread started" |
| `0.7.18` | Reboot-cause tags, crash-PC capture (RIDs 39-41), coredump to flash |
| `0.7.20` | The fatal handler stamps the crash site **before** logging — it used to call `LOG_PANIC()` first, which hangs under deferred logging, so none of `0.7.18`'s forensics ever produced a record |

⚠️ **Confirm-on-attach cuts both ways.** Since `0.7.14` the image is confirmed
when Thread attaches, *not* after a successful REGISTER. A build that attaches
but fails to register will therefore **stick — MCUboot will not revert it**.
Deploy to a small, physically reachable group first.

---

## 0. The pinned "sweet-spot" config (do not change without re-validating)

| Knob | Value | Why |
|---|---|---|
| Firmware build | `build_prod` (ftd + resprobe_lwm2m + prod_fat) | canonical fat-production |
| Version | `0.7.21-ami` | robust-OTA baseline (`0.7.14`) + the silent-death fixes of `0.7.18`-`0.7.20` |
| Router upgrade/downgrade | `10 / 12` (main.c:3000) | validated sweet spot; less router thrash |
| MAX_CHILDREN | `32` per router | ~10 routers × 32 = ~320-node capacity |
| OTA confirm | on Thread-attach | survives slow REGISTER on congested mesh |
| delivery-liveness timeout | `1200 s` | self-heal stuck-delivery |
| LWM2M_UPDATE_PERIOD | `300 s` | feeds the liveness gate; lifetime stays 86400 |
| CoAP block size | `64 B` | avoids the USB-host overcurrent cliff |
| TX power | `≥ 0 dBm` (currently 0) | NEVER drop below 0 (−16 collapsed the mesh) |
| Board spacing | **≥ 1 unit apart** | avoids RF desense from clustered radios |
| Mesh | PAN `0xEFEB`, channel 25 | the active dataset (TLV in main.c) |

---

## 1. Prerequisites

```bash
# venv with west + esptool + paramiko (the AMI toolchain)
PY="/c/Users/jsgir/Documents/ESP32/.venv/Scripts/python.exe"
WS="/c/Users/jsgir/Documents/ESP32/zephyrproject"     # west workspace
cd "/c/Users/jsgir/Documents/UNAL/Unal-Flash-tool/firmware/ami-lwm2m-node"
```
- Pi4 OTBR up (PAN 0xEFEB, channel 25), SRP advertised. SSH root:root **@192.168.1.111**.
- TB Edge up **@192.168.1.111:8090** (tenant@thingsboard.org / tenant).
- `192.168.8.111` (R1000) is the **legacy** address — tools take `--mesh pi4` for
  the active fleet, `--mesh r1000` only for the old one.
- **No SRP advertised → nodes never find TB Edge.** After any OTBR restart:
  `ot-ctl srp server enable` and re-publish the service.
- **Out-of-tree Zephyr patch applied** (exact TX-byte hook) — see
  `tools/ZEPHYR_PATCHES.md`. Re-apply after any `west update`:
  ```bash
  cd "$WS/zephyr" && git apply <repo>/tools/zephyr_lwm2m_txbytes.patch
  ```

## 2. Build the firmware (once)

```bash
$PY tools/build_prod.py
# artifact:
BIN="$WS/build_prod/ami-lwm2m-node/zephyr/zephyr.signed.bin"   # the app (OTA)
MCU="$WS/build_prod/mcuboot/zephyr/zephyr.bin"                 # mcuboot (USB only)
```

### Pre-flight — three checks, none optional

```bash
# 1. the version you think you are shipping
strings "$BIN" | grep -m1 0.7.21-ami

# 2. the fault-injection command must NOT be in a fleet image.
#    CONFIG_AMI_TEST_FAULT compiles in `ami test panic`, which crashes the node
#    on command. It defaults to n; this check exists because the cost of being
#    wrong is a remote node anyone with shell access can kill.
strings "$BIN" | grep -c "BENCH ONLY"        # MUST print 0
grep -c "^CONFIG_AMI_TEST_FAULT=y" "$WS/build_prod/ami-lwm2m-node/zephyr/.config"   # MUST print 0

# 3. archive the ELF — RIDs 40/41 report raw addresses, and resolving them needs
#    the exact ELF of the build that was running. Skip this and a future crash
#    report is unusable noise.
$PY tools/archive_build.py --build-dir "$WS/build_prod/ami-lwm2m-node"
```

## 3. Deploy — TWO paths

### Path A — Greenfield (fresh boards, USB) → joins the mesh already on 0.7.21

```bash
# space the boards ≥1 unit apart, connect DIRECT to the PC (not clustered on a
# hub: spread/direct/good-cables ≈ 100% success, hub-clustered ≈ 29%), then:
$PY tools/flash_fleet_seq.py --coms COM19,COM20,COM21,... --build-dir build_prod
```

After flash the board boots → attaches → registers on `0.7.21-ami`. No OTA needed.

#### When a board will not take the flash

Two failures look alike and are not. Measured on the bench 2026-08-07:

| Symptom | What it actually is |
|---|---|
| `PermissionError(31)` *while connecting* | The C6 uses **native USB**. Resetting into download mode makes the USB device drop and re-enumerate, so esptool's open handle dies. Not a broken cable |
| `Write timeout` *on an open port* | With the console on UART0 **nothing reads the USB-CDC**, so the buffer fills. Expected, not a fault |
| Transfer dies after 2-4 blocks | **Native USB does not sustain bulk writes.** Deterministic — same byte count on different cables. This one is not fixable by retrying |

**The recipe that works** (two transports: USB resets, UART writes):

```bash
# 1) USB triggers the reset into download mode. May still print an error — fine.
#    Flaky: loop it, it took 5 attempts once.
$PY -m esptool --chip esp32c6 --port <USB_COM> --after no-reset flash-id

# 2) the ROM also listens on UART0 → send the bulk write over the FTDI adapter
$PY -m esptool --chip esp32c6 --port <FTDI_COM> --baud 115200 \
    --before no-reset --after no-reset \
    write-flash --erase-all --flash-mode dio --flash-freq 80m --flash-size 4MB \
    0x0 build_prod/ami-lwm2m-node/zephyr/zephyr.bin
```

~34 s, hash verified. Needs a 3-wire FTDI on UART0 — `D6→RX`, `D7→TX`, `GND↔GND`
(`overlays/console_uart0.overlay`).

**Without an FTDI**, the manual fallback: **BOOT must be held at the instant
power arrives** — not before, not after. Pressing it on an already-powered board
does nothing. Hold BOOT, plug USB, wait 2 s, release, then flash with
`--before no-reset`.

Do **not** try to enter download mode in software via
`LP_AON_FORCE_DOWNLOAD_BOOT` — it was tried and removed; the node goes silent and
needs a power cut to revive.

### Path B — Update boards already on the mesh (older fw) → STAGED OTA
**Never blast OTAs** — it congests the mesh and drops collateral nodes. Use the
staged deployer (one node at a time, settle between, skip-current, resume-safe):
```bash
# dry-run first: classify current / to-update / unreachable
$PY tools/deploy_fleet_staged.py --version 0.7.21-ami --all --dry-run

# then deploy (≈6 min/node + settle; 30 nodes ≈ 3–4 h — run + monitor):
$PY tools/deploy_fleet_staged.py --version 0.7.21-ami --bin "$BIN" --all --settle 90

# subset / one node:
$PY tools/deploy_fleet_staged.py --version 0.7.21-ami --bin "$BIN" \
    --devices ami-esp32c6-1494 --settle 90
```
Off-mesh nodes are flagged UNREACHABLE (can't OTA) → recover via Path A or power-cycle.

## 4. Validate

```bash
# per-node delivery latency + cadence + stability + exact bandwidth + mesh health:
$PY tools/net_capacity.py --mins 30
# expect: streaming N/N, RPC median <150 ms, cadence ~60 s, reboots-in-window 0

# Grafana dashboard 'AMI Comms' (msgs/min, freshness, exact bytes/min, avg pkt):
$PY tools/grafana_setup.py        # -> http://192.168.8.111:3000/d/ami-comms (admin/admin)

# TB snapshot classifier:
$PY tools/fleet_audit.py
```
**Green = success:** every flashed node streaming, RPC OK (~100 ms), 0 reboots after
settle, version `0.7.21-ami`.

## 5. Monitor — the scaling watch-items

The firmware is sized for ~320 nodes; the scaling risk is **mesh address-resolution
(eidcache)**, not capacity. Watch as the fleet grows:
```bash
# eidcache health (resolved vs retry) — printed by net_capacity, or direct:
ssh root@192.168.8.111 "ot-ctl eidcache | grep -c retry"     # retry entries
ssh root@192.168.8.111 "ot-ctl counters mac | grep -E 'TxErrCca|TxDirectMaxRetryExpiry'"
```
- `eid_retry` rising and **crowding out live resolutions** (live nodes start 504) =
  the collapse. Retry entries for *dead/off-mesh* nodes are harmless clutter.
- `TxErrCca` > 0 climbing = PHY channel saturating → reduce per-node telemetry rate
  (raise pmax in `tools/tb_edge_monitoring_setup.py`) before adding more nodes.

## 6. Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| USB `device not functioning` | SuperMini host wedge | power-cycle; if persists, download-mode (hold BOOT) |
| Node off-mesh, RPC 504 for hours | dropped, can't OTA | physical power-cycle (re-attach) |
| OTA "not confirmed within window" | poll window short, node still swapping | verify with net_capacity — usually landed (false-negative) |
| OTA reverts to old fw | (pre-0.7.14 only) confirm-after-REGISTER | already fixed by confirm-on-attach |
| eidcache fills with retry → fleet 504 | stale-addr pileup from churn | OTBR restart clears it; then reduce churn (stable fw) |
| Mesh collapses after a deploy | blasted too many OTAs | use staged deployer; raise --settle |

## 7. Scaling 30 → 60 (→ 100)

Same flow. Capacity is fine (≥320). Extra discipline:
1. **Stage harder** — `--settle 120`+, deploy in sessions of ~10–15 nodes, let the
   mesh settle between sessions.
2. **Watch eidcache + CCA** after each session (§5). If `eid_retry` trends toward
   crowding live nodes, restart the OTBR to flush, then continue.
3. **Keep boards spaced** ≥1 unit — RF desense scales with density.
4. **Don't observe dead nodes** indefinitely (TB Edge per-device inactivity) so the
   OTBR stops retrying their addresses (keeps eidcache clean).
5. Re-run `net_capacity.py` as the source of truth for delivery times + stability.
