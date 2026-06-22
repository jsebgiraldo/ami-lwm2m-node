# Onboarding de comandos — cómo trabajar con el nodo y la flota

Referencia práctica. Tres planos: **(A) shell en el nodo** (serial), **(B) tools del host**
(build/flash/auditoría), **(C) servidores** (OTBR / TB Edge).

Rutas base:
- Repo firmware: `c:/Users/jsgir/Documents/UNAL/Unal-Flash-tool/firmware/ami-lwm2m-node`
- Venv Python (tiene west/esptool/pyserial): `C:\Users\jsgir\Documents\ESP32\.venv\Scripts\python.exe`
- West workspace (build dirs): `C:\Users\jsgir\Documents\ESP32\zephyrproject`
- TB Edge: `http://192.168.8.111:8090` (tenant@thingsboard.org / tenant)

---

## A) Shell del nodo (sobre USB-Serial-JTAG, 115200)

**Conectarse a la consola** de un board (reemplazá COMx):
```powershell
& "C:\Users\jsgir\Documents\ESP32\.venv\Scripts\python.exe" -m serial.tools.miniterm COMx 115200
# salir: Ctrl+]
```
> El shell solo existe si `CONFIG_SHELL=y` (sí en prod, "forensic capture" v0.6.43).

**Árbol de comandos `ami`:**
| Comando | Qué hace |
|---|---|
| `ami status` | estado general del nodo (Thread role, LwM2M, uptime, heap) |
| `ami diag` | diagnóstico de lectura por-OBIS (éxitos/fallos por código) |
| `ami temp` | temperatura del SoC (°C) |
| `ami reset` | reboot del nodo |
| `ami test thread` | test de attach a Thread |
| `ami test lwm2m` | test de registro LwM2M |
| `ami test dlms` | dispara un poll DLMS y reporta lecturas |
| `ami test all` | corre todos los tests |
| `ami log quiet` | silencia logs DLMS/RS485 (solo WRN) |
| `ami log verbose` | DBG completo DLMS/RS485 |
| `ami log meter\|cosem\|hdlc\|rs485\|lwm2m` | DBG de un módulo puntual |
| `ami obis list` | lista códigos OBIS y su estado de polling |
| `ami obis skip <index>` | fuerza saltar un OBIS |
| `ami obis enable <index>` | re-habilita un OBIS |
| `ami rgb <off\|red\|green\|blue\|yellow\|cyan\|magenta\|white>` | color del LED |
| `ami brightness [0..255]` | brillo (sin arg = muestra actual) |
| `dlms_interval <segundos>` | cambia el período de poll DLMS (comando raíz, no bajo `ami`) |

> Nota: el LED está no-op'd en HW (GPIO8 rompe la radio). Los comandos rgb/brightness existen pero no encienden nada.

---

## B) Tools del host (build / flash / auditoría)

Todos se corren desde el repo con el venv: `& $PY tools/<script>.py ...`
(donde `$PY = C:\Users\jsgir\Documents\ESP32\.venv\Scripts\python.exe`).

### Build
| Script | Produce |
|---|---|
| `tools/build_prod.py` | `build_prod` — canónico producción (keepalive=300) |
| `tools/build_ka90.py` | `build_ka90` — keepalive=90 |
| `tools/build_audit.py` | `build_audit` — + THREAD_ANALYZER + stack guard (auditoría) |

Build manual directo (west):
```powershell
$env:PATH = "C:\Users\jsgir\Documents\ESP32\.venv\Scripts;$env:PATH"
west build --build-dir build_prod -p always --sysbuild -b xiao_esp32c6/esp32c6/hpcore `
  "<repo>" -- -DEXTRA_CONF_FILE="overlays/ftd.conf;overlays/resprobe_lwm2m.conf;overlays/prod_fat.conf"
