# LAB — Making the Bench Node DISCOVER the ThingsBoard Server

> **Scope:** the one missing link between "the node attaches to the bench Thread
> mesh" and "the node REGISTERs with ThingsBoard". Standing up TB itself is
> covered separately; this document is only about getting
> `ThingsBoard-Edge._lwm2m._udp.default.service.arpa.` to resolve **from inside
> the Thread mesh**, on the WSL2 bench described in `docs/LAB_OTBR_BRINGUP.md`.

## TL;DR

```powershell
# 1. what can this otbr-agent do?
python tools\lab_tb\lab_tb_srp.py probe

# 2. publish + prove it (this is THE gate — exit 0 means the node can find TB)
python tools\lab_tb\lab_tb_srp.py publish

# 3. make it survive an otbr-agent restart
python tools\lab_tb\lab_tb_srp.py install

# 4. power-cycle the node, then confirm end-to-end without a COM port
wsl -d Ubuntu-24.04 -- python3 tools/diag_get.py --local --addr <node-OMR>
#    -> "reg_ok": 1  (or higher)
```

---

## 1. Why the bench node cannot find the server on its own

`src/lwm2m_discover.c` is the **only** path by which the firmware ever learns a
server address. `CONFIG_AMI_LWM2M_SERVER_IPV6_PRIMARY/_SECONDARY` still exist as
Kconfig symbols (`Kconfig:280-296`) but the code stopped reading them in
v0.6.65, deliberately — `prj.conf:385-395` records why (a stale fallback IP
masked a real routing bug for hours during the 2026-06-04 incident).

Three strings are pinned in `src/lwm2m_discover.c:24-26`. They are a **protocol
contract with the server side**, not configuration:

| Constant | Value |
|---|---|
| `SRV_INSTANCE_LABEL` | `ThingsBoard-Edge` |
| `SRV_TYPE_DOMAIN` | `_lwm2m._udp.default.service.arpa.` |
| `HOST_FQDN` | `thingsboard-edge.default.service.arpa.` |

The node tries two strategies, in order:

| # | Call | Source of address | Source of port |
|---|---|---|---|
| 1 | `otDnsClientResolveService(SRV_INSTANCE_LABEL, SRV_TYPE_DOMAIN)` (`:166`) | AAAA in the SRV **additional section**, taken verbatim (`:90`) — the client cannot choose among several | SRV record (`:91`) |
| 2 | `otDnsClientResolveAddress(HOST_FQDN)` (`:188`) | AAAA, **preferring an off-mesh-local address** (`:113-131`) | **hardcoded 5683** (`:22`, `:148`) |

If both fail, `lwm2m_discover_with_retry()` (`src/main.c:875-907`) backs off
5→10→20…60 s for `CONFIG_AMI_LWM2M_DNS_SD_RETRY_MAX` = 10 attempts (~9 min) and
then `sys_reboot(WARM)`. **A bench with no published record cannot register, no
matter how healthy ThingsBoard is.** That is precisely the failure this bench
has been manufacturing, and why `overlays/lab.conf:24-33` had to disable the
boot-register deadline to keep the node alive at all.

On the production Pi4 a persistent `otbr-srp` service kept that record in the
SRP server (`docs/DYNAMIC_DISCOVERY.md`); `tools/edge_health.py:94-100` asserts
it exists and hints *"publish via avahi-publish-host or SRP client"*. **Nothing
in this repo ever created it.** The tooling below does.

---

## 2. The design decision

ThingsBoard runs on the WSL host. The WSL host is **not** a Thread node, so it
cannot run an SRP client of its own and register itself the way a mesh device
would. Three candidate mechanisms:

### (a) avahi on the infra link + the OTBR **Discovery Proxy**

The textbook answer: the Discovery Proxy exists exactly to let Thread nodes
find services that live on the infrastructure side. `otbr-agent` receives the
node's query for `_lwm2m._udp.default.service.arpa.`, browses `_lwm2m._udp.local`
over mDNS on the infra interface, and translates the answer back into the Thread
domain.

Real problems on **this** bench:
- It only works if `otbr-agent` was compiled with `OTBR_DNSSD_DISCOVERY_PROXY=ON`
  (plus `OPENTHREAD_CONFIG_DNS_DYNAMIC_PROXY_ENABLE`). There is **no runtime
  switch** — if it is off, the only symptom is NXDOMAIN.
