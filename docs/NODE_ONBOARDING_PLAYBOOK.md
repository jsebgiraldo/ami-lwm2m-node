# AMI Node Onboarding Playbook

> Standard end-to-end process for bringing a new ESP32-C6 node into the AMI Thread + LwM2M + ThingsBoard system.

---

## Goal

For every new node, complete the same lifecycle:

1. Detect the board and serial/JTAG interface on Windows
2. Read the MAC address and derive the LwM2M endpoint
3. Build the correct firmware target
4. Flash the node
5. Provision the endpoint in ThingsBoard Edge
6. Verify Thread join, LwM2M registration, and telemetry
7. Confirm the endpoint is active in Edge and receiving telemetry

This document is the operator playbook for XIAO ESP32-C6, ESP32-C6 WROOM/DevKitC, and ESP32-C6 Super Mini.

---

## Board Matrix

| Board | Typical USB interface | Zephyr target | Flash method | Notes |
|------|------|------|------|------|
| XIAO ESP32-C6 | Native USB Serial/JTAG | `xiao_esp32c6/esp32c6/hpcore` | JTAG (`flash_jtag.py`) preferred | Best when using native USB/JTAG and RS485 expansion board |
| ESP32-C6 WROOM / DevKitC | External USB-UART bridge | `esp32c6_devkitc/esp32c6/hpcore` | `west flash --esp-device COMx` | Console goes over UART0/bridge |
| ESP32-C6 Super Mini | Native USB Serial/JTAG | `xiao_esp32c6/esp32c6/hpcore` | `west flash --esp-device COMx` first, JTAG fallback | In DEMO mode this reuses the native USB target cleanly because RS485 is not required |

Why the Super Mini currently reuses the XIAO target:

- It enumerates as native ESP USB Serial/JTAG (`VID_303A`, `PID_1001`)
- It does not need RS485 wiring in `CONFIG_AMI_DEMO_MODE=y`
- The USB-native shell/debug path matches the XIAO build better than the DevKitC UART0 path

---

## Endpoint Naming Rule

The firmware derives the endpoint name from the last two bytes of the MAC address:

`ami-esp32c6-XXYY`

Example:

- Base MAC: `fc:01:2c:e3:d2:ac`
- Last two bytes: `d2:ac`
- Endpoint: `ami-esp32c6-d2ac`

Command to read MAC before flashing:

```powershell
python -m esptool --port COMx chip-id
```

---

## Standard Flow

### 1. Detect the board on Windows

```powershell
Get-CimInstance Win32_SerialPort | Select-Object DeviceID,Name,PNPDeviceID
```

Interpretation:

- `USB\VID_303A&PID_1001&MI_00` = native ESP USB Serial/JTAG
- CH34x/CP210x style bridges = external UART board, usually WROOM/DevKitC-like flow

### 2. Read MAC and derive endpoint

```powershell
python -m esptool --port COMx chip-id
```

Then derive endpoint from the last 2 MAC bytes, or let:

```powershell
python .\tools\provision_node.py --mac XX:XX:XX:XX:YY:ZZ --verify
```

do the conversion for you.

### 3. Build firmware

Use DEMO mode unless a real DLMS/RS485 meter is connected.

- Current repo default: `CONFIG_AMI_DEMO_MODE=y`

XIAO / Super Mini:

```powershell
Set-Location "C:\Users\User\Documents\FW\ESP32C6-XIAO\DLMS-COSEM"
$ami = "C:\Users\User\Documents\UNAL\ami-lwm2m-node"
west build -p always -b xiao_esp32c6/esp32c6/hpcore $ami
```

WROOM / DevKitC:

```powershell
Set-Location "C:\Users\User\Documents\FW\ESP32C6-XIAO\DLMS-COSEM"
$ami = "C:\Users\User\Documents\UNAL\ami-lwm2m-node"
west build -p always -b esp32c6_devkitc/esp32c6/hpcore $ami
```

### 4. Flash firmware

Native USB boards:

```powershell
Set-Location "C:\Users\User\Documents\FW\ESP32C6-XIAO\DLMS-COSEM"
west flash --esp-device COMx --esp-baud-rate 460800
```

If native USB CDC becomes unstable, fallback to JTAG:

```powershell
Set-Location "C:\Users\User\Documents\UNAL\ami-lwm2m-node"
python .\tools\flash_jtag.py
```

### 5. Provision the endpoint in ThingsBoard Edge

```powershell
Set-Location "C:\Users\User\Documents\UNAL\ami-lwm2m-node"
python .\tools\provision_node.py --endpoint ami-esp32c6-XXXX --host 192.168.1.111 --port 8090
```

### 6. Verify

ThingsBoard verification:

```powershell
python .\tools\provision_node.py --endpoint ami-esp32c6-XXXX --host 192.168.1.111 --port 8090 --verify
```

Node shell verification:

```text
ami log lwm2m
ami status
ami test lwm2m
```

Expected:

- Thread role becomes `child` or `router`
- `LwM2M : OK`
- Telemetry appears in ThingsBoard

