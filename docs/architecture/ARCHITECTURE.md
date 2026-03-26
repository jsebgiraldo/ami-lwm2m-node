# AMI System Architecture — Smart Energy Metering

## Overview

Advanced Metering Infrastructure (AMI) system that connects industrial electricity
meters to a cloud IoT platform using a Thread 802.15.4 wireless mesh network and
the LwM2M protocol.

## Architecture Diagram

```
┌─────────────────────┐
│   ThingsBoard Cloud  │   Layer 4: Cloud Platform
│   192.168.1.159:80   │   - Centralized device management
│   (CE 4.2.1.1)       │   - Dashboards, alarms, analytics
└──────────┬───────────┘   - REST API for profile management
           │ gRPC:7070
           │ (bidirectional)
┌──────────▼───────────┐
│   ThingsBoard Edge    │   Layer 3: Edge Gateway
│   RPi4 (OpenWrt)      │   - Local rule engine processing
│   192.168.1.111:8090  │   - Local PostgreSQL persistence
│   LwM2M: 5683/udp    │   - Bidirectional sync with Cloud
│   Docker containers   │   - Integrated LwM2M transport (port 5683/udp)
└──────────┬───────────┘
           │ IPv6 mesh-local
           │ CoAP/LwM2M (NoSec)
┌──────────▼───────────┐
│   OTBR (Border Router)│   Layer 2: Border Router
│   RPi4 (OpenWrt)      │   - IPv6 Thread ↔ LAN bridge
│   192.168.1.111       │   - Native OpenWrt service
│   Thread Leader       │   - mesh-local: fdc6:63fd:328d:66df::/64
└──────────┬───────────┘
           │ IEEE 802.15.4
           │ Thread mesh (Ch25, PAN 0xABCD)
┌──────────▼───────────┐
│   XIAO ESP32-C6       │   Layer 1: Sensor Node (this repository)
│   Zephyr RTOS 4.2.0   │   - LwM2M client
│   OpenThread Router    │   - Half-duplex RS485 driver
│   ami-esp32c6-XXXX     │   - DLMS/COSEM parser
└──────────┬───────────┘
           │ RS485 (9600 8N1)
           │ DLMS/COSEM
┌──────────▼───────────┐
│   Microstar C2000     │   Layer 0: Electricity Meter
│   Single-phase Meter  │   - OBIS registers via DLMS
│   RS485 slave         │   - Voltage, current, power, energy
└───────────────────────┘
```

## System Layers

### Layer 0: Electricity Meter (Microstar C2000)
- **Communication**: RS485 half-duplex, 9600 baud, 8N1
- **Protocol**: DLMS/COSEM (IEC 62056)
- **Available data**: Voltage, current, active/reactive/apparent power,
  power factor, total active energy, frequency
- **Polling**: Every 30 seconds from the IoT node

### Layer 1: IoT Node (ESP32-C6 — this repository)
- **MCU**: Espressif ESP32-C6 (RISC-V, native 802.15.4 radio)
- **Board**: Seeed XIAO ESP32-C6
- **OS**: Zephyr RTOS 4.2.0
- **Radio**: IEEE 802.15.4 → Thread mesh → OpenThread Router
- **IoT Protocol**: LwM2M client (Eclipse Wakaama via Zephyr)
- **Exposed LwM2M Objects**:
  | Object ID | Name | Description |
  |-----------|------|-------------|
  | 0 | Security | Server URI, NoSec mode |
  | 1 | Server | Lifetime, binding |
  | 3 | Device | Manufacturer, model, serial, firmware |
  | 4 | ConnMon | Signal strength, link quality, router |
  | 5 | Firmware | OTA update support |
  | 10242 | PowerMeter | Single-phase meter (custom IPSO) |

### Layer 2: OTBR (OpenThread Border Router)
- **Hardware**: Raspberry Pi 4 with OpenWrt
- **Function**: Bridge between Thread (802.15.4) network and IPv6/IPv4 LAN
- **Thread network**: "AMI-Pilot-2025", Channel 25, PAN ID 0xABCD
- **Mesh-local prefix**: fdc6:63fd:328d:66df::/64
- **OTBR EID**: fdc6:63fd:328d:66df:6a54:12ef:8c67:bd1c
- **OTBR shares the same host** as ThingsBoard Edge (RPi4)

