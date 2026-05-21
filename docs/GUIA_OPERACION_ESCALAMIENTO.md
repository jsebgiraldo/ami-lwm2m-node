# Guía de Operación y Escalamiento — Red AMI LwM2M sobre Thread

**Proyecto:** AMI LwM2M Node (medidores) — ESP32-C6 + Zephyr + OpenThread, gestionados por ThingsBoard Edge.
**Fecha:** 2026-05-21 · **Firmware de referencia:** v0.6.31 · **Estado:** 29-30 nodos estables (>12 h continuas).
**Meta de este documento:** consolidar todo lo aprendido (bloqueos, soluciones, arquitectura, limitaciones) y definir qué se necesita para **escalar a 60 nodos**.

---

## 1. Arquitectura del sistema

```
[Medidor DLMS] --RS485/HDLC--> [Nodo ESP32-C6 XIAO]
                                      |  (LwM2M sobre CoAP/UDP)
                                      v
                            Red Thread (802.15.4, IPv6)
                                      |
                          [OTBR + TB Edge en Pi4]  192.168.8.111
                                      |  (gRPC cloud-edge :7070)
                                      v
                          [ThingsBoard Central]   192.168.8.124
```

| Capa | Componente | Detalle |
|---|---|---|
| Nodo | XIAO ESP32-C6 (BOYA flash, mfr 0x46) | Zephyr 4.3.x, cliente LwM2M, OpenThread MTD/FTD |
| Radio/Malla | OpenThread (IEEE 802.15.4) | IPv6, prefijo mesh-local `fd32:e7c9:e9af:adf2::/64`, ~250 kbps compartidos |
| Borde | Pi4 "EKH01-DE87" `192.168.8.111` | OpenWrt 23.05.5, 3.84 GB RAM, OTBR (otbr-agent) + Docker |
| Servidor LwM2M | TB Edge `thingsboard/tb-edge:4.3.1.1EDGE` | Embebe **Leshan**; CoAP/UDP :5683; Postgres local |
| Nube | TB Central `192.168.8.124` | Sync por gRPC (`CLOUD_RPC_HOST=192.168.8.124:7070`) |
| Radio Thread | nRF (RCP) en `/dev/ttyACM0` | spinel+hdlc+uart 115200 |

**Descubrimiento:** los nodos resuelven el servidor LwM2M por **DNS-SD/SRP** (`_lwm2m._udp.default.service.arpa`). El Edge se anuncia en la malla con una IPv6 mesh (`fd32:...:3282`) vía el SRP server del OTBR.

---

## 2. Bloqueos encontrados y cómo se resolvieron

Resumen ejecutivo de los problemas de la campaña y su solución definitiva.

