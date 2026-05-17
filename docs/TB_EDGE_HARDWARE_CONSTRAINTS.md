# TB Edge Hardware Constraints — R1000 (Raspberry Pi CM4)

Findings from the 7-node 30-day-stability work, recorded as design criteria
for future hardware/sizing decisions. Captured live from the production
gateway (`192.168.8.175`), not extrapolated.

## Platform under test

| Item | Value |
|---|---|
| Host | Seeed R1000 with **Raspberry Pi Compute Module 4 Rev 1.1** |
| Architecture | aarch64 (4-core Cortex-A72) |
| RAM | **1848 MB total** — no swap, no zram |
| Disk | **7.1 GB rootfs** (OpenWrt overlay) — 90 % used at install |
| OS | OpenWrt + Docker for the TB Edge stack |
| TB Edge | `thingsboard/tb-edge:4.3.1.1EDGE` + `postgres:15-alpine` |

## Steady-state resource budget (after tuning, 7 LwM2M nodes, observed)

| Component | RSS / footprint | % of 1.8 GB |
|---|---|---|
| OS + OpenWrt + Docker engine | ~150 MB | 8 % |
| Postgres (after tuning) | ~50 MB shared\_buffers + 30 MB RSS | 4 % |
| tb-edge JVM (`-Xmx768m`, G1GC) | ~750-900 MB at peak | 41-49 % |
| Filesystem buff/cache | ~700 MB (reclaimable) | — |
| **Available headroom** | **~700 MB** | 38 % |
| Disk: TB Edge image | 1.16 GB | 16 % of rootfs |
| Disk: TB Edge postgres data (160 MB now, growing) | depends on load | — |

CPU load: 0.78-0.95 (4-core, ~24 % per core). CPU is **not** the bottleneck.

## Hard constraints (cannot be tuned away)

These bound the maximum fleet size and stability window on this platform.

1. **1.8 GB RAM, no swap.** When pressure spikes, the kernel OOM-kills
   processes rather than spilling to disk. The JVM heap + postgres
   shared\_buffers + OS is already ~60 % of RAM at idle. Available for
   spike absorption: ~700 MB.

2. **7.1 GB rootfs at 90 % used.** Docker images alone consume 1.43 GB.
   Postgres telemetry tables will grow with fleet size × keys × notify
   rate. At 7 nodes × 39 keys × current cadence the DB grew to 160 MB in
   the first day — extrapolating linearly to 30 nodes × 30 days that is
   ~2 GB. **There is not enough rootfs space.** Either telemetry TTL
   must be configured, or postgres must be mounted on external storage.

3. **No process isolation by default.** Containers run with no memory
   limit set. A JVM heap leak or postgres runaway will starve everything
   else, including the OS. Limits **must** be configured per service.

4. **Single Thread router (the OTBR).** OpenThread caps children at
   **32 per router**. At 30 nodes you are at 94 % of the cap — one
   partition flap and the next attach is refused. Mitigation is firmware
   side, not Edge: build 2-3 nodes with `--variant ftd` to become
   router-eligible, distributing children across parents.

## Tunable items — what was already applied here

These are now live on the production R1000 (`postgresql.auto.conf`):

| Setting | Default | Tuned to | Saves | Reasoning |
|---|---|---|---|---|
| `postgres.shared_buffers` | 128 MB | **48 MB** | 80 MB | Edge writes are mostly latest-value telemetry; 48 MB caches the hot rows for a 160 MB DB |
| `postgres.work_mem` | 4 MB | **2 MB** | a few MB / query | Few concurrent connections, simple plans |
| `postgres.maintenance_work_mem` | 64 MB | **32 MB** | 32 MB at peak | No big VACUUM/CREATE INDEX work in this workload |
| `postgres.effective_cache_size` | **4 GB** | **512 MB** | (planner hint only) | The 4 GB default is a lie on a 1.8 GB box and skews plans |
| `postgres.max_connections` | 100 | **50** | small | TB Edge pool needs ~20; the headroom is just lock-table memory |

**Net saving:** ~85 MB of RAM moved from postgres reservations to JVM
buff/cache headroom. Verified before/after via `free -m`.

## Tunable items — recommended for Phase 1 (15-node scale)

Not applied yet — each requires recreating a container (~3 min
disruption) so defer until the next planned maintenance window.

1. **JVM heap bump**: `-Xmx768m → -Xmx1024m`. The 85 MB freed by the
   postgres tuning above makes this safe. Edit `JAVA_OPTS` in
   `/opt/docker/tb-edge/docker-compose.yml`.

