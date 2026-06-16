# Dynamic Discovery — How Boards Find TB Edge

**Rule**: nothing in the address path is hardcoded. Every IP, prefix, and
route the system uses is derived at runtime from a dynamic source. If a
value here ever has to be edited by hand, treat that as a regression and
fix the dynamic mechanism upstream.

## Chain at a glance

```
ESP32-C6 board                                              Pi4 OTBR
─────────────                                              ─────────
1. boot                                                    otbr-agent
                                                           starts with
2. join Thread mesh as Child  ────── (radio packets) ───→  -B br-lan
   role-change callback fires                              (UCI config)
                                                                │
3. lwm2m_discover_resolve()           OpenThread DNS-SD         ▼
   srvName: ThingsBoard-Edge        ───────────────→     ot::Srp::Client
   type:    _lwm2m._udp.default                          queries SRP server
   .service.arpa.                                        (anycast .fc00)
                                                                │
                                                                ▼
4. resolved →                                            SRP advertises:
   coap://[fd32:e7c9:e9af:adf2:                          host: thingsboard-edge
          ded8:87e6:e29c:3282]:5683                      addrs:[fd32:…:3282]
                                                         port: 5683
5. lwm2m_rd_client_start()                                      │
   REGISTER POST /rd                                            │
            ─── coap://[fd32:…:3282]:5683 ────────────────────→ │
                                                                ▼
                                                         TB Edge transport
                                                         (pi4-edge-v2 docker
6.                                                       container, eth0 on
   ← ack 2.01 Created + Location:                        host net = bound to
     /rd/<regId>  ──────── src=fd32:…:3282 ───────       wpan0)
        (Linux RFC 6724 §5 picks
         non-deprecated mesh-local
         as source because BR is
         running and addr-guard
         keeps preferred_lft=forever)
```

## Components by layer

### Pi4 — system services (OpenWrt, persistent)

| Service | Role | Persistence |
|---|---|---|
| `otbr-agent` | Spawns OpenThread daemon, reads UCI for `infra_if_name` and `thread_if_name`, passes `-B <infra>` so Border Router knows the upstream interface | UCI `/etc/config/otbr-agent` + `/etc/init.d/otbr-agent` (enabled) |
| `otbr-addr-guard` | procd respawn service that runs `otbr-addr-lifetime-guard` to keep wpan0 mesh-local addresses with `preferred_lft=forever` so RFC 6724 §5 picks them as outbound source | `/etc/init.d/otbr-addr-guard` (enabled, START=96) |
| `otbr-srp` | Configures SRP server on OTBR so services in the mesh can register and be discovered (TB Edge registers itself here) | UCI `/etc/config/otbr-srp` |
| Border Router (`br`) | Inside otbr-agent. Advertises OMR prefix to mesh, manages routing between Thread mesh and br-lan. Must have `infraif` set to a real upstream interface (br-lan) — NOT lo, NOT eth0 | Controlled by `otbr-agent -B br-lan` startup flag, derived from UCI `infra_if_name` |

**Critical UCI setting** (verified via `uci show otbr-agent`):
```
otbr-agent.service.infra_if_name='br-lan'
otbr-agent.service.thread_if_name='wpan0'
```

If `infra_if_name` is wrong (e.g. `eth0` standalone, `lo`), BR fails to
init with `Error 13: InvalidState`. Symptoms: wpan0 addresses go
`preferred_lft=0sec`, Edge replies source from the wrong interface,
boards see src/dst CoAP mismatch and discard.

### TB Edge (docker container, `pi4-edge-v2`)

| Mechanism | Role |
|---|---|
| Network mode `host` | Container shares Pi4's network namespace → has direct access to wpan0 / br-lan |
| SRP self-registration | Edge registers `ThingsBoard-Edge._lwm2m._udp.default.service.arpa.` with the OTBR SRP server. Address comes from the wpan0 mesh-local address (currently `fd32:…:3282`) |
| Bound port | UDP 5683 on the host = reachable from boards via the SRP-advertised address |

### Board firmware (Zephyr 4.3 + OpenThread)

| Mechanism | File | Role |
|---|---|---|
| OpenThread DNS-SD client | `src/lwm2m_discover.c` | Resolves `ThingsBoard-Edge._lwm2m._udp.default.service.arpa.` via SRP → gets `(addr, port)` |
| Discovery retry/backoff | `src/main.c::lwm2m_discover_with_retry()` | Up to 10 attempts with exponential backoff. Falls through to recover_work if all fail |
| LwM2M RD client | Zephyr stdlib | Sends POST /rd to discovered URI, registers, then maintains lifetime |
| Watchdog | `src/lwm2m_watchdog.c` | Forces COLD reboot if no first REGISTER ACK within deadline (with anti-storm backoff if persistent) |