```

### Flash
| Script | Uso |
|---|---|
| `tools/flash_fleet_seq.py --coms COM17,COM18,... [--build-dir build_audit]` | flasheo SECUENCIAL de varios (anti-wedge) |
| `tools/flash_one.py --com COMx [--build-dir D] [--skip-provision] [--no-wait-tb]` | un board (lee MAC + provisiona en TB) |

**Receta anti-wedge (single-shot manual)** — la que vence el "device not functioning":
```powershell
$B = "C:\Users\jsgir\Documents\ESP32\zephyrproject\build_audit"
& $PY -m esptool --chip esp32c6 --port COMx --baud 460800 --before default-reset --after hard-reset `
  write-flash --erase-all --flash-freq 20m --flash-mode dout `
  0x0 "$B\mcuboot\zephyr\zephyr.bin" 0x20000 "$B\ami-lwm2m-node\zephyr\zephyr.signed.bin"
```
- Boards wedged: poné en **download mode** (mantener BOOT + power-cycle) y usá `--before no-reset`.
- Si el RTS no bootea tras flashear → **power-cycle físico** (los SuperMini lo necesitan).
- NUNCA flashees en paralelo (cascadea el wedge). Siempre secuencial.

### Detección de boards (COM ↔ MAC ↔ Lab, sin esptool, sin wedge)
```powershell
Get-PnpDevice -Class Ports -Status OK | Where-Object {$_.InstanceId -match 'VID_303A&PID_1001'} | ForEach-Object {
  $com = if ($_.FriendlyName -match '\((COM\d+)\)') { $matches[1] } else { '?' }
  $parent = (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName 'DEVPKEY_Device_Parent').Data
  $mac = if ($parent -match '\\([0-9A-Fa-f:]{17})$') { $matches[1].ToLower() } else { '?' }
  [PSCustomObject]@{ COM=$com; MAC=$mac }
} | Sort-Object COM | Format-Table -AutoSize
```
> Mapa MAC→Lab en `tools/fleet_map.csv`. ⚠️ NO tocar COM68 / MAC ...ad64 (otro proyecto).

### Auditoría / monitoreo (read-only, vía TB)
| Script | Qué da |
|---|---|
| `tools/fleet_audit.py` | snapshot one-shot: clasifica HEALTHY/WATCH/STUCK/STALE por board |
| `tools/fleet_track.py [period_s]` | tracking continuo de power-on (reporting/routers/resets) |
| `tools/flicker_baseline.py [poll_s]` | detecta flaps active/inactive → `tools/flicker_log.csv` |
| `tools/audit_console_capture.py [--duration N]` | captura consola de TODOS los COMs + parsea THREAD_ANALYZER (stacks/overflow) |
| `tools/otbr_correlate.py --host user@ip` | tablas mesh + eidcache del OTBR (correlación inbound) |

---

## C) Servidores

### TB Edge (observe / pmax / perfiles)
```powershell
& $PY tools/tb_edge_monitoring_setup.py --only observe --dry-run   # ver cambios
& $PY tools/tb_edge_monitoring_setup.py --only observe             # aplicar
```
- pmin/pmax viven en `deviceProfile → transportConfiguration.observeAttr.attributeLwm2m`.
- **Propagan solo en el próximo REGISTER** del board → forzar con Reboot RPC o power-cycle.

### OTBR (Pi4) — diagnóstico de malla (vía SSH)
```bash
ot-ctl state              # leader/router/child
ot-ctl router table       # routers + link quality
ot-ctl child table        # hijos directos
ot-ctl eidcache           # resolución de direcciones (retry/0 = collapse)
ot-ctl dataset active -x  # dataset (canal/netkey) para el sniffer
```

---

## Flujos típicos

**Flashear y verificar un board:**
1. detectar COM (PowerShell arriba) → 2. `flash_fleet_seq.py --coms COMx` →
3. power-cycle si no bootea → 4. `fleet_audit.py` para confirmar fw + streaming.

**Diagnosticar un board "mudo":**
1. `fleet_audit.py` (¿STUCK/STALE?) → 2. consola `miniterm COMx` + `ami status` →
3. si no responde, `otbr_correlate.py` (¿lo resuelve el OTBR?).

**Cambiar cadencia de telemetría:** editar `tb_edge_monitoring_setup.py` (pmax) → aplicar → Reboot RPC.