### Layer 3: ThingsBoard Edge
- **Image**: `thingsboard/tb-edge:4.2.1EDGE` (Docker, host networking)
- **LwM2M Server**: LwM2M transport built into TB Edge, port 5683/udp
- **HTTP API**: port 8090 (8080 occupied by OpenWrt dppd)
- **Database**: PostgreSQL 15 (container `tb-edge-postgres`)
- **Function**:
  - Receives LwM2M data from the node
  - Applies local rule engine processing
  - Persists telemetry in PostgreSQL
  - Bidirectionally syncs with Cloud via gRPC

### Layer 4: ThingsBoard Cloud
- **Host**: 192.168.1.159 (on-premise LAN)
- **Version**: ThingsBoard CE 4.2.1.1
- **API**: Port 80
- **gRPC**: Port 7070 (connection from Edge)
- **Function**:
  - Centralized device and profile management
  - Visualization dashboards
  - LwM2M profile changes must be made here (Cloud REST API)
    so they propagate to Edge without being reverted

## LwM2M Protocol — Details

### Registration and Observation
1. Node boots → joins Thread mesh (~11s)
2. Sends LwM2M Register to `coap://[OTBR_EID]:5683` (~17s total)
3. TB Edge accepts registration → marks device ACTIVE
4. Edge configures Observe on resources from the `C2000_Monofasico_v2` profile
5. Node sends Notify periodically according to configured pmin/pmax

### Observe Strategy (ObserveStrategy: SINGLE)
**IMPORTANT**: Do NOT use COMPOSITE_BY_OBJECT — causes empty Observe in Zephyr.

| Group | Resources | pmin | pmax | Use |
|-------|-----------|------|------|-----|
| Operational Telemetry | VoltageR, CurrentR, ActivePowerR, TotalEnergy | 15s | 30s | Real-time monitoring |
| Load Characterization | ReactivePower, ApparentPower, PowerFactor | 60s | 300s | Power quality analysis |
| Network & System | Frequency, Device info, Connectivity, Firmware | 60s | 300s | Diagnostics |

### Object Version Format (defaultObjectIDVer)
The LwM2M profile must use the **"V"** format (`"1.2"`, `"1.0"`, etc.)  
**NEVER** the "VER" format (`"3_1.2"`, `"10242_1.0"`) — causes mismatch during registration.

## RS485 / DLMS Communication

### Protocol
- RS485 half-duplex with DE/RE control via GPIO
- DLMS/COSEM (IEC 62056) over HDLC
- Slave address: 1 (meter), Client: 0x10

### Complete Reference Table: OBIS → LwM2M → Telemetry

The node reads the meter every **30 seconds** via RS485/DLMS. Values are stored
in LwM2M object **10242** (PowerMeter, custom IPSO) and the TB Edge server
observes them in two frequency groups.

#### Group 1 — Operational Telemetry (pmin=15s, pmax=30s)

| OBIS Code | Quantity | Unit | LwM2M Path | RID | Telemetry Key | Type |
|-----------|----------|------|------------|-----|---------------|------|
| 1-1:32.7.0 | Voltage phase R | V | /10242/0/4 | 4 | `voltage` | Float |
| 1-1:31.7.0 | Current phase R | A | /10242/0/5 | 5 | `current` | Float |
| 1-1:21.7.0 | Active power R | kW | /10242/0/6 | 6 | `activePower` | Float |
| 1-1:1.8.0 | Total active energy | kWh | /10242/0/41 | 41 | `activeEnergy` | Float |

#### Group 2 — Load Characterization (pmin=60s, pmax=300s)