### What is **NOT** hardcoded anymore

- ✗ TB Edge IP address — discovered via SRP, never typed in
- ✗ Mesh-local prefix — comes from Thread dataset (boards inherit when attaching)
- ✗ OMR prefix — published by BR (`fda0:13c7:aa71:1::/64`)
- ✗ Edge port — comes from SRP service record (5683 is just the convention)
- ✗ wpan0 `preferred_lft` — managed by otbr-addr-guard
- ✗ Source IP for outbound CoAP — picked by Linux RFC 6724 §5 once BR keeps addresses non-deprecated

### What is **still** hardcoded (and why) — audit candidates

- `CONFIG_AMI_LWM2M_SERVER_IPV6_PRIMARY` in `prj.conf:325` — used as a
  **fallback** if DNS-SD discovery fails 10 attempts. Currently set to
  an old/stale IP from a previous setup. **TODO**: either remove
  entirely (force board to keep retrying SRP) or set to a documented
  test/lab address; in production we should never reach it.
- SRP service name `ThingsBoard-Edge._lwm2m._udp.default.service.arpa.`
  in `src/lwm2m_discover.c:24-26` — this is a contract between board and
  Edge; both sides need the same name. Treat as a versioned protocol
  constant, not a config knob.

## How to verify the chain is healthy

Run these in order. If any step fails, fix it before moving on.

### 1. Pi4 — OTBR Border Router is `running`

```
$ ot-ctl br state
running
$ ot-ctl br infraif
if-index:4, is-running:yes    ← if-index must be br-lan (4 on this box)
$ ot-ctl br omrprefix
Local: fda0:13c7:aa71:1::/64  ← BR must own an OMR prefix
```

If `stopped` or `InvalidState`:
- Check `uci get otbr-agent.service.infra_if_name` → must be `br-lan`
- Restart `/etc/init.d/otbr-agent restart` then re-check

### 2. Pi4 — wpan0 mesh-local addresses are non-deprecated

```
$ ip -6 addr show dev wpan0 | grep -c 'preferred_lft forever'
6                              ← should be ≥ 1, ideally all global addrs
$ ip -6 addr show dev wpan0 | grep -c 'preferred_lft 0sec'
1                              ← link-local (fe80::) is fine to be 0sec
```

If global addresses are `0sec`:
- Check `/etc/init.d/otbr-addr-guard status` → must be running
- Check BR state (step 1) → must be `running`, the BR is what keeps
  the kernel from deprecating these

### 3. Pi4 — SRP server has TB Edge registered

```
$ ot-ctl srp server service | grep -A 3 _lwm2m
ThingsBoard-Edge._lwm2m._udp.default.service.arpa.
    deleted: false
    port: 5683
    addresses: [fd32:e7c9:e9af:adf2:ded8:87e6:e29c:3282]
```

If missing:
- `pi4-edge-v2` container may have crashed → `docker ps` and `docker logs pi4-edge-v2`
- SRP server itself may be off → `ot-ctl srp server state`

### 4. Pi4 — Edge replies from the right source

```
$ tcpdump -i wpan0 -nn -c 10 'udp port 5683'
```

Outbound packets (port 5683 in source) must have source =
`fd32:e7c9:e9af:adf2:ded8:87e6:e29c:3282` (the SRP-registered Edge
address). If they're coming from `fdd8:…` (br-lan OMR) or `…:fc11`
(RLOC anycast), BR is misconfigured or `addr-guard` isn't running.

### 5. Board — DNS-SD resolves and REGISTER ACKs

