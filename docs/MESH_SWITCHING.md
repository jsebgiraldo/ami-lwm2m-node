# Switching mesh target — Pi 4 vs R1000

> **TL;DR:** the firmware compiles against one of two Thread datasets,
> selected via Kconfig overlay. Default is **`r1000`** (production
> UNAL-R1000). Pass `--mesh pi4` for the legacy UNAL-Thread on the Pi 4
> EKH01 (kept for migration of existing nodes).

---

## Why this exists

Originally the firmware had a single hardcoded dataset TLV in `src/main.c`
(commit before 2026-04-28) targeting the Pi 4 EKH01 OTBR (`192.168.1.111`,
channel 25, network `UNAL-Thread`).

On 2026-04-28 we provisioned a second OTBR on the Seeed R1000 with a clean
channel and lessons learned applied — see [`docs/architecture/edge-r1000.md`](https://github.com/.../edge-r1000.md)
in the OpenWrt repo for the full spec. Each OTBR has:

- Different channel (Pi 4 = 25, R1000 = 21)
- Different PAN ID, ExtPAN, NetworkKey, PSKc, mesh-local prefix
- Different LwM2M server fallback IPv6

The firmware can't speak to both at once — it commits to one mesh at boot
via `otDatasetSetActiveTlvs()`. We pick the mesh at **build time**.

---

## How it works

3 pieces working together:

1. **`Kconfig`** declares a `choice AMI_MESH` with 2 mutually-exclusive options:
   - `CONFIG_AMI_MESH_R1000=y` (production default)
   - `CONFIG_AMI_MESH_PI4=y` (legacy)

2. **`src/main.c`** — `apply_otbr_dataset()` uses `#ifdef CONFIG_AMI_MESH_*`
   to compile in the corresponding TLV blob. One blob per mesh target.

3. **`overlays/<mesh>.conf`** — sets `CONFIG_AMI_MESH_*=y` plus the cosmetic
   Kconfig values (`CONFIG_OPENTHREAD_CHANNEL`, network name, LwM2M lifetime,
   fallback server IPv6) so the boot banner reports the right values and the
   LwM2M client uses the per-mesh tuning (e.g. r1000 has `lifetime=120s`).

The build system (`tools/build_firmware.py`) composes the variant overlay
(`med` / `ftd`) with the mesh overlay (`pi4` / `r1000`) into a single
`-DEXTRA_CONF_FILE=...;...` argument.

---

## Build cookbook

Each (variant, mesh) combination produces its own artifact at
`build_<variant>_<mesh>/zephyr/zephyr.bin`. No special case for the default
mesh — keeps the layout explicit and avoids silent binary swaps.

### Build for the production R1000 mesh (default)

```powershell
python tools\build_firmware.py
# equivalent to: --variant med --mesh r1000
# output:        build_med_r1000/zephyr/zephyr.bin
```

```powershell
python tools\build_firmware.py --variant ftd
# output: build_ftd_r1000/zephyr/zephyr.bin
```

### Build for the legacy Pi 4 mesh

```powershell
python tools\build_firmware.py --mesh pi4
# output: build_med_pi4/zephyr/zephyr.bin
```

```powershell
python tools\build_firmware.py --variant ftd --mesh pi4
# output: build_ftd_pi4/zephyr/zephyr.bin
```

### Build everything (4 combinations)

```powershell
python tools\build_firmware.py --variant all --mesh all --pristine
# outputs:
#   build_med_r1000/zephyr/zephyr.bin
#   build_ftd_r1000/zephyr/zephyr.bin
#   build_med_pi4/zephyr/zephyr.bin
#   build_ftd_pi4/zephyr/zephyr.bin
```

---

## Flashing — picking the right binary

`onboard_node.py`, `batch_onboard.py` and `fleet_status.py` all use
`--mesh r1000` by default. Pass `--mesh pi4` to target the legacy mesh.

```powershell
# Default flow (r1000):
python tools\onboard_node.py

# Legacy fleet:
python tools\onboard_node.py --mesh pi4
```

---

## How the firmware boots and which mesh it joins

1. ESP32-C6 powers on, runs Zephyr `main()`.
2. `apply_otbr_dataset()` is called early (before LwM2M init):
   - `otInstanceErasePersistentInfo()` wipes any NVS-cached dataset (lesson
     learned: NVS persistence beat Kconfig changes; we now force-wipe).
   - The compiled-in TLV blob (PI4 or R1000 depending on `CONFIG_AMI_MESH_*`)
     is applied via `otDatasetSetActiveTlvs()`.
   - IPv6 + Thread are enabled.
3. The startup banner prints which mesh was joined:
   ```
   [00:00:01.234] <inf> ami_lwm2m: OTBR dataset applied: UNAL-R1000 (Seeed R1000, Ch21)
   [00:00:01.235] <inf> ami_lwm2m: Mesh-local: fdf1:a391:6243:2a67::/64
   ```
4. Once the node is `Child` / `Router`, LwM2M client tries to discover the
   server via DNS-SD (`thingsboard-edge.default.service.arpa.`) against the
   OTBR's SRP server. If discovery fails, it falls back to
   `CONFIG_AMI_LWM2M_SERVER_IPV6_PRIMARY` (set per-mesh in the overlay).

---

## Reverting a node from one mesh to the other

You **must reflash** — there's no runtime switch. The dataset is committed
to NVS by `otDatasetSetActiveTlvs` and persists across reboots. The next
firmware boot calls `otInstanceErasePersistentInfo()` which wipes NVS and
applies whatever TLV is compiled in.

So:

```powershell
# Move a node from Pi 4 mesh to R1000 mesh
python tools\build_firmware.py --variant med --mesh r1000
west flash --build-dir build_med_r1000 --esp-device COM7
# Node boots, erases NVS, applies R1000 dataset, joins UNAL-R1000.
```

---

## Choosing your mesh

| Decision | Choose |
|---|---|
| **New deployment / production** | **`r1000`** (default) — clean channel, backbone selectivo |
| Existing nodes still paired to Pi 4 EKH01 | `pi4` — until migrated |
| Capacity testing per ADR `lwm2m-update-rate-and-mesh-capacity` | `r1000` — clean baseline |
| Migration of legacy fleet | reflash `pi4` units to `r1000` gradually |

---

## Provisioning to TB Edge (per-mesh)

After flashing, register the node in the **right TB Edge** UI:

| Mesh | TB Edge URL | Port | Edge name in TB Central |
|---|---|---|---|
| `pi4` | `http://192.168.1.111:8090` | 8090 | `Edge Gateway OpenWrt` |
| `r1000` | `http://192.168.1.175:8090` | 8090 | `edge-r1000-wm6108` |

Use `tools/provision_node.py --host <EDGE_IP> --port 8090 --endpoint ami-esp32c6-XXXX`
adjusting `--host` per mesh.

---

## See also

- `Kconfig` — `choice AMI_MESH` definition
- `src/main.c` — `apply_otbr_dataset()` with `#ifdef`s
- `overlays/pi4.conf` and `overlays/r1000.conf`
- `tools/fleet_common.py` — `MESH_TARGETS`, `mesh_overlay_path()`,
  `composed_overlay_arg()`
- `tools/build_firmware.py` — `--mesh` flag
- OpenWrt repo specs for each OTBR side:
  - `docs/architecture/edge-thingsboard.md` (Pi 4)
  - `docs/architecture/edge-r1000.md` (R1000)
- ADRs (in OpenWrt repo `docs/decisions/`):
  - `thread-mesh-role-assignment.md` — why MED-by-default + REED backbone
  - `lwm2m-update-rate-and-mesh-capacity.md` — why lifetime ≥ 60s
  - `edge-zero-touch-provisioning.md` — TB Central edge creation flow
