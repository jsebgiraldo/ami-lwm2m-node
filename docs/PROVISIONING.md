# AMI Node — Factory Provisioning Guide

> How to register a new (factory-fresh) node with ThingsBoard Edge and Cloud.

---

## System Context

```
[Meter]──RS485──[XIAO ESP32-C6]──Thread 802.15.4──[OTBR+TB Edge RPi4]──gRPC──[TB Cloud]
                 (this node)                        192.168.1.111:5683/udp     192.168.1.159
```

When the node boots, it sends an **LwM2M Registration** to the Edge server at
`coap://[fdc6:63fd:328d:66df:6a54:12ef:8c67:bd1c]:5683`.  
**If the device does not exist in TB Edge, the connection is rejected** (or ignored) and
the node will not send any data.

Therefore, **before deploying any new node**, it must be registered in ThingsBoard.

---

## Quickstart (WROOM C6, UART nativo, modo DEMO)

Use this flow to standardize bring-up of every new node without a physical meter.

1. Build firmware for WROOM/DevKitC:
```powershell
Set-Location "C:\Users\User\Documents\FW\ESP32C6-XIAO\DLMS-COSEM"
$ami = "C:\Users\User\Documents\UNAL\ami-lwm2m-node"
west build -p always -b esp32c6_devkitc/esp32c6/hpcore $ami
```
2. Flash over native UART:
```powershell
west flash --esp-device COM7 --esp-baud-rate 460800
```
3. Open serial and capture endpoint printed at boot:
```powershell
& "C:\Program Files\PuTTY\plink.exe" -serial COM7 -sercfg 115200,8,n,1,N
```
4. Provision the endpoint in TB Edge:
```powershell
Set-Location "C:\Users\User\Documents\UNAL\ami-lwm2m-node"
python .\tools\provision_node.py --endpoint ami-esp32c6-XXXX --host 192.168.1.111 --port 8090
```
5. Validate connectivity from node shell:
```text
ami log lwm2m
ami status
ami test lwm2m
```

In demo mode (`CONFIG_AMI_DEMO_MODE=y`), meter values are simulated and pushed to
Object 10242 so telemetry flow can be validated end-to-end.

---

## Concept: How is a node identified?

The node builds its LwM2M identity at boot using the `build_endpoint_name()` function
in `src/main.c`:

```c
// src/main.c  lines 341-353
snprintf(endpoint_name, sizeof(endpoint_name),
         "ami-esp32c6-%02x%02x",
         link->addr[link->len - 2],   // MAC byte -2
         link->addr[link->len - 1]);  // MAC byte -1
```

### Real example
| Field | Value |
|-------|-------|
| Full MAC (HW label) | `98:A3:16:61:24:34` |
| Last 2 bytes | `0x24`, `0x34` |
| **LwM2M Endpoint** | **`ami-esp32c6-2434`** |

> The MAC can be read:
> - From the **physical label** printed on the module (bottom of the XIAO)
> - From the **serial monitor** at boot: `LOG_INF("Endpoint: %s", endpoint_name)`
> - Using `esptool.py flash_id --port COMx` before flashing the firmware

---

## Device Profile Contents

The node uses the **`C2000_Monofasico_v2`** profile (TB Edge UUID `b6d55c90-12db-11f1-b535-433a231637c4`).

This profile defines:
- **Observe** (16 resources): voltage, current, powers, energies, frequency, RSSI, LQI
- **Attributes** (3): manufacturer, modelNumber, serialNumber — read once at registration
- **Telemetry** (13): the same resources, stored as time-series in PostgreSQL
- **Transport**: LwM2M NoSec (no encryption, no bootstrap)

The Edge automatically syncs this profile with TB Cloud via gRPC — no manual replication is needed.

---

## Provisioning Strategies

### Option A — REST Script (recommended for pilots ≤ 100 nodes)

**When to use**: full control, audit trail of registered nodes.

