# LAB_THINGSBOARD — bench ThingsBoard LwM2M server bring-up

**Goal:** stand up a real ThingsBoard CE LwM2M server on this PC so the lab node
(`ami-esp32c6-3bb0`) actually **REGISTERs**, making the bench representative of
production instead of a serverless environment that manufactures failures.

Until this runbook is executed the bench is a lie: `overlays/lab.conf:32-33`
disables `CONFIG_AMI_BOOT_REGISTER_DEADLINE_S` and stretches
`CONFIG_AMI_HW_WATCHDOG_BOOT_GRACE_HARD_S` to 3600 s **specifically because there
is no server**. Every registration / observe / RPC / OTA path is therefore
untested, and the two escapes are values we would never ship. Step 8 removes
them — that step, not step 1, is what actually closes the gap.

Related: [`docs/LAB_OTBR_BRINGUP.md`](LAB_OTBR_BRINGUP.md) (the Thread side),
[`docs/LAB_LWM2M_DISCOVERY.md`](LAB_LWM2M_DISCOVERY.md) (the DNS-SD deep dive),
[`docs/DYNAMIC_DISCOVERY.md`](DYNAMIC_DISCOVERY.md) (the production design),
[`tools/lab_tb/README.md`](../tools/lab_tb/README.md) (per-tool reference).

---

## 0. The one contract that must hold everywhere

Three independent components have to agree on the same numbers. Every historical
failure on this bench has been one of them silently disagreeing.

| Thing | Value | Enforced in | Because |
|---|---|---|---|
| LwM2M UDP port | **5683** | `tools/lab_tb/docker-compose.yml` (`LWM2M_BIND_PORT`), the SRP **SRV record**, `src/lwm2m_discover.c:22` (`LWM2M_DEFAULT_PORT`) | Strategy 1 takes the port from the SRV record; Strategy 2 (host-AAAA fallback) **hardcodes 5683**. Publishing anything else makes Strategy 2 dial a dead port and turns a recoverable failure into a permanent one. |
| LwM2M bind address | **`::`** (IPv6 wildcard) + `network_mode: host` | `docker-compose.yml` | The socket must live in WSL's own netns, the one that owns `wpan0`. A `0.0.0.0` bind is invisible to the mesh; a *bridged* container fails asymmetrically — REGISTER may arrive while every inbound Observe/RPC times out. |
| Advertised address (AAAA) | the **OMR** address on `wpan0`, e.g. `fdaf:e549:1751:1:1199:8c2b:a32e:38ee` | `lab_tb_srp.py` / `srp_publish_lwm2m.sh` **refuse** to publish a mesh-local address | `src/lwm2m_discover.c:113-144` prefers off-mesh-local and warns `only mesh-local address available` — a mesh-local AAAA breaks src/dst symmetry on the BR (the 2026-06-04 outage). |
| Service instance | `ThingsBoard-Edge._lwm2m._udp.default.service.arpa.` | `src/lwm2m_discover.c:24-25` | Protocol contract, not config. Do not "tidy" it. |
| Host FQDN | `thingsboard-edge.default.service.arpa.` | `src/lwm2m_discover.c:26` | Same. |
| CoAP transport | **off**, moved to 5690 | `COAP_ENABLED=false` **and** `COAP_SERVER_ENABLED=false` **and** `COAP_BIND_PORT=5690` | TB's CoAP transport binds 5683 *first*; LwM2M then dies with `java.net.BindException`. `COAP_ENABLED=false` alone is **not** enough (`.context/ESTADO.md` Bug 1 — already burned once here). |
| HTTP / REST | **8080** | `docker-compose.yml`, `fleet_common.MESH_TO_EDGE["lab"]` | Production uses 8090 only because `dppd` held 8080 there; 8080 is free here. |
| Endpoint name | `ami-esp32c6-3bb0` | `src/main.c:3100-3113`, mirrored by `fleet_common.mac_to_endpoint()` | `ami-esp32c6-<last 2 bytes of link addr, lower hex>` from MAC `98:a3:16:61:3b:b0`. Device name == label == `credentialsId` == LwM2M endpoint. |
| Device profile | `AMI_LwM2M_Node` | `fleet_common.py:45` | `docs/PROVISIONING.md:196` still names the retired `C2000_Monofasico_v2` — **that doc is stale, follow the code.** |
| Tenant login | `tenant@thingsboard.org` / `tenant` | created by `LOAD_DEMO=true` at install | Hardcoded in `fleet_common.py:43-44`; every `tb_*.py` logs in with it. A clean install without demo data leaves only sysadmin and *all* provisioning fails. |