### 7. Confirm in Edge (manual operations view)

```powershell
python .\tools\provision_node.py --endpoint ami-esp32c6-XXXX --host 192.168.1.111 --port 8090 --verify
```

Expected:

- `Active: True`
- `Telemetry:` contains recent values (`activeEnergy`, `frequency`, `voltage`, etc.)

---

## Automatic Flow

Use `tools/onboard_node.py` when the board type and COM are already known.

WROOM example:

```powershell
python .\tools\onboard_node.py --mac 98:a3:16:61:9e:70 --com COM13 --board esp32c6_devkitc/esp32c6/hpcore --host 192.168.1.111 --port 8090
```

Super Mini example:

```powershell
python .\tools\onboard_node.py --mac fc:01:2c:e3:d2:ac --com COM10 --board xiao_esp32c6/esp32c6/hpcore --host 192.168.1.111 --port 8090
```

---

## Operations Checklist (Copy/Paste)

Use this exact sequence for every new node.

### A. Identify node + endpoint

```powershell
Get-CimInstance Win32_SerialPort | Select-Object DeviceID,Name,PNPDeviceID
python -m esptool --port COMx chip-id
```

Endpoint rule: `ami-esp32c6-XXYY` (last two MAC bytes).

### B. Build

```powershell
Set-Location "C:\Users\User\Documents\FW\ESP32C6-XIAO\DLMS-COSEM"
$ami = "C:\Users\User\Documents\UNAL\ami-lwm2m-node"
west build -p always -b <board-target> $ami
```

Where `<board-target>` is one of:

- `xiao_esp32c6/esp32c6/hpcore` (XIAO, Super Mini)
- `esp32c6_devkitc/esp32c6/hpcore` (WROOM/DevKitC)

### C. Flash

```powershell
west flash --esp-device COMx --esp-baud-rate 460800
```

Fallback (native USB unstable):

```powershell
Set-Location "C:\Users\User\Documents\UNAL\ami-lwm2m-node"
python .\tools\flash_jtag.py
```

### D. Provision in Edge

```powershell
Set-Location "C:\Users\User\Documents\UNAL\ami-lwm2m-node"
python .\tools\provision_node.py --endpoint ami-esp32c6-XXXX --host 192.168.1.111 --port 8090
python .\tools\provision_node.py --endpoint ami-esp32c6-XXXX --host 192.168.1.111 --port 8090 --verify
```

### E. Validate runtime on node

Serial shell commands:

```text
ami log lwm2m
ami status
ami test lwm2m
ami status
```

Acceptance criteria:

- Thread `OK` (router/child)
- LwM2M `OK (registered)`
- Edge verify shows `Active: True` and non-empty telemetry

---

## Current Super Mini Bring-up Record

This is the real data captured for the newly connected ESP32-C6 Super Mini.

### Detection

- COM port: `COM10`
- PNP ID: `USB\VID_303A&PID_1001&MI_00\...`
- USB debug interface: `USB JTAG/serial debug unit`

### Chip identity

- Chip: `ESP32-C6FH4 (QFN32)`
- Base MAC: `fc:01:2c:e3:d2:ac`
- Endpoint: `ami-esp32c6-d2ac`

### Actions completed

1. Build completed for `xiao_esp32c6/esp32c6/hpcore`
2. Flash completed over native USB on `COM10`
3. Device provisioned in Edge:
   - Endpoint: `ami-esp32c6-d2ac`
   - Profile: `C2000_Monofasico_v2`
4. First verification right after flash:
   - `Active: False`
   - `Telemetry: (no data yet)`

### What this means

The infrastructure side is ready, but the node still needs runtime confirmation:

- successful boot into AMI firmware
- Thread attach
- first LwM2M register
- first telemetry push

If `Active: False` persists and Edge logs remain empty, collect the serial boot log first.

---

## Troubleshooting

### Native USB boards show `Error 31`

Symptom:

- `plink` or serial monitor reports `Configuring serial port: Error 31`

Action:

1. Close all `plink`/serial tools
2. Replug the board
3. Retry one clean serial session only
4. If needed, use `flash_jtag.py` instead of CDC-based workflows

### Edge shows device provisioned but inactive

Symptom:

- `Active: False`
- no telemetry
- no recent endpoint logs in `tb-edge`

Action:

1. Capture serial boot log
2. Confirm printed endpoint matches provisioned endpoint
3. Run `ami log lwm2m`
4. Check that the node reaches Thread attach
5. Check TB Edge logs for the endpoint during boot

### Wrong board target used

Symptoms include:

- no shell
- no Thread traffic
- no AMI boot log

Use this rule:

- native USB/JTAG board with no physical meter attached: start with `xiao_esp32c6/esp32c6/hpcore`
- external USB-UART board: start with `esp32c6_devkitc/esp32c6/hpcore`

---

## Operational Rule Going Forward

For every new node, record these 6 values in the commissioning log:

1. Board type
2. COM port
3. Base MAC
4. Endpoint
5. Zephyr target used
6. ThingsBoard device ID

That makes replacement, reflash, and fleet audits deterministic.