| # | Bloqueo | Síntoma | Causa raíz | Solución |
|---|---|---|---|---|
| 1 | **OTA no funciona por TB Edge** | `fw_state` INITIATED→FAILED, 0 bloques a `/5/0/0` | TB **Edge no ejecuta el motor de FOTA LwM2M** (es función de Central, que no tiene sesión CoAP con el nodo) | Empujar la imagen nosotros vía RPC **`WriteReplace /5/0/0`** (hex) a través del Leshan del Edge. Ver §5. |
| 2 | **Modelos OMA borrados** | `Tenant hasn't such resource: id [5] version [1.0]` | La reinstalación de TB Edge borró los modelos OMA; faltaban Object 3 (Device) y 5 (Firmware) v1.0 | Subir `models/3.xml` + `models/5.xml` con `tools/tb_edge_upload_models.py` y reiniciar el contenedor |
| 3 | **Transferencia OTA se corta al 90%** | Bloques paran a los ~528 KB / 120 s | `LWM2M_TIMEOUT` por defecto = 120000 ms cortaba la transferencia lenta sobre Thread | `LWM2M_TIMEOUT=600000` en el compose del Edge |
| 4 | **Bricking masivo al flashear** | Boot-loop: `Checksum failure ets_main.c 331`, `rst:TG0_WDT` | Flashear con **`dio/40m`**: el ROM del ESP32-C6 no lee confiable la flash BOYA marginal → lee 0xFF → checksum falla. esptool verifica OK (lee con stub), el brick es invisible hasta el primer boot | Flashear con **`dout/20m`** (default nuevo de `flash_ota_migrate.py`). Ver §4. |
| 5 | **Flota caída tras reboot/apagón** | Solo ~7/30 reconectan; nadie re-registra | El **SRP server arranca DESHABILITADO** en el OTBR → `_lwm2m` no se anuncia → ningún nodo descubre el Edge | Hook en `/usr/sbin/otbr-srp-publish`: `ot-ctl srp server enable` tras unirse a Thread. Ver §3. |
| 6 | **Tormenta de reinicios** | Nodos reinician en lockstep cada 300 s, desgaste de flash | Watchdog "active-from-boot" hace cold-reboot a los 300 s sin primer registro; 30 nodos sincronizados durante una caída del servidor | Anti-storm v0.6.30: jitter por nodo + backoff exponencial (300→3600 s) + tope de 5 reboots (luego deja de reiniciar). Ver §4. |
| 7 | **Flapping de sesiones** | Nodos registran y caen; `recover_count` alto (45) | **Topología en estrella**: todos MTD colgando de un único router (OTBR) a RSSI marginal (-77..-86 dBm, LQ 2) → pérdida de paquetes CoAP | Flashear ~10-15% como **FTD (routers)** → malla multi-salto. Ver §4 y §6. |
| 8 | **`docker logs` corrupto** | `invalid character '\x00'` | Escrituras rotas (incidente de cgroups) dejaron bytes nulos en el json-file (sin rotación) | Vaciar el log (contenedor detenido) + rotación `max-size 20m, max-file 5` en el compose |
| 9 | **Corrupción de cgroups del Docker** | Creación de contenedor cuelga; `failed to enable controllers` | Reinicios repetidos de dockerd dañaron la jerarquía cgroup v2 | `/etc/init.d/dockerd stop && start` (full stop/start, no `restart`) |
| 10 | **RCP (radio nRF) colgado** | `spinel Wait for response timeout`, otbr-agent en crash-loop | El nRF quedó sin responder a spinel | **Reboot del Pi4** (re-enumera el USB del RCP); el SRP server queda deshabilitado tras el reboot → aplicar §3 |

**Lección transversal:** operar el Edge por SSH con comandos multi-paso debe hacerse como **un solo script `nohup` detached + polling de un logfile** — los read-timeouts de paramiko mandan SIGHUP al proceso remoto y dejan contenedores a medio crear (fue el origen del incidente de cgroups).

---

## 3. El Edge (Pi4) — configuración, fixes y operación

### 3.1 Hardware y software
- **Pi4 EKH01-DE87**, `192.168.8.111`, SSH `root:root`. OpenWrt 23.05.5, aarch64, **3.84 GB RAM**.
- Docker: `pi4-edge-v2` (TB Edge 4.3.1.1EDGE) + `pi4-edge-postgres` (postgres 15). Compose en `/opt/docker/pi4-edge/docker-compose.yml`.
- OTBR: `otbr-agent` en el host (no en contenedor), RCP nRF en `/dev/ttyACM0`. Co-existe con una red **Wi-SUN** (`wisun0`) — cuidado al tocar border-routing.
- OpenWrt es minimalista: **no hay** python3/pip/coap-client/setsid/timeout en el host. Usar Docker o paramiko desde el PC.

### 3.2 Fixes aplicados al Edge (todos persistentes)
1. **SRP server en boot** — `/usr/sbin/otbr-srp-publish` ahora ejecuta `ot_ctl srp server enable` tras `wait_for_thread_attached`, antes de publicar. Backup: `.bak-srpfix`.
   - Nota: `srp server auto enable` NO sirve aquí (depende de border-routing, que está detenido). Por eso el enable explícito.