UART (via COM port, `dtr=False rts=False` so we don't reset on open):
```
<inf> ami_lwm2m: DNS-SD lookup attempt 1/10 (timeout=7000ms)...
<inf> lwm2m_discover: DNS-SD service resolved:
       coap://[fd32:e7c9:e9af:adf2:ded8:87e6:e29c:3282]:5683
<inf> net_lwm2m_rd_client: RD Client started …
```

Then look at `Obj33000(diag): up=XXs reg=N/M`:
- `reg=1/1` or higher within 30 s = healthy
- `reg=0/M` with M growing = REGISTER not ACK'd; one of steps 1-4 above is broken

Live JTAG counter read (no UART needed):
```
mdw 0x40845c80 1   # lwm2m_diag_reg_success — must be ≥ 1
mdw 0x40845c84 1   # lwm2m_diag_reg_attempts
mdw 0x40845c88 1   # last_emit_uptime — must be > 0 after first ACK
```

## Server-initiated dynamic management (TB Edge RPC API)

Once a board is registered, TB Edge can issue LwM2M operations via HTTP RPC.
This is the management plane: read live state, write thresholds, execute
role transitions — all without USB reflash.

### Method mapping (TB Edge LwM2M transport)

The TB Edge LwM2M RPC uses non-obvious method names. The ones that work:

| LwM2M operation | TB Edge `method` field |
|---|---|
| Read | `Read` |
| Write (replace value of a resource) | `WriteReplace` (NOT `Write` — returns `METHOD_NOT_ALLOWED`) |
| Write (multi-instance update) | `WriteUpdate` |
| WriteAttributes (pmin/pmax) | `WriteAttributes` (currently returns `INTERNAL_SERVER_ERROR` — open) |
| Execute | `Execute` |
| Discover | `Discover` |

Send via:
```
POST http://192.168.8.111:8090/api/rpc/twoway/{deviceId}
Body: {
  "method": "Read|WriteReplace|Execute|...",
  "params": { "id": "/<obj>/<inst>/<res>", "value": <int/str/bool> },
  "timeout": 5000
}
```

### Critical lifetime gotcha — `CONFIG_LWM2M_ENGINE_DEFAULT_LIFETIME`

TB Edge tracks "active" by REGISTER lifetime — and the LwM2M transport in TB Edge has
a known bug where **REG_UPDATE does NOT refresh `lastActivityTime`**; only the initial
REGISTER does. The behaviour:

- Client registers at T=0, lifetime=L → TB Edge sets `lastActivityTime` = T=0
- Client sends REG_UPDATE at T=0.8L, T=1.6L, … → TB Edge **does not advance** the timestamp
- At T=L, TB Edge stamps `lastDisconnectTime = lastActivityTime` and the device shows
  `active=False` to the RPC layer. Any subsequent `POST /api/rpc/twoway/{deviceId}`
  returns `HTTP 504` (gateway timeout — no live route).

With `CONFIG_LWM2M_ENGINE_DEFAULT_LIFETIME=300` (the Zephyr default we used through
v0.6.65) this fired every 5 minutes: telemetry kept flowing (CoAP socket was alive),
but server-initiated RPC was blocked except in narrow windows right after REGISTER.

**v0.6.66 fix:** raise lifetime to 86400 s (24 h) and bump `SECONDS_TO_UPDATE_EARLY`
to 17 280 s (20 % of lifetime, ~4.8 h cushion). TB Edge treats the device as active
for a full day at a time, so RPC works continuously. The tradeoff is that dead-device
detection on the server side lags by up to 24 h — we mitigate that by watching the
`lwm2m_notify_emitted` counter from conn-monitor as a per-minute liveness proxy.

### Object 33001 — dynamic Thread role / threshold control

`src/lwm2m_obj_thread_role.c` exposes:

| Resource | Op | Type | Default | Meaning |
|---|---|---|---|---|
| `/33001/0/0` | E | — | — | become_router |
| `/33001/0/1` | E | — | — | become_child (also sets eligible=false to make sticky) |
| `/33001/0/2` | RW | U8 | 10 | `router_upgrade_threshold` (REED promotes to Router if it sees < N routers) |
| `/33001/0/3` | RW | U8 | 12 | `router_downgrade_threshold` (Router demotes if it sees > N routers) |
| `/33001/0/4` | R | STRING | — | `current_role` (Detached/Child/Router/Leader; refreshed by periodic poll) |
| `/33001/0/5` | R | BOOL | — | `is_router_eligible` (false on MTD builds) |

Examples that work in production today (after v0.6.66):
```
Read  /33001/0/2  → returns 10  (the boot default)
WriteReplace /33001/0/2 = 15  → next promotion decision uses 15
Execute /33001/0/0  → device immediately becomes Router
Execute /33001/0/1  → device demotes to Child and stops auto-promoting
```

Use case: `tools/topology_optimizer.py` (referenced in main.c) is the server-side
balancing tool that walks the mesh, picks under-loaded routers via these RPCs and
demotes them so the global router count converges to a sane number even when the
upgrade-threshold algorithm leaves a residual imbalance.

## Incident history

| Date | Symptom | Root cause | Fix |
|---|---|---|---|
| 2026-06-04 | Fleet-wide `reg_success=0`, boards in noreg storm despite Pi4 SRP advertising correct address. tcpdump showed Edge sourcing replies from `fdd8:…:de87` (br-lan) instead of mesh-local `fd32:…:3282` | OTBR `br` was `stopped` because UCI `infra_if_name='eth0'` and `br init` had been called with if-index 2 (lo). Without BR running, Linux marked wpan0 mesh-local addresses as `preferred_lft=0sec`, RFC 6724 §5 skipped them as outbound source, kernel picked br-lan address → CoAP src/dst mismatch on boards, replies silently discarded | `uci set otbr-agent.service.infra_if_name='br-lan'; uci commit otbr-agent` + runtime `ot-ctl br init 4 1; ot-ctl br enable`. Verified `running`, addresses went back to `forever`, board REGISTERs ACK'd within seconds |