```
                    ┌────────────────────────────────────┐
Operator            │  python provision_node.py          │
  ──[MAC label]──►  │  --mac 98:a3:16:61:24:34           │
                    └──────────────┬─────────────────────┘
                                   │ REST POST /api/device
                                   ▼
                    ┌────────────────────────────────────┐
                    │  ThingsBoard Edge :8090            │
                    │  Creates device "ami-esp32c6-2434" │
                    │  Profile: C2000_Monofasico_v2      │
                    │  Creds: LWM2M NoSec                │
                    └──────────────┬─────────────────────┘
                                   │ automatic gRPC sync
                                   ▼
                    ┌────────────────────────────────────┐
                    │  ThingsBoard Cloud :80             │
                    │  Device replicated                 │
                    └────────────────────────────────────┘

When the node boots:
  LwM2M Register → Edge accepts → ACTIVE → data flows
```

### Option B — Auto-provisioning (for large-scale deployments)

ThingsBoard supports `ALLOW_CREATE_NEW_DEVICES`, where **any new endpoint that
registers is auto-created** under the profile with this option enabled.

> ⚠️  With LwM2M NoSec without Bootstrap, this is enabled in ThingsBoard 4.x by setting
> `provisionType: ALLOW_CREATE_NEW_DEVICES` + a `provisionDeviceKey` in the profile.
> The node does NOT need to know the key — it only needs the correct endpoint.
> **CAVEAT**: any device with any endpoint can auto-register if it knows the server →
> only use on controlled networks (Thread mesh qualifies).

Enable via REST (see "Advanced Administration" section).

### Option C — Manual from Web UI

For a single node: `TB Edge UI` → Entities → Devices → `+` → name=`ami-esp32c6-XXXX`,
profile=`C2000_Monofasico_v2`. Then go to Credentials and select LwM2M type.

---

## Step-by-step — Method A (Script)

### Prerequisites

```bash
pip install requests
```

The script is at `tools/provision_node.py`.

### Step 1 — Get the MAC of the new node

**From the physical label** (recommended in manufacturing):
The XIAO ESP32-C6 module has the MAC printed on the bottom (`Wi-Fi/BT addr`).
The Thread MAC uses the same last 2 bytes.

**From the serial monitor** (if firmware is already flashed):
```
Connect with minicom/PuTTY at 115200 baud. Output at boot:
  *** AMI MAIN ENTRY ***
  *** Firmware: v0.16.0 ***
  ...
  [INF] Thread attached! Role=2 after 8s
  [INF] Endpoint: ami-esp32c6-2434      ← use this value
```

**From esptool** (before flashing):
```bash
python -m esptool --port COM11 flash_id
# Displays: MAC: 98:a3:16:61:24:34
```

### Step 2 — Run the script

```bash
# From the repo root directory
python tools/provision_node.py --mac 98:a3:16:61:24:34
```

Expected output:
```
============================================================
  AMI Node Provisioner
  Target : http://192.168.1.111:8090
  Profile: C2000_Monofasico_v2
  Nodes  : 1
  Action : PROVISION
============================================================
  [OK] Authenticated as tenant@thingsboard.org

──────────────────────────────────────────────────────────
  Endpoint : ami-esp32c6-2434
  Profile  : C2000_Monofasico_v2  (b6d55c90...)
  [OK] Device created: cc9da070-135b-11f1-80f9-cdb955f2c365
  [OK] Credentials set: LWM2M_CREDENTIALS / NO_SEC / endpoint=ami-esp32c6-2434

============================================================
  SUMMARY: 1 total | 1 created | 0 already existed | 0 errors
============================================================
```

### Step 3 — Flash the firmware to the node

If not done yet:
```powershell
.\build_flash.ps1 -Flash -Port COM11
```

The firmware is the same for **all nodes** — no per-device parameterization exists.
The only differences between nodes are:
- Endpoint (derived from MAC, automatic)
- IPv6 address derived from the Thread radio EUI-64 (automatic)

### Step 4 — Power on and verify

The node will take ~17 seconds to:
1. Join the Thread network (credentials hardcoded in `prj.conf`)
2. Obtain a mesh-local IPv6 address
3. Register with LwM2M on the Edge

Verify with the script:
```bash
python tools/provision_node.py --mac 98:a3:16:61:24:34 --verify
```

Expected output:
```
──────────────────────────────────────────────────────────
  Endpoint : ami-esp32c6-2434
  Device ID   : cc9da070-135b-11f1-80f9-cdb955f2c365
  Active      : True
  Profile     : C2000_Monofasico_v2
  Cred type   : LWM2M_CREDENTIALS
  Cred ID     : ami-esp32c6-2434
  Telemetry   : voltage = 124.84
  Telemetry   : current = 0.0
  Telemetry   : activePower = 0.0
```