- The infra interface in WSL2 is a NAT'd `eth0` inside a VM. mDNS there is a
  moving part we gain nothing from.
- Controlling the advertised AAAA is awkward: avahi naturally publishes the
  addresses of the interface it advertises on, and the address we need is a
  **wpan0** address avahi does not own. It takes a static `/etc/avahi/hosts`
  entry to force it.

Kept as the documented fallback. Not the default.

### (b) ✅ Register into the OTBR's own SRP server, via the OTBR's own SRP client

`otbr-agent` runs a full OpenThread instance — SRP **server** *and* SRP
**client**. We drive that client to register:

```
host    thingsboard-edge      ->  <wpan0 OMR address>
service ThingsBoard-Edge._lwm2m._udp  port 5683
```

This is *truthful*: the OMR address really is an address of the machine that is
running ThingsBoard, so we are not faking a record, we are stating a fact the
Thread side had no other way to learn.

Why it wins here:
1. The record lands in `ot-ctl srp server service` — the exact place production
   has it and `edge_health.py` checks, so the bench's verification surface
   matches the fleet's.
2. No optional build flag beyond `OT_SRP_CLIENT`, which we probe first and
   which is standard in `ot-br-posix` builds.
3. **We pin exactly one address.** Strategy 1 has no way to choose among
   several AAAAs (`lwm2m_discover.c:86-91`); Strategy 2 warns
   *"only mesh-local address available (may break src/dst symmetry on BR)"*.
   Registering only the OMR address makes both strategies agree, and encodes
   the 2026-06-04 fix by construction instead of by luck.
4. No mDNS, no D-Bus, no avahi, no infra-link multicast.

### (c) An `ot-ctl` command that injects into the SRP server directly

Does not exist. The SRP server registry is only writable by SRP updates (which
are signed DNS UPDATEs). **(b) *is* the `ot-ctl` mechanism for adding a local
service** — the loopback registration is the supported way to do it.

---

## 3. What gets published

| Field | Value | Comes from |
|---|---|---|
| Service instance | `ThingsBoard-Edge` | `lwm2m_discover.c:24` — must match exactly |
| Service type | `_lwm2m._udp` | `lwm2m_discover.c:25` |
| Domain | `default.service.arpa.` | Thread DNS-SD default domain |
| Host label | `thingsboard-edge` | `lwm2m_discover.c:26` |
| **Port** | **5683** | `lwm2m_discover.c:22` — hardcoded on the Strategy-2 path |
| AAAA | wpan0 **OMR** address, e.g. `fdaf:e549:1751:1:1199:8c2b:a32e:38ee` | picked live from `ot-ctl ipaddr` ∩ `ot-ctl br omrprefix` |

### The port is not a preference

Strategy 2 hardcodes 5683. Even if the SRV record advertised something else and
Strategy 1 honoured it, any node that falls through to the host-AAAA path would
talk to the wrong port and look "registered but silent". **Bind the ThingsBoard
LwM2M transport on UDP 5683.**

Related landmine already burned once on this project (`.context/ESTADO.md:148-169`):
the TB CoAP transport grabs 5683 **first** and LwM2M then dies with
`java.net.BindException: Address already in use`. The recorded fix is
`LWM2M_ENABLED=true` + `LWM2M_BIND_PORT=5683` + `COAP_BIND_PORT=5690` +
`COAP_ENABLED=false` + `COAP_SERVER_ENABLED=false`. Grep the container log for
`Started endpoint at coap://[0:0:0:0:0:0:0:0]:5683` before believing anything
here.

### The address is not arbitrary either

The advertised AAAA must be **off-mesh-local (the OMR address)**, not the
mesh-local EID and not an RLOC/ALOC. When the node sends to the OMR address,
Linux picks that same OMR address as the source of the reply, so the CoAP
src/dst pair stays symmetric. A mesh-local target is what broke the fleet on
2026-06-04 (`docs/DYNAMIC_DISCOVERY.md` incident table) and it is why
`lwm2m_discover.c:50-57` exists at all. The publisher **refuses** to advertise a
mesh-local address rather than publish that trap.

---

## 4. Publish it

### 4.1 With the tool (recommended)