2. **Container memory limits**, in `docker-compose.yml`:
   ```yaml
   tb-edge:
     mem_limit: 1280m       # JVM 1024m + ~256m overhead
   postgres:
     mem_limit: 200m        # shared_buffers 48m + pool + RSS
   ```
   Without these, a leak in either container OOM-kills the OS.

   **CAVEAT on this OpenWrt build:** the stock kernel does NOT have the
   `memory` cgroup controller enabled. Docker prints
   `Your kernel does not support memory limit capabilities or the
   cgroup is not mounted. Limitation discarded.` and starts the
   container with no enforcement. To make these limits actually bind,
   the kernel needs `cgroup_enable=memory swapaccount=1` on the
   bootargs (typically via `/boot/cmdline.txt` on the CM4) and
   `CONFIG_MEMCG=y`. Until then, JVM `-Xmx` is the only enforced cap
   and a postgres leak is unbounded — keep zram swap (item 3) as the
   only spillover.

3. **zram swap** (256-512 MB compressed). Provides graceful spillover on
   brief memory spikes without disk wear. OpenWrt has `kmod-zram` —
   `modprobe zram; echo 256M > /sys/block/zram0/disksize; mkswap;
   swapon`.

4. **TB Edge `Xms`** stays at 256 MB so a fresh start doesn't pre-commit
   the heap; G1GC grows it on demand.

## Tunable items — Phase 2 (>15 nodes) if heap pressure appears

These trade telemetry quality for capacity.

- **Reduce the device profile's observe set.** Currently 11 resources
  per device. Many of them (`mac_tx_*`, `mac_rx_*`) are useful but not
  critical for the 30-day stability target. Trimming to 5 (active,
  voltage, current, frequency, temperature) cuts LwM2M context memory
  and observe-notify CPU by roughly half.

- **Raise `pmin` on meter resources from 60 s to 120 s.** Halves notify
  rate, lowers TB-Edge rule-engine throughput by ~2×.

- **Move postgres data to external storage** (USB SSD on the R1000).
  Eliminates the rootfs disk cap and lets telemetry TTL be set
  aggressively without losing important history.

## Hardware ceiling — what would need a bigger box

For >30 nodes or for 90-day retention with full telemetry granularity,
the CM4 is the wrong host. Indicators:

- DB > 4 GB → rootfs exhaustion even after pruning
- JVM heap consistently > 80 % after `Xmx1024m`
- Postgres RSS > 200 MB
- More than ~50 concurrent LwM2M observers

The natural step up is a CM4 with **4 GB RAM + a USB SSD for /opt**, or
a small x86 box (e.g. Intel N100, 8 GB RAM, 64 GB SSD) running the same
docker-compose stack unchanged.

## Operational scars worth recording

These are not hardware limits — they're **operational fragility** observed
in this deployment that the firmware must tolerate:

1. **TB Edge re-installs wipe the DB** (despite bind mounts, the user's
   parallel migration sometimes resets postgres). Each wipe loses the
   device profile, devices, LwM2M credentials, alarms, dashboard, rule
   chain edits, and uploaded object models. Full bring-up is automated
   via:
   ```
   tools/tb_edge_upload_models.py
   tools/tb_edge_provision.py
   tools/tb_edge_monitoring_setup.py
   ssh root@.../tb-edge docker restart tb-edge-v2
   ```
   ~3 min + container start time.

2. **TB Edge caches the device profile in memory** at LwM2M transport
   layer. Updating the profile via the REST API does NOT invalidate the
   transport cache — `docker restart tb-edge-v2` is required for the
   change to take effect on already-registered clients.

3. **`defaultObjectIDVer`** in `clientLwM2mSettings` is a SINGLE version
   string (`"1.0"`) — NOT a JSON-encoded object/map. The TB UI export
   stringifies the per-object map; that exported form is rejected by
   the create API.

4. **Custom LwM2M objects need their XML model uploaded** to TB Edge's
   resource library via `POST /api/resource` (type `LWM2M_MODEL`).
   Without the model, registration succeeds but every notification is
   silently dropped with `Tenant hasn't such the resource`. The build
   in `tools/tb_edge_upload_models.py` generates 10242 / 33000 / 3303
   XMLs from the firmware source.

5. **The OpenThread border router's SRP server** loses its service
   registrations whenever the Thread partition re-forms. The
   `otbr-srp` service must be restarted after such events to republish
   `ThingsBoard-Edge._lwm2m._udp`. Without the SRP entry, nodes never
   resolve the LwM2M server via DNS-SD and reboot-loop.

These items survived from observation across two days of work and should
be considered when designing the **next** generation of the gateway.