If `Active: False` after 30 seconds, see the Troubleshooting section.

---

## Batch Provisioning (CSV)

For N nodes at the same time:

1. Create file `nodes_batch.csv`:
```csv
mac,location,installed_by
98:a3:16:61:24:34,Apt-101,JSG
AA:BB:CC:DD:EE:FF,Apt-102,JSG
11:22:33:44:55:66,Apt-103,JSG
```

2. Run:
```bash
python tools/provision_node.py --csv nodes_batch.csv
```

The script is **idempotent** — if the device already exists, it is skipped (`[SKIP]`).

---

## ThingsBoard Device States

| State | Description | Meaning |
|-------|-------------|--------|
| `active: false` | Not connected | Device created but no active LwM2M registration |
| `active: true` | Connected | LwM2M registration valid (lifetime=300s, renews every ~270s) |
| Not shown | Not provisioned | Node cannot connect → run provision_node.py first |

---

## Factory-to-Production Checklist

```
FACTORY                          FIELD / LAB
─────────────────────────────    ────────────────────────────────────────────────
1. Solder/assemble XIAO +        4. Connect RS485 adapter to meter
   RS485 expansion board            (A/B/GND, 9600 8N1 half-duplex)

2. Read module MAC               5. Run provision_node.py --mac XX:XX...
   (label or esptool)               → Registers in TB Edge/Cloud

3. Flash firmware                6. Power on the node
   build_flash.ps1 -Flash           → OpenThread join (~8s)
   -Port COMx                       → LwM2M register (~17s)
                                    → TB Edge marks ACTIVE

                                 7. Verify data in Edge / telemetry
                                    or: provision_node.py --verify

                                 8. (Optional) Assign to customer/asset in TB Cloud
```

---

## Parameters that distinguish one node from another

> **Everything is in the firmware binary compiled once** except:

| Parameter | Where it is determined | Note |
|-----------|----------------------|------|
| LwM2M Endpoint | At runtime, from MAC | Unique per hardware, automatic |
| Thread IPv6 address | At runtime, from radio EUI-64 | Unique per hardware, automatic |
| Thread Network Key | Hardcoded in `prj.conf` | **Same for all nodes in the pilot!** |
| LwM2M Server URI | Hardcoded in `prj.conf` | Same for all |
| Data profile | Configured in TB Edge | Applies equally to all |

> For multi-network or multi-customer production: parameterize the network key and server URI
> via NVS (flash settings) or OTA config push. See "Roadmap" section.

---

## LwM2M Profile — Quick Reference

### Observed resources

| Path | Telemetry key | Description |
|------|---------------|-------------|
| `/10242_1.0/0/4` | `voltage` | Phase R voltage (V) |
| `/10242_1.0/0/5` | `current` | Phase R current (A) |
| `/10242_1.0/0/6` | `activePower` | Phase R active power (kW) |
| `/10242_1.0/0/7` | `reactivePower` | Phase R reactive power (kvar) |
| `/10242_1.0/0/10` | `apparentPower` | Phase R apparent power (kVA) |
| `/10242_1.0/0/11` | `powerFactor` | Phase R power factor |
| `/10242_1.0/0/39` | `totalPowerFactor` | Total power factor |
| `/10242_1.0/0/41` | `activeEnergy` | Total active energy (kWh) |
| `/10242_1.0/0/42` | `reactiveEnergy` | Reactive energy (kvarh) |
| `/10242_1.0/0/45` | `apparentEnergy` | Apparent energy (kVAh) |
| `/10242_1.0/0/49` | `frequency` | Frequency (Hz) |
| `/4_1.3/0/2` | `radioSignalStrength` | 802.15.4 RSSI (dBm) |
| `/4_1.3/0/3` | `linkQuality` | Thread link LQI |

### Attributes (no observe, read once)

| Path | Key | Description |
|------|-----|-------------|
| `/3_1.2/0/0` | `manufacturer` | "Tesis-AMI" |
| `/3_1.2/0/1` | `modelNumber` | "XIAO-ESP32-C6" |
| `/3_1.2/0/2` | `serialNumber` | "AMI-001" |