```powershell
# Which mechanisms does this otbr-agent actually have?
python tools\lab_tb\lab_tb_srp.py probe
#   thread_state      leader
#   srp_server_state  running
#   srp_client_cli    True         <- if False, jump to section 7 (avahi fallback)
#   omr_prefix        fdaf:e549:1751:1::/64

# Which address will be advertised? (sanity-check before publishing)
python tools\lab_tb\lab_tb_srp.py address
#   fdaf:e549:1751:1:1199:8c2b:a32e:38ee

# Publish, then verify. Exit 0 == the node can discover the server.
python tools\lab_tb\lab_tb_srp.py publish
```

Everything is tunnelled through `wsl -d Ubuntu-24.04 -u root -- ot-ctl` by
default. Overrides:

```powershell
# containerised OTBR instead of the native systemd one
python tools\lab_tb\lab_tb_srp.py publish --exec "wsl -d Ubuntu-24.04 docker exec otbr ot-ctl"
# from inside WSL
python3 tools/lab_tb/lab_tb_srp.py publish --exec "ot-ctl"
# machine-readable for the orchestrator
python tools\lab_tb\lab_tb_srp.py publish --json
```

The publish is **idempotent**: if the registry already holds exactly the right
host + service + port + address it is a no-op, so it is safe to re-run from any
bring-up script.

### 4.2 By hand (`ot-ctl` only)

Prefix every line with `wsl -d Ubuntu-24.04 -u root -- ` from Windows.

```bash
# 0. preconditions
ot-ctl state                 # leader | router | child
ot-ctl br state              # running   (no OMR prefix without this)
ot-ctl br omrprefix          # Local: fdaf:e549:1751:1::/64
ot-ctl ipaddr                # pick the address inside that prefix
ot-ctl srp server state      # running  (else: ot-ctl srp server enable)

# 1. register (clean slate first; errors on a first run are expected)
ot-ctl srp client stop
ot-ctl srp client service clear
ot-ctl srp client host clear

ot-ctl srp client host name thingsboard-edge
ot-ctl srp client host address fdaf:e549:1751:1:1199:8c2b:a32e:38ee
ot-ctl srp client service add ThingsBoard-Edge _lwm2m._udp 5683
ot-ctl srp client autostart enable

# 2. wait for it
ot-ctl srp client host state      # ToAdd -> Registered
```

`stop` + `host clear` resets the *client's* view without touching the ECDSA key
OpenThread persists in settings, so the re-registration reuses the same KEY and
the server treats it as an update of the same name rather than a name conflict.
That is what makes the sequence re-runnable.

If `host state` never leaves `ToAdd`, `autostart` did not find the local SRP
server. Read where it is looking with `ot-ctl srp client server`, or start it
explicitly:

```bash
ot-ctl srp client start <server-ipv6> <server-port>
# equivalently:  python tools\lab_tb\lab_tb_srp.py publish --server "[fdfe:...:4f2e]:53535"
```

### 4.3 Make it persistent

**The SRP registration lives in `otbr-agent`'s RAM.** Anything that restarts
otbr-agent — RCP renumbering, `wsl --shutdown`, a `systemctl restart` — silently
drops it, and the node then reboot-loops on DNS-SD failure roughly 9 minutes
later. This is the bench equivalent of the Pi4's `otbr-srp` UCI service.

```powershell
python tools\lab_tb\lab_tb_srp.py install
```

That pushes two files into WSL (normalising CRLF→LF on the way, which is the
usual reason a Windows-authored unit file mysteriously fails):

| Repo file | Installed to |
|---|---|
| `tools/lab_tb/srp_publish_lwm2m.sh` | `/usr/local/sbin/srp_publish_lwm2m.sh` (0755) |
| `tools/lab_tb/otbr-srp-lwm2m.service` | `/etc/systemd/system/otbr-srp-lwm2m.service` |

then `systemctl daemon-reload && systemctl enable --now otbr-srp-lwm2m.service`.
The unit is `PartOf=otbr-agent.service`, so it restarts exactly when the
registration would have been lost, and its `daemon` mode re-asserts the record
every 120 s — repairing a Thread re-attach or an OMR prefix change without a
human.

```bash
systemctl status otbr-srp-lwm2m.service
journalctl -u otbr-srp-lwm2m.service -f
```

---

## 5. Prove a Thread node can resolve it

Four layers, cheapest first. Layers 1–2 say *the record exists*; layer 3 runs
**the same OpenThread API the firmware calls**; layer 4 is the node itself.

### Layer 1–2 — the registry (what production checks)

