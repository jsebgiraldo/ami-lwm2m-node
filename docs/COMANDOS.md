# Useful Commands — AMI LwM2M Node

> All commands are run from **PowerShell**.
> The `.venv` and west workspace are in `DLMS-COSEM`.

---

## Build

```powershell
Set-Location "C:\Users\User\Documents\FW\ESP32C6-XIAO\DLMS-COSEM"
$ami = "C:\Users\User\Documents\UNAL\ami-lwm2m-node"
west build -p always -b xiao_esp32c6/esp32c6/hpcore $ami
```

## Build (WROOM C6 / DevKitC, UART nativo)

```powershell
Set-Location "C:\Users\User\Documents\FW\ESP32C6-XIAO\DLMS-COSEM"
$ami = "C:\Users\User\Documents\UNAL\ami-lwm2m-node"
west build -p always -b esp32c6_devkitc/esp32c6/hpcore $ami
```

## Flash

```powershell
Set-Location "C:\Users\User\Documents\FW\ESP32C6-XIAO\DLMS-COSEM"
$ami = "C:\Users\User\Documents\UNAL\ami-lwm2m-node"
& ".venv\Scripts\python.exe" "$ami\tools\flash_jtag.py"
```

## Flash (WROOM C6 / UART nativo)

```powershell
Set-Location "C:\Users\User\Documents\FW\ESP32C6-XIAO\DLMS-COSEM"
west flash --esp-device COM7 --esp-baud-rate 460800
```

## Build (ESP32-C6 Super Mini, USB nativo, modo DEMO)

```powershell
Set-Location "C:\Users\User\Documents\FW\ESP32C6-XIAO\DLMS-COSEM"
$ami = "C:\Users\User\Documents\UNAL\ami-lwm2m-node"
west build -p always -b xiao_esp32c6/esp32c6/hpcore $ami
```

## Flash (ESP32-C6 Super Mini, USB nativo)

```powershell
Set-Location "C:\Users\User\Documents\FW\ESP32C6-XIAO\DLMS-COSEM"
west flash --esp-device COM10 --esp-baud-rate 460800
```

Deteccion previa recomendada:

```powershell
Get-CimInstance Win32_SerialPort | Select DeviceID,Name,PNPDeviceID
python -m esptool --port COM10 chip-id
```

Caso real validado:

- COM: `COM10`
- Base MAC: `fc:01:2c:e3:d2:ac`
- Endpoint: `ami-esp32c6-d2ac`

## Build + Flash (combined)

```powershell
Set-Location "C:\Users\User\Documents\FW\ESP32C6-XIAO\DLMS-COSEM"
$ami = "C:\Users\User\Documents\UNAL\ami-lwm2m-node"
west build -p always -b xiao_esp32c6/esp32c6/hpcore $ami
& ".venv\Scripts\python.exe" "$ami\tools\flash_jtag.py"
```

## Serial monitor

```powershell
& "C:\Program Files\PuTTY\plink.exe" -serial COM7 -sercfg 115200,8,n,1,N
```

> `Ctrl+C` para salir. No toca RTS/DTR — no resetea el dispositivo.

## Flash + monitor

```powershell
Set-Location "C:\Users\User\Documents\FW\ESP32C6-XIAO\DLMS-COSEM"
$ami = "C:\Users\User\Documents\UNAL\ami-lwm2m-node"
& ".venv\Scripts\python.exe" "$ami\tools\flash_and_monitor.py"
```

---

## Shell CLI (uart:~$)