2. **`LWM2M_TIMEOUT=600000`** en el `environment` de tb-edge (compose). Backup: `.bak-ota`.
3. **Rotación de logs** json-file `max-size 20m, max-file 5` (compose). Backup: `.bak-logrot`. Aplica al próximo recreate.
4. **Modelos OMA** Object 3/5 v1.0 + custom (10242, 33000, 3303) cargados en el resource library (`tools/tb_edge_upload_models.py`).

### 3.3 Presupuesto de recursos (referencia)
Con 7 nodos en el R1000/CM4 (1.8 GB) — ver `docs/TB_EDGE_HARDWARE_CONSTRAINTS.md`:
- JVM tb-edge `-Xmx768m` → ~750-900 MB en pico. Postgres ~80 MB. OS+Docker ~150 MB.
- Pi4 actual (3.84 GB) con 29 nodos: load ~0.5, ~1.4 GB usados. JVM `-Xmx1g`. **Holgado.**

### 3.4 Recuperación rápida del Edge
- **Reiniciar solo TB (sin tocar OTBR/malla):** `docker stop pi4-edge-v2 && docker start pi4-edge-v2` (NO `compose up --recreate` salvo necesidad — riesgo cgroups). Los nodos re-registran solos; el SRP sigue arriba.
- **Tras reboot del Pi4:** verificar `ot-ctl srp server state` = `running` y `ot-ctl srp server service | grep lwm2m`. Si está disabled: `/etc/init.d/otbr-srp restart` (ya tiene el hook).
- **RCP colgado** (`ot-ctl state` = connection refused, crash-loop): reboot del Pi4; si persiste, replug físico del dongle nRF.

---

## 4. Los Nodos — firmware, flasheo y comportamiento

### 4.1 Variantes de firmware
| Variante | Build dir | Config | Uso |
|---|---|---|---|
| **MED** (Minimal End Device, MTD) | `build_ota` | `overlays/med.conf` (`CONFIG_OPENTHREAD_MTD=y`) | La mayoría de la flota. No puede ser router. |
| **FTD** (Full Thread Device, router-eligible) | `build_ota_ftd` | `overlays/ftd.conf` (`CONFIG_OPENTHREAD_FTD=y`) | ~10-15% del fleet, como routers de malla. |

Ambas con MCUboot (`overlays/ota.conf`) → capaces de OTA. Versión actual: **0.6.31**.