```bash
ot-ctl srp server host
#   thingsboard-edge.default.service.arpa.
#       deleted: false
#       addresses: [fdaf:e549:1751:1:1199:8c2b:a32e:38ee]

ot-ctl srp server service
#   ThingsBoard-Edge._lwm2m._udp.default.service.arpa.
#       deleted: false
#       port: 5683
#       host: thingsboard-edge.default.service.arpa.
```

### Layer 3 — resolve it through the DNS-SD server (the decisive one)

`ot-ctl dns service` is `otDnsClientResolveService()` and `ot-ctl dns resolve`
is `otDnsClientResolveAddress()` — byte for byte the two calls in
`lwm2m_discover.c`, minus the radio hop. If these pass, the only thing left
between the OTBR and the node is RF.

```bash
ot-ctl dns service ThingsBoard-Edge _lwm2m._udp.default.service.arpa.
#   ThingsBoard-Edge._lwm2m._udp.default.service.arpa.
#   Port:5683, Priority:0, Weight:0, TTL:7200
#   Host:thingsboard-edge.default.service.arpa.
#   HostAddress:fdaf:e549:1751:1:1199:8c2b:a32e:38ee TTL:7200

ot-ctl dns resolve thingsboard-edge.default.service.arpa.
#   DNS response for thingsboard-edge.default.service.arpa.
#     - fdaf:e549:1751:1:1199:8c2b:a32e:38ee TTL: 7200

ot-ctl dns browse _lwm2m._udp.default.service.arpa.      # also useful
```

All four checks in one command, with a non-zero exit on failure:

```powershell
python tools\lab_tb\lab_tb_srp.py verify
```

```
[ OK ] srp server host: thingsboard-edge.default.service.arpa. -> ['fdaf:...:38ee']
[ OK ] srp server service: ThingsBoard-Edge._lwm2m._udp... port=5683 deleted=false
[ OK ] dns service (firmware Strategy 1): Port:5683 ... HostAddress:fdaf:...:38ee
[ OK ] dns resolve (firmware Strategy 2): DNS response ... fdaf:...:38ee
```

### Layer 4 — from the node

The firmware ships `CONFIG_SHELL=y` + `CONFIG_OPENTHREAD_SHELL=y`
(`prj.conf:265,282`), so the OT CLI is available on the node's console — the
native USB-Serial-JTAG port, or UART0 if built with
`overlays/console_uart0.overlay`. Open it with `dtr=False rts=False` so the node
is not reset by the port opening.

```
uart:~$ ot state
child
uart:~$ ot dns config                     # <-- check this FIRST, see troubleshooting
Server: [fdaf:e549:1751:1:1199:8c2b:a32e:38ee]:53
uart:~$ ot dns service ThingsBoard-Edge _lwm2m._udp.default.service.arpa.
Port:5683, Priority:0, Weight:0, TTL:7200
Host:thingsboard-edge.default.service.arpa.
HostAddress:fdaf:e549:1751:1:1199:8c2b:a32e:38ee TTL:7200
uart:~$ ot dns resolve thingsboard-edge.default.service.arpa.
DNS response for thingsboard-edge.default.service.arpa. - fdaf:e549:1751:1:1199:8c2b:a32e:38ee TTL: 7200
```

The firmware's own retry loop is the real acceptance signal. On the console
after a power-cycle:

```
<inf> ami_lwm2m: DNS-SD lookup attempt 1/10 (timeout=7000ms)...
<inf> lwm2m_discover: DNS-SD service resolved: coap://[fdaf:e549:1751:1:1199:8c2b:a32e:38ee]:5683
<inf> ami_lwm2m: DNS-SD resolved: coap://[fdaf:e549:1751:1:1199:8c2b:a32e:38ee]:5683
<inf> net_lwm2m_rd_client: RD Client started ...
```

The `LOG_WRN("DNS-SD discovery failed — caller should fall back to Kconfig")`
line disappearing is the negative confirmation.

### Layer 4b — the same proof with **no COM port**

`src/coap_diag.c` serves `/diag` on `[::]:5685` independently of LwM2M, and it
reports `reg_ok` = `lwm2m_diag_get_reg_success()`:

```bash
# from INSIDE WSL (it has the OMR route natively)
python3 tools/diag_get.py --local --addr <node-OMR>
# {"fw":"0.7.17-ami","role":"Child","uptime_s":142,"resets":3,"reg_ok":1, ...}
```