| OBIS Code | Quantity | Unit | LwM2M Path | RID | Telemetry Key | Type |
|-----------|----------|------|------------|-----|---------------|------|
| 1-1:23.7.0 | Reactive power R | kvar | /10242/0/7 | 7 | `reactivePower` | Float |
| 1-1:29.7.0 | Apparent power R | kVA | /10242/0/10 | 10 | `apparentPower` | Float |
| 1-1:33.7.0 | Power factor R | — | /10242/0/11 | 11 | `powerFactor` | Float |
| 1-1:1.7.0 | Total active power | kW | /10242/0/34 | 34 | `totalActivePower` | Float |
| 1-1:3.7.0 | Total reactive power | kvar | /10242/0/35 | 35 | `totalReactivePower` | Float |
| 1-1:9.7.0 | Total apparent power | kVA | /10242/0/38 | 38 | `totalApparentPower` | Float |
| 1-1:13.7.0 | Total power factor | — | /10242/0/39 | 39 | `totalPowerFactor` | Float |
| 1-1:1.8.0 | Reactive energy | kvarh | /10242/0/42 | 42 | `reactiveEnergy` | Float |
| 1-1:9.8.0 | Apparent energy | kVAh | /10242/0/45 | 45 | `apparentEnergy` | Float |
| 1-1:14.7.0 | Frequency | Hz | /10242/0/49 | 49 | `frequency` | Float |

#### Additional observed resources (Group 2 — pmin=60s, pmax=300s)

| Object | LwM2M Path | RID | Telemetry Key | Description |
|--------|------------|-----|---------------|-------------|
| ConnMon (4) | /4/0/2 | 2 | `radioSignalStrength` | 802.15.4 radio RSSI |
| ConnMon (4) | /4/0/3 | 3 | `linkQuality` | Thread link quality |
| Firmware (5) | /5/0/3 | 3 | `fwState` | OTA update state |
| Firmware (5) | /5/0/5 | 5 | `fwUpdateResult` | Last OTA result |

#### Attributes (read once at registration, no observe)

| Object | LwM2M Path | RID | Attribute Key | Description |
|--------|------------|-----|---------------|-------------|
| Device (3) | /3/0/0 | 0 | `manufacturer` | Tesis-AMI |
| Device (3) | /3/0/1 | 1 | `modelNumber` | XIAO-ESP32-C6 |
| Device (3) | /3/0/2 | 2 | `serialNumber` | AMI-001 |

#### Timing Summary

| Stage | Interval | Notes |
|-------|----------|-------|
| DLMS read (RS485) | 30s | Meter polling via HDLC/COSEM |
| Observe Group 1 | pmin=15s, pmax=30s | Voltage, current, active power, energy |
| Observe Group 2 | pmin=60s, pmax=300s | Power quality, totals, frequency, radio, firmware |
| LwM2M Registration Update | ~270s | Lifetime=300s, renews 30s before expiry |
| LwM2M Lifetime | 300s | If not renewed, server marks device INACTIVE |

## Deployment — Docker Compose (Edge)

See [config_backups/docker-compose.yml](../config_backups/docker-compose.yml) for
the full file. Critical environment variables:

```yaml
environment:
  CLOUD_ROUTING_KEY: "1lg060jcvfp2tylc78mt"
  CLOUD_ROUTING_SECRET: "o1bcx4arcldnkjirru8n"
  CLOUD_RPC_HOST: "192.168.1.159"     # Direct LAN (NOT Tailscale)
  LWM2M_BIND_PORT: "5683"
  LWM2M_SECURITY_BIND_PORT: "5684"
  COAP_BIND_PORT: "5690"              # Different from LwM2M to avoid conflict
  COAP_ENABLED: "false"               # CoAP disabled (LwM2M only)
  SPRING_DATASOURCE_USERNAME: "tb_edge"
  SPRING_DATASOURCE_PASSWORD: "tb_edge_pwd"
```

## Credentials

| Component | Username | Password | Notes |
|-----------|----------|----------|-------|
| TB Edge Web | tenant@thingsboard.org | tenant | HTTP 8090 |
| TB Cloud API | tenant@thingsboard.org | tenant | HTTP 80 |
| PostgreSQL Edge | tb_edge | tb_edge_pwd | DB: tb_edge |
| RPi4 SSH | root | (key-based) | 192.168.1.111 |

## Thread Network

| Parameter | Value |
|-----------|-------|
| Network Name | AMI-Pilot-2025 |
| Channel | 25 |
| PAN ID | 0xABCD |
| Network Key | 00:11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff |
| Extended PAN ID | 12:34:56:78:90:ab:cd:ef |
| Mesh-local prefix | fdc6:63fd:328d:66df::/64 |
