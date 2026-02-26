# DLMS/COSEM RS485 Meter Integration — Architecture Reference

## Overview

This document describes the RS485 physical layer integration for reading **real**
metering data from a **Microstar DLMS/COSEM** smart meter, transported over
**Thread mesh** via **LwM2M** to ThingsBoard Edge/Cloud.

```
┌─────────────┐   RS485    ┌──────────────┐  Thread/6LoWPAN  ┌──────────┐
│  Microstar  │──(A/B/GND)─│  XIAO ESP32  │────802.15.4────▶│   OTBR   │
│  DLMS Meter │  9600 8N1  │  C6 + RS485  │                 │  (RPi4)  │
│  3-Phase    │            │  Expansion   │                 │  Leshan  │
└─────────────┘            └──────────────┘                 └────┬─────┘
                                                                 │ CoAP
                                                            ┌────▼─────┐
                                                            │   TB     │
                                                            │  Edge    │
                                                            └────┬─────┘
                                                                 │ MQTT
                                                            ┌────▼─────┐
                                                            │ TB Cloud │
                                                            └──────────┘
```

## Hardware Connections

### Seeed Studio XIAO RS485 Expansion Board → XIAO ESP32-C6

| Board Pin | XIAO Connector | ESP32-C6 GPIO | Function              |
|-----------|----------------|---------------|-----------------------|
| D4        | Pin 4          | **GPIO22**    | UART1 RX (from meter) |
| D5        | Pin 5          | **GPIO23**    | UART1 TX (to meter)   |
| D2        | Pin 2          | **GPIO2**     | DE/RE control          |

> **IMPORTANT**: The XIAO ESP32-C6 pin mapping differs from ESP32-C3!
> GPIO22/23 are shared with I2C0, which is disabled in the devicetree overlay.

### RS485 Serial Parameters

| Parameter  | Value          |
|------------|----------------|
| Baud rate  | 9600           |
| Data bits  | 8              |
| Parity     | None           |
| Stop bits  | 1              |
| Duplex     | Half-duplex    |
| DE/RE      | GPIO2 (HIGH=TX, LOW=RX) |

## Software Architecture

### Layer Stack

```
┌─────────────────────────────────────────────┐
│              main.c  (30s loop)             │
│   update_sensors() → meter_poll()           │
│   meter_push_to_lwm2m() → lwm2m_set_f64()  │
├─────────────────────────────────────────────┤
│           dlms_meter.c/h                    │
│   OBIS→LwM2M mapping (27 codes)            │
│   2-phase read: scalers + values            │
│   connect → read_all → disconnect           │
├─────────────────────────────────────────────┤
│           dlms_cosem.c/h                    │
│   AARQ (LN + LLS auth)                     │
│   GET.request / GET.response                │
│   Data type decoding (15+ types)            │
├─────────────────────────────────────────────┤
│           dlms_hdlc.c/h                     │
│   SNRM / UA / DISC / I-frames              │
│   CRC-16/CCITT (0x8408)                     │
│   HCS + FCS verification                    │
├─────────────────────────────────────────────┤
│           rs485_uart.c/h                    │
│   Half-duplex UART with DE pin              │
│   ISR ring buffer (512 bytes)               │
│   Semaphore-based RX notification           │
├─────────────────────────────────────────────┤
│     UART1 @ 9600 baud (GPIO22/23)           │
│     GPIO2 (DE/RE control)                   │
│     Devicetree overlay                      │
└─────────────────────────────────────────────┘
```

### Protocol Flow (per poll cycle)

```
ESP32-C6                              Microstar Meter
    |                                       |
    |--- SNRM (0x93) ---------------------->|
    |<-- UA   (0x73) -----------------------|
    |                                       |
    |--- AARQ (LN, LLS pw="22222222") ---->|
    |<-- AARE (success) -------------------|
    |                                       |
    |--- GET.req (1-1:32.7.0 attr=2) ----->|  Voltage R
    |<-- GET.resp (float64: 121.3) --------|
    |                                       |
    |--- GET.req (1-1:31.7.0 attr=2) ----->|  Current R
    |<-- GET.resp (float64: 5.2) ----------|
    |           ... 25 more reads ...       |
    |                                       |
    |--- RLRQ ----------------------------→|
    |<-- RLRE ----------------------------- |
    |                                       |
    |--- DISC (0x53) ---------------------->|
    |<-- UA   (0x73) -----------------------|
    |                                       |
```

### DLMS/COSEM Parameters

| Parameter              | Value                          |
|------------------------|--------------------------------|
| Client SAP             | 16 → HDLC addr 0x21           |
| Server Logical Device  | 1 → HDLC addr 0x03            |
| Application Context    | LN referencing (no ciphering)  |
| Authentication         | LLS (Level 1)                  |
| Password               | "22222222"                     |
| Max PDU size           | 128 bytes                      |
| HDLC max info TX/RX    | 128 bytes                      |
| HDLC window TX/RX      | 1                              |

## OBIS Code → LwM2M Object 10242 Mapping

### Phase R (Line 1)

| OBIS Code    | Description      | LwM2M Resource        | RID | Unit |
|--------------|------------------|-----------------------|-----|------|
| 1-1:32.7.0   | Voltage          | PM_TENSION_R          | 4   | V    |
| 1-1:31.7.0   | Current          | PM_CURRENT_R          | 5   | A    |
| 1-1:21.7.0   | Active Power     | PM_ACTIVE_POWER_R     | 6   | kW   |
| 1-1:23.7.0   | Reactive Power   | PM_REACTIVE_POWER_R   | 7   | kvar |
| 1-1:29.7.0   | Apparent Power   | PM_APPARENT_POWER_R   | 8   | kVA  |
| 1-1:33.7.0   | Power Factor     | PM_POWER_FACTOR_R     | 9   | —    |