| Command                | Description                                          |
|------------------------|------------------------------------------------------|
| `ami status`           | Print general node status                            |
| `ami reset`            | Reboot the node (cold reboot)                        |
| `ami log quiet`        | Suppress RS485/DLMS DBG logs (default at startup)    |
| `ami log verbose`      | Enable RS485/DLMS DBG logs (hex dumps)               |
| `ami log lwm2m`        | Enable LwM2M debug logs (rd_client/engine/registry)  |
| `ami test thread`      | Test Thread connectivity                             |
| `ami test lwm2m`       | Test LwM2M registration                              |
| `ami test dlms`        | Trigger DLMS poll and print readings                 |
| `ami test all`         | Run all tests                                        |
| `ami diag`             | Show per-OBIS read diagnostics (OK/AUTO/USER/ERR)    |
| `ami obis list`        | List all OBIS codes with their polling state         |
| `ami obis skip <idx>`  | Force-skip an OBIS code by index (USER-SKIP)         |
| `ami obis enable <idx>`| Re-enable an OBIS code (clears auto-skip too)        |
| `dlms_interval <s>`    | Change DLMS poll interval (5–300 s)                  |

> En modo demo (`CONFIG_AMI_DEMO_MODE=y`), `ami test dlms` y telemetría LwM2M
> usan datos simulados (sin medidor físico RS485).

---

## Provisioning rápido en ThingsBoard Edge

```powershell
Set-Location "C:\Users\User\Documents\UNAL\ami-lwm2m-node"
python .\tools\provision_node.py --endpoint ami-esp32c6-XXXX --host 192.168.1.111 --port 8090
```

Verificar:

```powershell
python .\tools\provision_node.py --endpoint ami-esp32c6-XXXX --host 192.168.1.111 --port 8090 --verify
```

## Onboarding automático (build + flash + provisioning + verify)

```powershell
Set-Location "C:\Users\User\Documents\UNAL\ami-lwm2m-node"
python .\tools\onboard_node.py --mac 98:a3:16:61:9e:70 --com COM13 --host 192.168.1.111 --port 8090
```

Opciones útiles:

```powershell
# Si ya compilaste:
python .\tools\onboard_node.py --endpoint ami-esp32c6-9e70 --com COM13 --skip-build

# Solo provisioning/verify (sin flash):
python .\tools\onboard_node.py --endpoint ami-esp32c6-9e70 --skip-build --skip-flash --verify-only
```

Super Mini (manual board selection):

```powershell
python .\tools\onboard_node.py --mac fc:01:2c:e3:d2:ac --com COM10 --board xiao_esp32c6/esp32c6/hpcore --host 192.168.1.111 --port 8090
```

## Validación operacional mínima en Edge

Después de provisioning, validar endpoint activo y telemetría:

```powershell
Set-Location "C:\Users\User\Documents\UNAL\ami-lwm2m-node"
python .\tools\provision_node.py --endpoint ami-esp32c6-XXXX --host 192.168.1.111 --port 8090 --verify
```

Checklist mínimo de aceptación:

- `Active: True`
- Telemetría no vacía
- En shell del nodo: `ami status` con `Thread : OK` y `LwM2M : OK`

## Flujo completo de onboarding

Ver tambien:

- [docs/NODE_ONBOARDING_PLAYBOOK.md](docs/NODE_ONBOARDING_PLAYBOOK.md)

Ese documento consolida el flujo estándar para XIAO, WROOM/DevKitC y Super Mini:

- deteccion del board
- lectura de MAC y endpoint
- build y flash
- provisioning en ThingsBoard
- verificacion Thread/LwM2M
- troubleshooting operativo

---

## Unit tests (host, no hardware required)

```powershell
Set-Location "C:\Users\User\Documents\UNAL\ami-lwm2m-node\tests"
gcc -o run_tests.exe test_main.c test_hdlc.c test_cosem.c `
    ../src/dlms_hdlc.c ../src/dlms_cosem.c `
    -I../src -Istubs -DUNIT_TEST -lm -Wall
.\run_tests.exe
```

---

## Notes

- Flash uses **OpenOCD JTAG** — no buttons or bootloader mode required.
- Either activate the `.venv` first, or use the full path as shown above.
- If the terminal is already in `DLMS-COSEM` with the venv active, only `west build ...` and `python ...` are needed.
- LwM2M server failover is configured in `prj.conf` with:
    - `CONFIG_AMI_LWM2M_SERVER_IPV6_PRIMARY="..."`
    - `CONFIG_AMI_LWM2M_SERVER_IPV6_SECONDARY="..."`
    The node switches automatically after repeated registration failures.