`reg_ok >= 1` is end-to-end proof: discovery resolved **and** ThingsBoard
answered the REGISTER. `reg_ok == 0` with a healthy layer 3 means the problem
moved downstream to TB (missing device, wrong profile, provisionType DISABLED
with no pre-created device) — not to discovery.

---

## 6. Once it works: undo the serverless bench hacks

`overlays/lab.conf:24-33` currently disables the registration watchdogs *because
there was no server*:

```conf
CONFIG_AMI_BOOT_REGISTER_DEADLINE_S=0
CONFIG_AMI_HW_WATCHDOG_BOOT_GRACE_HARD_S=3600
```

Those two lines are exactly what makes this bench unrepresentative. As soon as
layer 4b shows `reg_ok >= 1`, delete them (falling back to the `prj.conf`
production values) and rebuild:

```powershell
python tools\build_firmware.py --variant med --mesh lab
```

Otherwise you are still validating a serverless configuration with a server
sitting next to it.

---

## 7. Fallback — avahi + the OTBR Discovery Proxy

Use this **only** when `probe` reports `srp_client_cli False` (i.e.
`ot-ctl srp client state` answers `Error 35: InvalidCommand`, meaning otbr-agent
was built without `OT_SRP_CLIENT=ON`).

> ⚠ **Never run both paths at once.** The OTBR's *Advertising Proxy* already
> mirrors every SRP registration out to mDNS. Publishing a second
> `ThingsBoard-Edge` instance via avahi collides on the same link; avahi
> resolves it by renaming ours to `ThingsBoard-Edge #2`, which no longer matches
> `SRV_INSTANCE_LABEL` — and the resulting failure reads exactly like a firmware
> bug. Run `python tools\lab_tb\lab_tb_srp.py remove` before installing this.

```powershell
python tools\lab_tb\lab_tb_srp.py install-avahi
```

which, inside WSL:

1. writes `tools/lab_tb/avahi-lwm2m.service` →
   `/etc/avahi/services/lwm2m-thingsboard.service`:

   ```xml
   <service-group>
     <name replace-wildcards="no">ThingsBoard-Edge</name>
     <service protocol="any">
       <type>_lwm2m._udp</type>
       <port>5683</port>
       <host-name>thingsboard-edge.local</host-name>
     </service>
   </service-group>
   ```

2. appends the **host** record to `/etc/avahi/hosts` — the only way to make
   avahi publish an AAAA for a name it does not own, and the address must be the
   wpan0 OMR address, not eth0's:

   ```
   fdaf:e549:1751:1:1199:8c2b:a32e:38ee thingsboard-edge.local
   ```

3. `systemctl restart avahi-daemon` (service files are picked up on reload,
   `/etc/avahi/hosts` needs the restart).

Prerequisites and probes:

```bash
sudo apt-get install -y avahi-daemon avahi-utils      # if missing
ldd $(command -v otbr-agent) | grep -i avahi          # is avahi the mDNS backend?
avahi-browse -art | grep -i lwm2m                     # did avahi publish it?

# THE probe: does the Discovery Proxy bridge it into Thread DNS-SD?
ot-ctl dns browse _lwm2m._udp.default.service.arpa.
```

If that last command returns `Error 25: NotFound` / NXDOMAIN while
`avahi-browse` clearly shows the service, `otbr-agent` was built **without**
`OTBR_DNSSD_DISCOVERY_PROXY=ON`. There is no runtime switch — rebuild
`ot-br-posix` with that flag, or get `OT_SRP_CLIENT=ON` and use the primary
path. Verification afterwards is identical (`python tools\lab_tb\lab_tb_srp.py verify`).

---