> **The node has no fallback IP.** `CONFIG_AMI_LWM2M_SERVER_IPV6_*` was deleted in
> v0.6.65 (`prj.conf:389-396`) after a stale fallback masked a server-side routing
> bug for hours. Discovery is now pure DNS-SD: **if the SRP record is absent,
> nothing else you do to ThingsBoard matters.**

---

## 1. Prerequisites (verify, don't assume)

Everything runs from the repo root:

```powershell
cd C:\Users\jsgir\Documents\UNAL\Unal-Flash-tool\firmware\ami-lwm2m-node
```

### 1.1 Environment facts this runbook assumes

* Windows 11 + **WSL2 Ubuntu-24.04**, `.wslconfig` has `networkingMode=NAT`
  (**mirrored is broken on this host — do not switch it**).
* **Docker runs natively inside WSL** (`/var/run/docker.sock`), *not* Docker
  Desktop. Never prefix commands with `docker.exe`; always go through
  `wsl -d Ubuntu-24.04 -u root -- docker ...`.
* **OTBR is native systemd `otbr-agent` inside WSL**, not a container.
  (`overlays/lab.conf`'s header comment still says "openthread/otbr docker
  container" — stale, cosmetic only.)
* `ot-ctl` needs **`-u root`**: the default WSL user cannot open
  `/run/openthread-wpan0.sock`.

### 1.2 Preconditions

```powershell
# Thread network up and this host is on it
wsl -d Ubuntu-24.04 -u root -- ot-ctl state            # expect: leader
wsl -d Ubuntu-24.04 -u root -- ot-ctl srp server state # expect: running
wsl -d Ubuntu-24.04 -u root -- ot-ctl br omrprefix     # expect: fdaf:e549:1751:1::/64
wsl -d Ubuntu-24.04 -u root -- ip -6 addr show dev wpan0

# Docker native inside WSL
wsl -d Ubuntu-24.04 -u root -- docker version --format "{{.Server.Version}}"
wsl -d Ubuntu-24.04 -u root -- docker images thingsboard/tb-node

# Ports that MUST be free in WSL: tcp/8080 tcp/5432 udp/5683 udp/5684
wsl -d Ubuntu-24.04 -u root -- ss -lntup | grep -E ":(8080|5432|5683|5684)\b"   # expect: nothing
```

If `srp server state` is `disabled`:
`wsl -d Ubuntu-24.04 -u root -- ot-ctl srp server enable`.

If `br omrprefix` errors, border routing is down — fix that first (see
`docs/LAB_OTBR_BRINGUP.md`). **Do not work around it**; without an OMR prefix the
publisher will correctly refuse to advertise anything.

### 1.3 Python

`tools/lab_tb/lab_tb_common.py` calls `fleet_common.bootstrap_venv()` at import,
so a bare `python` re-execs itself under the project venv when `requests` is
missing. Either of these works:

```powershell
python tools\lab_tb\lab_tb_provision.py --help
C:\Users\jsgir\Documents\ESP32\.venv\Scripts\python.exe tools\lab_tb\lab_tb_provision.py --help
```

---

## 2. Start ThingsBoard

`thingsboard/tb-node:4.3.1.3` (already on the bench, 2.32 GB) **is** the CE
monolith for 4.3.x — all transports in one JVM, `queue.type=in-memory`,
`cache.type=caffeine`, no Kafka, no Redis. Only `postgres:18` (~150 MB) is pulled.

**First run — one command:**

```powershell
.\tools\lab_tb\lab_tb.ps1 -Action bootstrap
```

`bootstrap` = `preflight` -> `pull` -> `install` -> `up` (and `up` ends by running
`verify`). Budget **5-10 min**.

**Or phase by phase** (recommended the very first time, so a preflight failure is
unambiguous):

```powershell
.\tools\lab_tb\lab_tb.ps1 -Action preflight   # read-only; must PASS before install
.\tools\lab_tb\lab_tb.ps1 -Action pull        # ~150 MB (postgres:18 only)
.\tools\lab_tb\lab_tb.ps1 -Action install     # 2-6 min, ONE TIME, idempotent
.\tools\lab_tb\lab_tb.ps1 -Action up          # blocks until /login answers
```

`install` runs `docker compose run --rm -e INSTALL_TB=true -e LOAD_DEMO=true
thingsboard-ce`. **`LOAD_DEMO=true` is mandatory** — it is what creates
`tenant@thingsboard.org`.

<details>
<summary>Raw WSL equivalents (if the orchestrator prefers driving WSL directly)</summary>

```bash
D=/mnt/c/Users/jsgir/Documents/UNAL/Unal-Flash-tool/firmware/ami-lwm2m-node/tools/lab_tb
wsl -d Ubuntu-24.04 -u root -- bash -lc "cd $D && LAB_TB_DIR=\$PWD bash lab_tb_up.sh preflight"
wsl -d Ubuntu-24.04 -u root -- bash -lc "cd $D && docker compose -p lab-tb pull"
wsl -d Ubuntu-24.04 -u root -- bash -lc "cd $D && docker compose -p lab-tb run --rm -e INSTALL_TB=true -e LOAD_DEMO=true thingsboard-ce"
wsl -d Ubuntu-24.04 -u root -- bash -lc "cd $D && docker compose -p lab-tb up -d"
```
</details>

---

## 3. Verify the LwM2M transport is bound (gate 1)

```powershell
.\tools\lab_tb\lab_tb.ps1 -Action verify
```

Five gates, all must pass:

1. container `tb-lab` running
2. **udp/5683 bound, and the bind is `*:5683` or `[::]:5683` — not `0.0.0.0:5683`, not empty — owned by `java`**
3. `Started endpoint at coap://[0:0:0:0:0:0:0:0]:5683` in the container log, **no `BindException`**
4. `POST /api/auth/login` as `tenant@thingsboard.org` returns a token
5. `wpan0` has an off-mesh-local (OMR) address; prints the resulting `coap://[<OMR>]:5683`

The three raw checks, verbatim:

```powershell
wsl -d Ubuntu-24.04 -u root -- ss -lunp | grep 5683
wsl -d Ubuntu-24.04 -u root -- docker logs tb-lab 2>&1 | grep -i "Started endpoint"
wsl -d Ubuntu-24.04 -u root -- docker logs tb-lab 2>&1 | grep -i bindexception
```

Windows-side reachability (WSL2 NAT + `localhostForwarding`):

```powershell
.\tools\lab_tb\lab_tb.ps1 -Action url
```

If it reports that only the WSL IP works, use `--host <WSL_IP>` on every tool
below **and** change `fleet_common.MESH_TO_EDGE["lab"]` from `127.0.0.1` to that
IP.

> **Do not proceed past a failing gate 2 or 3.** Everything downstream will look
> like a node problem.

---

## 4. Publish the DNS-SD service (gate 2 — THE gate)

Nothing in the repo published this record before; this is the piece production
had (the Pi4's `otbr-srp` UCI service) and the bench did not. The mechanism is
the OTBR's **own** OpenThread SRP *client* registering with the SRP *server* on
the same instance — there is no `ot-ctl` command that writes the SRP registry
directly.

```powershell
# 4.1 read-only probe: does this otbr-agent even have the SRP client CLI?
python tools\lab_tb\lab_tb_srp.py probe
#     expect srp_client_cli=True, thread_state=leader, srp_server_state=running,
#     omr_prefix=fdaf:e549:1751:1::/64
#     srp_client_cli=False -> jump to the avahi fallback in §11.4

# 4.2 confirm the address that will be advertised
python tools\lab_tb\lab_tb_srp.py address
#     expect fdaf:e549:1751:1:1199:8c2b:a32e:38ee
#     exit 2 "refusing" = no OMR address; fix border routing, do not override

# 4.3 publish AND make it survive an otbr-agent restart (do both)
python tools\lab_tb\lab_tb_srp.py publish       # idempotent
python tools\lab_tb\lab_tb_srp.py install       # systemd unit, PartOf=otbr-agent.service
wsl -d Ubuntu-24.04 -u root -- systemctl status otbr-srp-lwm2m.service
```

> **Run `install`, not just `publish`.** The SRP registration lives in
> otbr-agent's RAM. Any restart (RCP renumbering, `wsl --shutdown`, a
> `systemctl restart`) silently drops it, and the node reboot-loops ~9 min later.
> This is the single most likely way the bench "mysteriously regresses".
> The unit re-asserts the record every 120 s, so an OMR prefix change self-heals.

<details>
<summary>Raw <code>ot-ctl</code> equivalent (prefix each with <code>wsl -d Ubuntu-24.04 -u root -- </code>)</summary>

```
ot-ctl srp server state          # running, else: ot-ctl srp server enable
ot-ctl srp client stop
ot-ctl srp client service clear
ot-ctl srp client host clear
ot-ctl srp client host name thingsboard-edge
ot-ctl srp client host address fdaf:e549:1751:1:1199:8c2b:a32e:38ee
ot-ctl srp client service add ThingsBoard-Edge _lwm2m._udp 5683
ot-ctl srp client autostart enable
ot-ctl srp client host state     # ToAdd -> Registered
```
</details>

---

## 5. Verify the record resolves the way the node will resolve it

```powershell
python tools\lab_tb\lab_tb_srp.py verify     # exit 0 = all four checks OK
```

The four checks, raw (prefix with `wsl -d Ubuntu-24.04 -u root -- `):

```
# L1/L2 — the record exists (what production's edge_health.py asserts)
ot-ctl srp server host
#   thingsboard-edge.default.service.arpa., deleted: false,
#   addresses: [fdaf:e549:1751:1:1199:8c2b:a32e:38ee]
ot-ctl srp server service
#   ThingsBoard-Edge._lwm2m._udp.default.service.arpa., deleted: false, port: 5683

# L3 — DECISIVE: literally the same OpenThread API the firmware calls
ot-ctl dns service ThingsBoard-Edge _lwm2m._udp.default.service.arpa.
#   Port:5683 ... Host:thingsboard-edge.default.service.arpa.
#   HostAddress:fdaf:e549:1751:1:...            == lwm2m_discover.c Strategy 1
ot-ctl dns resolve thingsboard-edge.default.service.arpa.
#   DNS response ... - fdaf:e549:1751:1:...     == lwm2m_discover.c Strategy 2
```

**Persistence proof** (run once, it is the regression the unit exists to prevent):

```powershell
wsl -d Ubuntu-24.04 -u root -- systemctl restart otbr-agent
# wait ~60 s
python tools\lab_tb\lab_tb_srp.py verify      # must still be 4/4
```

---

## 6. Provision models + profile + device

**Order matters and every skipped step fails silently.** Models must exist
*before* the node registers or TB drops the observes with no log line; Leshan
loads models into `LwM2mModelProvider` at **startup only**, so the container must
be restarted after an upload; and a profile change reaches a device only on
REGISTER.

`lab_tb_provision.py` does all of it in the right order, importing the production
tooling rather than re-implementing it (`tb_edge_provision.build_profile_body` /
`apply_profile` / `provision_device`, `tb_edge_upload_models.upload_model`,
`tb_edge_monitoring_setup.OBSERVE_ADDITIONS` / `apply_observe`).

```powershell
# 6.1 plan only, no writes
python tools\lab_tb\lab_tb_provision.py --dry-run

# 6.2 do it
python tools\lab_tb\lab_tb_provision.py
```

What it does:

1. wait for TB (a cold stack answers the TCP port 60-180 s before `/api/auth/login`)
2. upload **6** LwM2M object models — `3, 5, 10242, 33000, 3303, 33001` — from
   `models/`; byte-identical files are skipped
3. `docker restart tb-lab` **if a model changed** (`--restart always|never` to force)
4. device profile `AMI_LwM2M_Node` (production body) + `OBSERVE_ADDITIONS` + bench pmax policy
5. device `ami-esp32c6-3bb0` + `LWM2M_CREDENTIALS` / **NO_SEC**, `credentialsId == endpoint`
6. per-device `inactivityTimeout` = 20 min

Useful variants:

```powershell
# fast-converging bench: uptime_s heartbeat every 60 s, and model RIDs 23..37 added
python tools\lab_tb\lab_tb_provision.py --uptime-pmax 60 --full-33000
# localhost doesn't forward into WSL
python tools\lab_tb\lab_tb_provision.py --host <WSL_IP> --port 8080
# force a restart when a model's CONTENT changed but its title didn't
python tools\lab_tb\lab_tb_provision.py --restart always
# alarms + dashboard layer (optional, second pass)
python tools\lab_tb\lab_tb_provision.py --monitoring
```

**Re-run it once immediately** — it must be idempotent: `[same]` for all 6
models, `[restart] skipped (no model changed)`, `[observe] all additions already
present`, exit 0.

Two warnings it prints are **real defects, not bench artifacts** — note them,
don't silence them:

* the effective observe list is **44 paths** but the firmware ships
  `CONFIG_LWM2M_ENGINE_MAX_OBSERVER=36` (`prj.conf:304`) — 8 observes are
  silently refused client-side
* `/10242_1.0/0/4,5,6` (voltage, current, activePower) are observed with
  `pmax=0` in production — no protocol-enforced heartbeat, so a steady load is
  indistinguishable from a dead node. `--pmax-floor` (default 900 s) fixes it on
  the bench; worth backporting to the fleet.

> `ObjectVersion` stays **1.0** for object 33000 even though the title says v2.2.
> Bumping it is the documented trap: experimental IDs >= 32768 need an exact model
> match, the firmware's LwM2M-1.0 REGISTER emits a bare `</33000>` with no `;ver=`,
> TB falls back to `defaultObjectIDVer="1.0"`, and with a "2.2" model uploaded it
> silently drops every observe and telemetry mapping for the whole object.

Now **power-cycle the node** (profile changes propagate only on REGISTER).

---

## 7. Verify registration + telemetry (final gate)

```powershell
python tools\lab_tb\lab_tb_check.py            # ~5 s quick status
python tools\lab_tb\lab_tb_check.py --strict   # ~2.5 min, THE gate
python tools\lab_tb\lab_tb_check.py --strict --json
```

Exit **0** = PASS, **1** = at least one FAIL, **2** = bench TB unreachable.

| # | check | PASS requires |
|---|---|---|
| 1 | thingsboard | `/api/auth/login` returns a token, typically < 2 s |
| 2 | lwm2m transport | udp/**5683** bound in the WSL netns |
| 2b | lwm2m bind log | `Started endpoint at coap://[0:0:0:0:0:0:0:0]:5683`, no `BindException` |
| 3 | srp server | `ot-ctl srp server state` = `running` |
| 3b | srp host | `thingsboard-edge.default.service.arpa.`, `deleted: false` |
| 3c | srp service | `ThingsBoard-Edge._lwm2m._udp...`, `deleted: false`, **`port:5683`** |
| 3d | srp address | >= 1 advertised address **outside** the mesh-local /64 |
| 4 | device profile | `transportType=LWM2M`, `/33000_1.0/0/10` observed with `pmax > 0` |
| 4b | observer budget | observed paths <= 36 (WARN above) |
| 4c | device | `LWM2M_CREDENTIALS` + `NO_SEC` + `credentialsId == ami-esp32c6-3bb0` |
| 5 | registered | server-scope `lastActivityTime` age <= 360 s, `active=true` |
| 6 | telemetry | >= 8 Object-33000 keys, `uptime_s` age <= 360 s |
| 6b | telemetry live | `uptime_s` **strictly increases** over a 120 s window |
| 7 | inbound rpc | two-way `Read /3/0/3` answers within 15 s |

*Why 360 s:* `uptime_s` runs `pmin/pmax = 60/300`, plus 20 % slack. Provisioned
with `--uptime-pmax 60`? Then gate tighter:
`--strict --max-age 90 --delta-wait 90`.

*Why 6b and 7 are separate from 5:* a path in `keyName`/`telemetry` but missing
from `observe` shows its **registration-payload** value forever (this repo's #1
recurring bug), and a node can keep REGISTERing outbound long after inbound
delivery is black-holed (the "liveness gate: inbound vs outbound" failure).
`lastActivityTime` alone proves neither.

Independent confirmations:

```powershell
# the server saw it
wsl -d Ubuntu-24.04 -u root -- docker logs tb-lab 2>&1 | grep -i "New registration"
# the node is alive, WITHOUT a COM port and WITHOUT ThingsBoard
wsl -d Ubuntu-24.04 -- python3 tools/diag_get.py --local --addr <node-OMR>
#   reg_ok >= 1  proves discovery resolved AND TB answered the REGISTER
```

`reg_ok == 0` with §5 green means the fault moved downstream to TB provisioning
(§6), **not** discovery — which is exactly the isolation this bench needed.

---

## 8. Close the loop — make the bench representative

This is the point of the whole exercise. Once `lab_tb_check.py --strict` passes,
delete these two lines from `overlays/lab.conf` (they exist *only* because the
bench had no server) and rebuild:

```
CONFIG_AMI_BOOT_REGISTER_DEADLINE_S=0
CONFIG_AMI_HW_WATCHDOG_BOOT_GRACE_HARD_S=3600
```

```powershell
python tools\build_firmware.py --variant med --mesh lab
# flash, then re-gate:
python tools\lab_tb\lab_tb_check.py --strict
```

**A second `--strict` PASS on that build is the real acceptance criterion.**
Until then you are still validating a serverless configuration.

---

## 9. Day-to-day operations

```powershell
.\tools\lab_tb\lab_tb.ps1 -Action status
.\tools\lab_tb\lab_tb.ps1 -Action logs -Follow
.\tools\lab_tb\lab_tb.ps1 -Action restart      # MANDATORY after any model upload
.\tools\lab_tb\lab_tb.ps1 -Action srpinfo      # print the DNS-SD contract + checks
python tools\lab_tb\lab_tb_srp.py verify
python tools\lab_tb\lab_tb_check.py
```

Other fleet tools now reach the bench because `fleet_common.MESH_TO_EDGE` gained
`"lab": ("127.0.0.1", 8080)`:

```powershell
python tools\tb_edge_monitoring_setup.py --mesh lab
python tools\provision_node.py --endpoint ami-esp32c6-3bb0 --host 127.0.0.1 --port 8080 --profile AMI_LwM2M_Node --verify
```

Web UI: <http://localhost:8080> — `tenant@thingsboard.org` / `tenant`.

---

## 10. Teardown

```powershell
# stop TB, keep the database
.\tools\lab_tb\lab_tb.ps1 -Action down

# unpublish the DNS-SD record (node will start failing discovery immediately)
python tools\lab_tb\lab_tb_srp.py remove
python tools\lab_tb\lab_tb_srp.py uninstall        # also removes the systemd unit

# full wipe: containers + postgres volume (destroys all provisioning)
.\tools\lab_tb\lab_tb.ps1 -Action reset -Yes
# then start again from §2 bootstrap
```

Order matters on the way down too: remove the SRP record **before** stopping TB
if you want the node to fail cleanly on discovery rather than time out on a
half-dead server.

---

## 11. Troubleshooting

### 11.1 Node logs `DNS-SD lookup failed (err=-2)` and reboots

`src/main.c:899` logs `DNS-SD lookup failed (err=%d); attempt %d/%d` with
exponential backoff (5s, 10s, 20s, ... capped 60 s). After
`CONFIG_AMI_LWM2M_DNS_SD_RETRY_MAX` = **10** attempts (~9 min) it
`sys_reboot(SYS_REBOOT_WARM)` (`src/main.c:3445-3452`). `err=-2` = `-ENOENT` =
**the name does not exist**, i.e. the server side never published it.

Diagnose top-down; stop at the first failure:

| Probe | Bad result -> cause |
|---|---|
| `python tools\lab_tb\lab_tb_srp.py verify` | any FAIL -> the record is gone. Did `otbr-agent` restart? Did you run `install` or only `publish`? |
| `ot-ctl srp server state` | not `running` -> `ot-ctl srp server enable` |
| `ot-ctl srp server service` | empty -> re-run `lab_tb_srp.py publish` |
| `ot-ctl srp client host state` | stuck at `ToAdd` -> autostart didn't find the local server; retry with `--server "[<mleid>]:<port>"` |
| `ot-ctl dns service ...` | NXDOMAIN while `srp server service` shows the record -> DNS-SD server-side resolution issue, not publication |
| on the node: `ot dns config` | shows `2001:4860:4860::8888` -> the node's DNS client never adopted the mesh SRP server; unroutable, every lookup times out with a perfectly healthy server side |
| `python tools\diag_get.py --local --addr <node-OMR>` | no answer -> the node is dead/off-mesh; this is not a discovery problem at all |

**The record survives nothing by default.** If discovery worked yesterday and
doesn't today, check `systemctl status otbr-srp-lwm2m.service` first.

### 11.2 ThingsBoard / transport

| Symptom | Cause | Fix |
|---|---|---|
| `java.net.BindException: Address already in use` near the LwM2M init | TB's CoAP transport grabbed 5683 first | all three of `COAP_ENABLED=false`, `COAP_SERVER_ENABLED=false`, `COAP_BIND_PORT=5690`. Verify with `docker exec tb-lab env \| grep -E "COAP\|LWM2M"` |
| `ss` shows `0.0.0.0:5683` instead of `[::]:5683` | IPv4-only bind — **invisible to the mesh** | `LWM2M_BIND_ADDRESS: "::"`. If TB rejects `::`, delete the `*BIND_ADDRESS` lines and let the JVM dual-stack wildcard do it |
| No udp/5683 at all, no BindException | env var name ignored by Spring (silently) | authoritative names: `docker run --rm --entrypoint sh thingsboard/tb-node:4.3.1.3 -c "grep -n -A3 'lwm2m:' /usr/share/thingsboard/conf/thingsboard.yml"`. Note `LWM2M_SERVER_PORT` **does not exist** — it is `LWM2M_BIND_PORT` |
| REGISTER arrives but every Observe/RPC times out | container is **bridged**, not host-networked | `network_mode: host` on both services; `ports:` is ignored in host mode |
| `/api/auth/login` returns 401 for `tenant@thingsboard.org` | installed without `LOAD_DEMO=true` | `-Action reset -Yes` then `-Action bootstrap` |
| `http://localhost:8080` refused from Windows | WSL2 NAT + `localhostForwarding` | `-Action url` tells you which URL works; if it's the WSL IP, update `MESH_TO_EDGE["lab"]` |
| TB won't start / OOM | `Xmx2048m` + postgres needs ~3 GB | `TB_JAVA_XMX` in `tools/lab_tb/.env`, or `memory=` in `.wslconfig` (**leave `networkingMode=NAT` alone**) |
| postgres data vanished after recreate | `TB_PG_IMAGE` swapped to <= 16 without changing the mount | postgres 18 keeps PGDATA at `/var/lib/postgresql/18/docker`; the volume mounts `/var/lib/postgresql` |

### 11.3 Registered but no / frozen telemetry

| Symptom | Cause | Fix |
|---|---|---|
| Registration OK, **0** telemetry keys ever map | models missing or uploaded *after* registration | re-run `lab_tb_provision.py`, `-Action restart`, power-cycle the node. TB log: `Tenant hasn't such the resource: Object model with id [10242] version [1.0]` |
| One key frozen at its boot value | path in `keyName`/`telemetry` but **absent from `observe`** — the repo's #1 recurring bug | `lab_tb_provision.py` applies `OBSERVE_ADDITIONS`; then power-cycle |
| Some keys never appear at all | 44 observed paths vs `CONFIG_LWM2M_ENGINE_MAX_OBSERVER=36` | expected today; real fix is a firmware Kconfig bump or a trimmed observe list |
| `/33000` telemetry entirely absent | the 33000 model was uploaded with `ObjectVersion` 2.2 | keep it **1.0**; profile paths stay `/33000_1.0/...` |
| RPC to `/33001/0/x` -> `unable to find obj: 33001` | 33001 model not uploaded (it has no generator, only `models/33001.xml`) | `lab_tb_provision.py` uploads it from disk; confirm the file exists |
| Write RPC -> `METHOD_NOT_ALLOWED` | method is **`WriteReplace`**, not `Write` | use `WriteReplace` |
| Device flaps `active`/inactive | TB default inactivity 600 s vs sparse LwM2M traffic | already handled (`DEFAULT_INACTIVITY_TIMEOUT=1200` + per-device 20 min) |
| Registration rejected, `LwM2MAuthException`, node reboot-loops | `provisionType=DISABLED` and the device doesn't pre-exist | run `lab_tb_provision.py` **before** the node registers |

### 11.4 Fallback: avahi + OTBR Discovery Proxy

Only if `lab_tb_srp.py probe` reports `srp_client_cli=False` (i.e. `ot-ctl srp
client` answers `Error 35: InvalidCommand`):

```powershell
wsl -d Ubuntu-24.04 -u root -- apt-get install -y avahi-daemon avahi-utils
python tools\lab_tb\lab_tb_srp.py remove          # NEVER run both paths at once
python tools\lab_tb\lab_tb_srp.py install-avahi
wsl -d Ubuntu-24.04 -u root -- ot-ctl dns browse _lwm2m._udp.default.service.arpa.
```

NXDOMAIN there means `otbr-agent` was built without
`OTBR_DNSSD_DISCOVERY_PROXY=ON` — a **build flag with no runtime switch** — and
this path cannot work; rebuild otbr-agent or fix the SRP client path.

> Never run SRP **and** avahi together: the OTBR Advertising Proxy already
> mirrors SRP into mDNS, so a second avahi copy collides and avahi renames the
> instance to `ThingsBoard-Edge #2` — which stops matching `SRV_INSTANCE_LABEL`
> and reads exactly like a firmware bug.

### 11.5 Environment gotchas that waste hours

* **`ot-ctl` without `-u root`** -> permission error on
  `/run/openthread-wpan0.sock`. Always `wsl -d Ubuntu-24.04 -u root -- ot-ctl ...`.
* **Docker is native in WSL, not Desktop** -> `docker.exe` from PowerShell talks
  to the wrong daemon (or none). Always tunnel through `wsl ... -- docker`.
* **`networkingMode=NAT`** in `.wslconfig` — mirrored mode is broken on this host.
  Nothing here is reachable from the LAN, which is fine: the mesh reaches TB over
  `wpan0`, not over eth0.
* **CRLF** — `lab_tb_up.sh`, `srp_publish_lwm2m.sh` and the two `.service` files
  are LF-pinned in `tools/lab_tb/.gitattributes`; a CRLF systemd unit dies with
  `/bin/sh^M: bad interpreter`. `lab_tb.ps1` also strips CR at call time.
* **`docs/PROVISIONING.md` is stale** (profile `C2000_Monofasico_v2`,
  `192.168.8.176`). Follow the code. `overlays/lab.conf`'s "otbr docker container"
  comment is likewise stale — OTBR is native systemd here.
* **`tools/tb_central_provision.py` must not be used on the bench** — it is
  cloud-attached-Edge only (hardcoded `192.168.8.124`, paramiko SSH into the Pi4).

---

## 12. File inventory

| Path | Role |
|---|---|
| `tools/lab_tb/docker-compose.yml` | the stack: `postgres:18` + `thingsboard/tb-node:4.3.1.3`, host networking, LwM2M NoSec on `[::]:5683` |
| `tools/lab_tb/.env.example` | documented overrides; copy to `.env` only to deviate |
| `tools/lab_tb/lab_tb_up.sh` | in-WSL worker: `preflight pull install up verify status logs restart down reset srpinfo bootstrap` |
| `tools/lab_tb/lab_tb.ps1` | Windows driver (`-Action`, path translation, CRLF-safe, `url` check) |
| `tools/lab_tb/lab_tb_srp.py` | DNS-SD publisher/verifier: `probe address publish verify remove install uninstall install-avahi` |
| `tools/lab_tb/srp_publish_lwm2m.sh` | POSIX engine installed to `/usr/local/sbin/`, also runs standalone |
| `tools/lab_tb/otbr-srp-lwm2m.service` | systemd unit, `PartOf=otbr-agent.service`, re-asserts every 120 s |
| `tools/lab_tb/avahi-lwm2m.service` | fallback mDNS service definition (§11.4) |
| `tools/lab_tb/lab_tb_common.py` | shared glue: TB discovery/wait, WSL/docker/ot-ctl bridge, SRP parsing, DNS-SD constants |
| `tools/lab_tb/lab_tb_provision.py` | 6 models -> restart -> profile -> device + NoSec credentials |
| `tools/lab_tb/lab_tb_check.py` | 12-check end-to-end PASS/FAIL gate |
| `tools/lab_tb/README.md` | per-tool reference for the above |
| `tools/lab_tb/.gitattributes` | LF pinning for the files executed inside WSL |
| `tools/fleet_common.py` | **modified**: `MESH_TO_EDGE["lab"] = ("127.0.0.1", 8080)` |
| `docs/LAB_LWM2M_DISCOVERY.md` | DNS-SD deep dive (why SRP over avahi, the 4 verification layers) |
| `docs/LAB_THINGSBOARD.md` | **this file** — the ordered bring-up |