---

## Advanced Administration

### Enable auto-provisioning (Option B)

To allow any AMI node to self-register without manual pre-provisioning:

```bash
# 1. Read the current profile
curl -s -H "Authorization: Bearer $TOKEN" \
  http://192.168.1.111:8090/api/deviceProfile/b6d55c90-12db-11f1-b535-433a231637c4 \
  > /tmp/profile.json

# 2. Edit: change provisionType and add provisionDeviceKey
#    "provisionType": "ALLOW_CREATE_NEW_DEVICES",
#    "provisionDeviceKey": "ami-lwm2m-provision-key-2025",

# 3. Update via PUT
curl -s -X PUT -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d @/tmp/profile_updated.json \
  http://192.168.1.111:8090/api/deviceProfile
```

> With `ALLOW_CREATE_NEW_DEVICES`, the node is auto-created on its first LwM2M
> registration. The device inherits the profile configured as DEFAULT (or the first one
> with provisioning enabled). To ensure the correct profile, mark
> `C2000_Monofasico_v2` as DEFAULT (`"default": true`).

### Export the list of provisioned nodes

```bash
python tools/provision_node.py --csv - --verify  # Future: --list all
```

For now, via API:
```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://192.168.1.111:8090/api/tenant/deviceInfos?pageSize=100&page=0" \
  | python -c "import sys,json; d=json.load(sys.stdin); [print(x['name'],x.get('active')) for x in d['data']]"
```

### Delete a node from the system (retirement or replacement)

```bash
python tools/provision_node.py --mac 98:a3:16:61:24:34 --delete
```

---

## Troubleshooting

| Symptom | Probable cause | Solution |
|---------|---------------|----------|
| `active: false` after 60s | Node did not join Thread | Check channel/PAN/NetworkKey in prj.conf |
| `active: false` + Thread OK | LwM2M registration failed | Check endpoint in TB; review node serial logs |
| Endpoint visible but no data | Profile misconfigured | Check profileData → observeAttr in TB |
| `Timeout` when provisioning | Edge not reachable | Check Docker: `docker ps` on RPi4 |
| `Profile not found` | Profile deleted or renamed | Recreate from `docs/config_backups/c2000_monophase_profile.json` |
| Numeric data = 0 | RS485 meter not connected | Check A/B/GND wiring and HDLC address |

### View node logs in real time

```powershell
# Serial monitor (115200, COM11)
python -m serial.tools.miniterm COM11 115200
```

Normal boot sequence:
```
*** AMI MAIN ENTRY ***
*** Firmware: v0.16.0 ***
[INF] Waiting for Thread network...
[INF] Thread attached! Role=2 after 8s
[INF] Extra 5s wait for IPv6 addresses...
[INF] Endpoint: ami-esp32c6-2434
[INF] LwM2M objects configured
[INF] Server: coap://[fdc6:63fd:328d:66df:6a54:12ef:8c67:bd1c]:5683
[INF] LwM2M RD client started
[INF] LwM2M client registered (session 0x...)
[INF] DLMS meter poll OK: V=124.8 I=0.00 P=0.0W
```

### Check Edge status

```bash
# SSH to RPi4
ssh root@192.168.1.111
docker ps  # tb-edge and tb-edge-postgres must be UP
docker logs tb-edge 2>&1 | tail -50
```

---

## Roadmap — For multi-customer production

When the pilot scales to multiple Thread networks (different buildings/customers):

1. **Parameterize Thread Network Key per installation**: Use NVS in flash to
   store/overwrite the Thread dataset without recompiling the firmware.

2. **Parameterize LwM2M Server URI**: The Edge server may change per deployment;
   read URI from NVS instead of `prj.conf`.

3. **Thread commissioning mechanism**: Instead of a universal network key,
   use Thread Commissioner (otbr-agent + ot-ctl) to commission each node
   individually with a temporary credential.

4. **TB Cloud multi-tenant**: Each customer has their own tenant in TB Cloud;
   the provisioning script should accept `--tenant` as an argument.

5. **OTA (Object 5)**: The firmware already supports Object 5 (Firmware Update). Upload
   images to TB OTA Package and push from the device profile.