### Phase S (Line 2)

| OBIS Code    | Description      | LwM2M Resource        | RID | Unit |
|--------------|------------------|-----------------------|-----|------|
| 1-1:52.7.0   | Voltage          | PM_TENSION_S          | 14  | V    |
| 1-1:51.7.0   | Current          | PM_CURRENT_S          | 15  | A    |
| 1-1:41.7.0   | Active Power     | PM_ACTIVE_POWER_S     | 16  | kW   |
| 1-1:43.7.0   | Reactive Power   | PM_REACTIVE_POWER_S   | 17  | kvar |
| 1-1:49.7.0   | Apparent Power   | PM_APPARENT_POWER_S   | 18  | kVA  |
| 1-1:53.7.0   | Power Factor     | PM_POWER_FACTOR_S     | 19  | —    |

### Phase T (Line 3)

| OBIS Code    | Description      | LwM2M Resource        | RID | Unit |
|--------------|------------------|-----------------------|-----|------|
| 1-1:72.7.0   | Voltage          | PM_TENSION_T          | 24  | V    |
| 1-1:71.7.0   | Current          | PM_CURRENT_T          | 25  | A    |
| 1-1:61.7.0   | Active Power     | PM_ACTIVE_POWER_T     | 26  | kW   |
| 1-1:63.7.0   | Reactive Power   | PM_REACTIVE_POWER_T   | 27  | kvar |
| 1-1:69.7.0   | Apparent Power   | PM_APPARENT_POWER_T   | 28  | kVA  |
| 1-1:73.7.0   | Power Factor     | PM_POWER_FACTOR_T     | 29  | —    |

### 3-Phase Totals

| OBIS Code    | Description        | LwM2M Resource          | RID | Unit |
|--------------|--------------------|-------------------------|-----|------|
| 1-1:1.7.0    | Active Power       | PM_3P_ACTIVE_POWER      | 34  | kW   |
| 1-1:3.7.0    | Reactive Power     | PM_3P_REACTIVE_POWER    | 35  | kvar |
| 1-1:9.7.0    | Apparent Power     | PM_3P_APPARENT_POWER    | 38  | kVA  |
| 1-1:13.7.0   | Power Factor       | PM_3P_POWER_FACTOR      | 39  | —    |

### Energy Accumulators

| OBIS Code    | Description        | LwM2M Resource          | RID | Unit |
|--------------|--------------------|-------------------------|-----|------|
| 1-1:1.8.0    | Active Energy      | PM_ACTIVE_ENERGY        | 41  | kWh  |
| 1-1:3.8.0    | Reactive Energy    | PM_REACTIVE_ENERGY      | 42  | kvarh|
| 1-1:9.8.0    | Apparent Energy    | PM_APPARENT_ENERGY      | 45  | kVAh |

### Other

| OBIS Code    | Description        | LwM2M Resource          | RID | Unit |
|--------------|--------------------|-------------------------|-----|------|
| 1-1:14.7.0   | Frequency          | PM_FREQUENCY            | 49  | Hz   |
| 1-1:91.7.0   | Neutral Current    | PM_NEUTRAL_CURRENT      | 50  | A    |

**Total: 27 OBIS codes → 27 LwM2M resources mapped**

## Scaler Handling

DLMS registers store values as integers with a **scaler** attribute:

```
real_value = raw_value × 10^scaler
```

The meter reader uses a 2-phase strategy:
1. **Phase 1** (once per session): Read `scaler_unit` (attribute 3) for each
   OBIS code and cache the multiplier `10^scaler`.
2. **Phase 2** (every poll): Read the raw value (attribute 2) and multiply
   by the cached multiplier.

If scaler reading fails, a multiplier of 1.0 is assumed.

## Fallback Mode

When the physical meter is unavailable (disconnected, communication error),
the firmware falls back to **simulated** 3-phase data:

- Voltage: 118–124 V per phase
- Current: 3.0–7.0 A per phase
- Power Factor: 0.85–0.95
- Frequency: 59.9–60.1 Hz
- Power: Calculated from V × I × PF

The log output distinguishes real vs. fallback data:
- Real:     `INF: Meter poll OK: V=121.3/119.8/120.5 ...`
- Fallback: `WRN: FALLBACK 3P: R=120.3V/5.2A ...`

## Files Created

| File                                    | Purpose                          |
|-----------------------------------------|----------------------------------|
| `boards/xiao_esp32c6_hpcore.overlay`    | DT overlay: UART1, DE GPIO       |
| `src/rs485_uart.c/h`                    | Half-duplex RS485 UART driver    |
| `src/dlms_hdlc.c/h`                     | HDLC framing (IEC 62056-46)     |
| `src/dlms_cosem.c/h`                    | COSEM application layer           |
| `src/dlms_meter.c/h`                    | Meter reader + OBIS→LwM2M map   |
| `docs/dlms_rs485_architecture.md`       | This document                     |

## Build

```bash
west build -p always -b xiao_esp32c6/esp32c6/hpcore
west flash
```

## Version History

| Version | Date       | Changes                                |
|---------|------------|----------------------------------------|
| 0.5.0   | 2025-07   | RS485/DLMS physical layer integration  |
| 0.4.0   | 2025-06   | Thread diagnostics, dashboard, E2E     |
| 0.3.0   | 2025-05   | LwM2M Object 10242 (3-Phase Meter)     |
