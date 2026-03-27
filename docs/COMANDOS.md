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

## Flash

```powershell
Set-Location "C:\Users\User\Documents\FW\ESP32C6-XIAO\DLMS-COSEM"
$ami = "C:\Users\User\Documents\UNAL\ami-lwm2m-node"
& ".venv\Scripts\python.exe" "$ami\tools\flash_jtag.py"
```

## Build + Flash (combined)

```powershell
Set-Location "C:\Users\User\Documents\FW\ESP32C6-XIAO\DLMS-COSEM"
$ami = "C:\Users\User\Documents\UNAL\ami-lwm2m-node"
west build -p always -b xiao_esp32c6/esp32c6/hpcore $ami
& ".venv\Scripts\python.exe" "$ami\tools\flash_jtag.py"
```

## Serial monitor

```powershell
& "C:\Program Files\PuTTY\plink.exe" -serial COM11 -sercfg 115200,8,n,1,N
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
| `ami test thread`      | Test Thread connectivity                             |
| `ami test lwm2m`       | Test LwM2M registration                              |
| `ami test dlms`        | Trigger DLMS poll and print readings                 |
| `ami test all`         | Run all tests                                        |
| `ami diag`             | Show per-OBIS read diagnostics (OK/AUTO/USER/ERR)    |
| `ami obis list`        | List all OBIS codes with their polling state         |
| `ami obis skip <idx>`  | Force-skip an OBIS code by index (USER-SKIP)         |
| `ami obis enable <idx>`| Re-enable an OBIS code (clears auto-skip too)        |
| `dlms_interval <s>`    | Change DLMS poll interval (5–300 s)                  |

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