## 8. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `publish` → "no off-mesh-local (OMR) address … refusing" | Border routing is not up, so no OMR prefix exists | `ot-ctl br state` must be `running`; check the backbone/infra interface the OTBR was started with, then `ot-ctl br omrprefix` |
| `srp client host name` → `Error 35: InvalidCommand` | otbr-agent built without `OT_SRP_CLIENT=ON` | section 7 (avahi fallback) |
| `srp client host state` stuck at `ToAdd` | autostart did not select the local SRP server | `ot-ctl srp client server` to see what it chose; `ot-ctl srp client start <addr> <port>` explicitly, or `--server` |
| Registry OK but `ot-ctl dns service` → NXDOMAIN | The DNS-SD server is answering from a different domain, or the query went to an off-mesh DNS server | pass the server explicitly: `ot-ctl dns service ThingsBoard-Edge _lwm2m._udp.default.service.arpa. <OMR-addr> 53` |
| **Node's** `ot dns config` shows `2001:4860:4860::8888` | The node's DNS client never auto-set its server from the SRP server (OT falls back to Google DNS, unroutable on the mesh) — usually the node's SRP client has not found the server yet | wait for attach to settle; confirm `ot srp client server` on the node; confirm `ot-ctl srp server state` is `running` on the BR |
| Everything resolves, node logs `DNS-SD service resolved`, but no REGISTER | Discovery is fine; the problem is downstream in TB | check the TB LwM2M bind line `Started endpoint at coap://[::]:5683`, the device + `LWM2M_CREDENTIALS` (`credentialsId == ami-esp32c6-3bb0`), and `provisionType` |
| Node resolved a `fdfe:…` / mesh-local address | Something published a mesh-local AAAA (auto host-address mode, or a hand-typed address) | re-run `publish` — it pins the OMR address only; never use `srp client host address auto` here |
| Record vanishes after a while | otbr-agent restarted (RCP renumbering, `wsl --shutdown`) — the registration is RAM-only | `python tools\lab_tb\lab_tb_srp.py install` |
| Two `ThingsBoard-Edge` entries / one named `ThingsBoard-Edge #2` | Both the SRP and the avahi path are active | `remove` one of them (section 7 warning) |
| Unit fails with `/bin/sh^M: bad interpreter` | CRLF line endings from the Windows checkout | re-install via `python tools\lab_tb\lab_tb_srp.py install` (it normalises), or `sed -i 's/\r$//' /usr/local/sbin/srp_publish_lwm2m.sh` |

---

## 9. Files

| Path | Role |
|---|---|
| `tools/lab_tb/lab_tb_srp.py` | Windows/WSL driver: `probe` / `address` / `publish` / `verify` / `remove` / `install` / `uninstall` / `install-avahi`. Stdlib only, `--json`, non-zero exit on failure. |
| `tools/lab_tb/srp_publish_lwm2m.sh` | The in-WSL engine (POSIX sh). Also runs standalone: `publish` / `verify` / `remove` / `address` / `daemon`. |
| `tools/lab_tb/otbr-srp-lwm2m.service` | systemd unit, `PartOf=otbr-agent.service`, re-asserts the record every 120 s. |
| `tools/lab_tb/avahi-lwm2m.service` | Fallback static avahi service definition (Discovery-Proxy path). |
| `src/lwm2m_discover.c` | The client side of the contract. Change a string here and you must change it in all four files above. |

### Where this sits in the bench bring-up

`tools/lab_tb/` is one package; discovery is step 2 of four:

| Step | Command | Owns |
|---|---|---|
| 1 | `./tools/lab_tb/lab_tb.ps1 -Action bootstrap` | the ThingsBoard stack (`docker-compose.yml`, LwM2M on 5683) |
| **2** | **`python tools\lab_tb\lab_tb_srp.py install`** | **this document — the DNS-SD record** |
| 3 | `python tools/lab_tb/lab_tb_provision.py` | models + `AMI_LwM2M_Node` profile + device `ami-esp32c6-3bb0` + NO_SEC credentials |
| 4 | `python tools/lab_tb/lab_tb_check.py --strict` | the whole-chain gate |

The DNS-SD contract strings live once, in `tools/lab_tb/lab_tb_common.py`
(`SRV_INSTANCE_LABEL` / `SRV_TYPE_DOMAIN` / `HOST_FQDN` / `LWM2M_PORT`);
`lab_tb_srp.py` imports them when available and falls back to identical
literals so it still runs under a bare interpreter during bring-up.
`lab_tb_check.py` check 3 verifies the record — `lab_tb_srp.py` is the
publisher that check was written to expect.

Order matters in one place: **publish the record (step 2) before power-cycling
the node**, and re-run step 2 after anything that restarts `otbr-agent` unless
the systemd unit is installed.

## 10. See also

- `docs/LAB_OTBR_BRINGUP.md` — bringing the bench Thread network up in the first place
- `docs/DYNAMIC_DISCOVERY.md` — the production chain and the 2026-06-04 incident
- `tools/edge_health.py:94-100` — the production assertion this bench now satisfies
- `tools/diag_get.py` — COM-port-free node state (`reg_ok`)