### 4.2 Flasheo por USB — el setting crítico
**Siempre `dout/20m`** (default nuevo). `dio/40m` brickea unidades BOYA marginales (bloqueo #4).
```powershell
$env:PATH = "C:\Users\jsgir\Documents\ESP32\.venv\Scripts;$env:PATH"
cd C:\Users\jsgir\Documents\UNAL\Unal-Flash-tool\firmware\ami-lwm2m-node
python tools/flash_ota_migrate.py --com COMxx                      # MED (default)
python tools/flash_ota_migrate.py --com COMxx --build-dir build_ota_ftd   # FTD router
```
- Hace e2e por nodo: flash → MCUboot → Thread attach → registro LwM2M → verifica `active` + telemetría.
- Registra `tools/mac_com_map.csv` (COM, MAC, endpoint, variante) en cada flasheo.
- Si un puerto está trabado (`Could not open` por USB-JTAG en boot-loop): **replug físico** y reintentar.

### 4.3 Comportamiento del LED (v0.6.31) — "OFF idle, ON TX"
- **Arranque/conectando:** LED encendido (BLUE/CYAN).
- **Al registrar:** flash verde 800 ms → **modo operación → LED OFF**.
- **Idle (operación continua):** LED apagado.
- **Cada transmisión real** (push de datos del medidor): blink verde ~90 ms.
- **Errores:** RED fijo (reg-failure/timeout/detach Thread) — NO se enmascara con el blink de TX.

### 4.4 Robustez (watchdogs y contadores)
- **Watchdog de liveness LwM2M** (anti-storm v0.6.30): jitter + backoff (300→600→1200→2400→3600 s) + tope de 5 cold-reboots; contador NVS `noreg_boots`; reset al primer REGISTER.
- **Watchdog de HW** (TG0) + boot-watchdog: cubren cuelgues a nivel ROM/tarea.
- **Object 33000** expone diagnóstico: `total_resets`, `watchdog_count`, `recover_count`, `last_reset_reason`, `uptime_s`, etc. (clave para forense).
- Telemetría persistida en NVS sobrevive reboots.

---

## 5. OTA over-the-air (sin USB) — procedimiento verificado

TB Edge **no** empuja OTA. Método validado (0.6.28→0.6.29 e2e):
1. **Reset estado:** RPC `WriteReplace /5/0/1 = ""` (vuelve State a Idle).
2. **Empujar imagen:** RPC **ONEWAY** `WriteReplace /5/0/0 = hex(zephyr.signed.bin)`. El opaque debe ir en **hex**. ONEWAY evita el timeout de 30 s de Tomcat. Leshan hace el block1 (bloques de 256 B sobre Thread, ~4 KB/s, ~130 s para 585 KB).
3. **Esperar** State `/5/0/3` = 2 (Downloaded).
4. **Aplicar:** RPC ONEWAY `Execute /5/0/2` → MCUboot swap + reboot.
5. **Verificar** `/3/0/3` = versión objetivo.

Herramientas: `tools/ota_push_direct.py`, `tools/ota_e2e_1494.py`. Requiere `LWM2M_TIMEOUT=600000` (§3.2).

**Limitación OTA:** es **uno-a-uno** y lento (~2-3 min/nodo). Una campaña OTA de 60 nodos es secuencial → planear ventanas. El nodo debe estar en MCUboot (migrado por USB una vez).

---

## 6. Escalamiento a 60 nodos — qué se necesita

### 6.1 Malla Thread (el limitante principal)
- **Routers:** Thread admite máx **32 routers**; el líder mantiene un número óptimo. Para 60 nodos: **6-9 FTD bien distribuidos** (10-15%). Colocarlos *entre* el OTBR y los clústeres lejanos, NO juntos.
- **Hijos por router:** revisar `CONFIG_OPENTHREAD_MAX_CHILDREN` (default ~10). Con 60 MTD repartidos en ~7 routers ≈ 8-9 hijos/router — ajustar al alza si hace falta (p.ej. 16).
- **Ancho de banda:** Thread comparte ~250 kbps. **Este es el cuello de botella real.** 60 nodos × 14 recursos cada ~15 s saturarían la malla. Mitigaciones:
  - Subir `CONFIG_AMI_LWM2M_NOTIFY_MIN_INTERVAL_MS` (throttle por recurso; ya suprime ~50%).
  - Subir el `lifetime` LwM2M (ya 300 s) reduce overhead de UPDATE.
  - Espaciar/escalonar los push (jitter de transmisión).
- **Cobertura RF:** medir RSSI/LQ (`ot-ctl neighbor table`). Ningún nodo debería estar a >-85 dBm de su router. Si los hay, agregar un FTD cerca.

### 6.2 Edge (Pi4) — sizing para 60 nodos
- **JVM:** la carga LwM2M (observes/notifies) escala ~lineal con nodos×recursos. 60 nodos ≈ 8× la carga de 7. Subir `JAVA_OPTS -Xmx` a **1.5-2 GB** (hay 3.84 GB) y monitorear GC. Pico actual con 29 nodos: cómodo.
- **Postgres:** la tasa de inserts de telemetría sube ~lineal. Vigilar I/O de disco (overlay de OpenWrt). Considerar retención/agregación.
- **Leshan/CoAP:** pools de hilos del transporte LwM2M (`uplink/downlink pool size`) — vigilar si aparecen colas. El `LWM2M_TIMEOUT` alto (600 s) sigue OK.
- **SRP server:** mantiene N registros + leases; 60 servicios es manejable, pero validar tras escalar.
- **Sync al Cloud:** el gRPC Edge→Central sube ~lineal; monitorear `Failed to deliver`.

### 6.3 Workflow de despliegue de 60 nodos
- Flashear por USB ~30 s + registro ~1 min = **~1.5-2 min/nodo** → 60 nodos ≈ **1.5-2 h**. Hacerlo por tandas; `mac_com_map.csv` registra todo automáticamente.
- Designar y rotular físicamente los **6-9 FTD** (flashear con `--build-dir build_ota_ftd`).
- Tras cada tanda: health-check (active count, `ot-ctl router table`, `watchdog/recover` counts).

### 6.4 Checklist para 60 nodos
- [ ] 6-9 nodos FTD distribuidos por cobertura (no juntos).
- [ ] `CONFIG_OPENTHREAD_MAX_CHILDREN` suficiente (≥16) en la variante FTD.
- [ ] Throttle de notify / lifetime ajustados para no saturar los 250 kbps.
- [ ] Edge `-Xmx` 1.5-2 GB; monitoreo de RAM/GC/postgres.
- [ ] SRP server enable-on-boot verificado (§3.2).
- [ ] Todos flasheados con `dout/20m` (sin brick).
- [ ] Plan de campañas OTA escalonadas (uno-a-uno).

---

## 7. Limitaciones conocidas

| Limitación | Impacto | Mitigación |
|---|---|---|
| TB Edge no hace OTA LwM2M | OTA manual vía WriteReplace, uno-a-uno | `ota_push_direct.py`; planear ventanas |
| Ancho de banda Thread (~250 kbps compartido) | Techo de telemetría a escala | Throttle de notify, lifetime largo, escalonar |
| OTBR único = punto único de fallo | Si cae el Pi4, cae toda la malla | Backups de config; recuperación documentada (§3.4) |
| SRP server no auto-enable en boot | Caída de flota tras apagón si falta el hook | Hook en otbr-srp-publish (aplicado) |
| Flash BOYA sensible | Brick con dio/40m | `dout/20m` obligatorio (default) |
| USB-JTAG se traba en boot-loop | esptool no abre el puerto | Replug físico |
| OTA transfer lento (~4 KB/s) | ~2-3 min/nodo | Solo para recuperar/actualizar, no masivo frecuente |
| Co-existencia Wi-SUN en el Pi4 | Habilitar border-routing afecta ambos | No tocar BR salvo necesidad |

---

## 8. Referencia rápida de comandos

```powershell
# Flashear (MED / FTD) — siempre dout/20m, registra mac_com_map.csv
python tools/flash_ota_migrate.py --com COMxx
python tools/flash_ota_migrate.py --com COMxx --build-dir build_ota_ftd

# Build (en el workspace west)
west build --sysbuild --build-dir build_ota -b xiao_esp32c6/esp32c6/hpcore <app> -- -DEXTRA_CONF_FILE="overlays/med.conf;overlays/r1000.conf;overlays/ota.conf"
# (FTD: overlays/ftd.conf; build_ota_ftd)

# OTA over-the-air
python tools/ota_push_direct.py --device ami-esp32c6-XXXX --version X.Y.Z
```
```bash
# Edge (SSH root:root@192.168.8.111)
ot-ctl state ; ot-ctl srp server state ; ot-ctl router table ; ot-ctl neighbor table
docker ps ; docker stats --no-stream ; docker logs --tail 50 pi4-edge-v2
ot-ctl srp server enable           # si quedó disabled tras reboot
```

---

*Documento generado durante la campaña de estabilización (29-30 nodos, >12 h continuas). Ver también: `docs/architecture/LESSONS_LEARNED.md`, `docs/TB_EDGE_HARDWARE_CONSTRAINTS.md`, `docs/PROVISIONING.md`, `docs/E2E_PROCEDURE.md`.*
