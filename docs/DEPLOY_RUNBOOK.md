# AMI Fleet Deployment Runbook — 30 nodes (→ 60)

**Sweet-spot firmware: `0.7.14-otacfm`** (commit baseline `18adbb1`). This is the
validated production build: delivery-liveness gate, exact TX-byte telemetry
(Obj 33000 RID 38), and — critically — **robust OTA (confirm-on-Thread-attach)**
so updates don't roll back on a congested mesh.

---

## 0. The pinned "sweet-spot" config (do not change without re-validating)

| Knob | Value | Why |
|---|---|---|
| Firmware build | `build_prod` (ftd + resprobe_lwm2m + prod_fat) | canonical fat-production |
| Version | `0.7.14-otacfm` | robust-OTA baseline |
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
- Pi4 OTBR up (PAN 0xEFEB, channel 25), SRP advertised. SSH root:root @192.168.8.111.
- TB Edge up @192.168.8.111:8090 (tenant@thingsboard.org / tenant).
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
# sanity:
strings "$BIN" | grep -m1 0.7.14-otacfm
```

## 3. Deploy — TWO paths

### Path A — Greenfield (fresh boards, USB) → joins mesh already on 0.7.14
USB is unreliable on the SuperMini (host wedge). Single-shot, anti-wedge:
```bash
# space the boards ≥1 unit apart, connect to the hub, list their COM ports, then:
$PY tools/flash_fleet_seq.py --coms COM19,COM20,COM21,... --build-dir build_prod
```
- **Wedge** (`device not functioning` / 0 verified): power-cycle the board (unplug
  USB) and re-run; if it re-wedges, **download-mode** = hold BOOT while replugging,
  then re-run. RTS reset alone does NOT boot these.
- After flash the board boots → attaches → registers on `0.7.14-otacfm`. No OTA needed.

### Path B — Update boards already on the mesh (older fw) → STAGED OTA
**Never blast OTAs** — it congests the mesh and drops collateral nodes. Use the
staged deployer (one node at a time, settle between, skip-current, resume-safe):
```bash
# dry-run first: classify current / to-update / unreachable
$PY tools/deploy_fleet_staged.py --version 0.7.14-otacfm --all --dry-run

# then deploy (≈6 min/node + settle; 30 nodes ≈ 3–4 h — run + monitor):
$PY tools/deploy_fleet_staged.py --version 0.7.14-otacfm --bin "$BIN" --all --settle 90

# subset / one node:
$PY tools/deploy_fleet_staged.py --version 0.7.14-otacfm --bin "$BIN" \
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
settle, version `0.7.14-otacfm`.

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
