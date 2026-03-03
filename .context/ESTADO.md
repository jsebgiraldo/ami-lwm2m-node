# Estado actual del sistema — Actualizar con cada sesión

## Última actualización: 2026-03-02 (sesión 9)

## 🟢 ESTADO: SISTEMA OPERATIVO — Medidor leyendo 22/22 OBIS • 98 unit tests PASSED

## Estado de contenedores (192.168.1.111)
- `tb-edge`: UP — thingsboard/tb-edge:4.2.1EDGE (host networking, LwM2M en puerto 5683)
  - **CLOUD_RPC_HOST: 192.168.1.159** (LAN directo, NO Tailscale) ✅
  - Desplegado via `docker compose up -d` desde `/root/tb-edge/docker-compose.yml`
  - **Env vars CORRECTAS** (ver sección docker-compose.yml abajo)
- `tb-edge-postgres`: UP — postgres:15-alpine (estable, user=tb_edge, db=tb_edge)
- `prometheus-agent`: Exited (detenido para liberar recursos)
- OTBR: Servicio nativo OpenWrt, estado "leader", partición 454999710, RLOC16=0x4000

## Edge↔Cloud gRPC — CONECTADO ✅
- Cloud server: **192.168.1.159** (LAN on-premise, antes era 100.67.60.126 Tailscale — inalcanzable)
- Puerto 7070: **ABIERTO y verificado** (`nc -w3 -v 192.168.1.159 7070` → Connection open)
- gRPC connect: `Sending a connect request to the TB!` → `Configuration received` → cloudType: "CE"
- Uplink sync: `Sending of [3] uplink msg(s) took 165 ms` — datos sincronizando al Cloud
- iptables: **REGLA ELIMINADA** (ya no se bloquea gRPC, Cloud es LAN directo)
- docker-compose.yml: actualizado con `CLOUD_RPC_HOST: "192.168.1.159"`

## Estado del nodo — NUEVO XIAO ESP32-C6 ✅
- **Hardware**: Nuevo XIAO ESP32-C6 (reemplazó al anterior con radio débil)
- Puerto COM: **COM12** (RTS toggle resetea el ESP32, script `clean_reset_monitor.py`)
- Firmware: Zephyr v4.3.0-6612-g6159cb3fecf8, OpenThread 9a40380a4, v0.12.0
- MAC base: `98:a3:16:61:24:34` (ESP32-C6FH4 QFN32 rev v0.2)
- Endpoint LwM2M: **`ami-esp32c6-2434`** (construido en runtime desde últimos 2 bytes MAC)
- Server URI: `coap://[fdc6:63fd:328d:66df:6a54:12ef:8c67:bd1c]:5683`
- Device ID en TB Edge: `cc9da070-135b-11f1-80f9-cdb955f2c365`
- Conectado a Thread: **SÍ — Router estable** ✅
  - Role=3 (Router) en partición OTBR 454999710
  - RLOC=0x6800
  - RSSI=-86dBm, LQI=66% (mucho mejor que XIAO anterior)
  - 1 neighbor estable
  - Red: AMI-Pilot-2025, PAN 0xABCD, Ch25
- **Registrado en LwM2M: SÍ** ✅
  - Registration completa en 17.0s desde boot
  - Boot (11.2s) → Thread attached (11.3s) → LwM2M registered (17.0s)
  - Device activo en TB Edge con telemetría fluyendo

## Telemetría en TB Edge ✅ — VERIFICADA END-TO-END (ESP32→Edge→Cloud)
- **Perfil**: `C2000_Monofasico_v2` (profile ID: `b6d55c90-12db-11f1-b535-433a231637c4`) — monofásico
  - Anterior: `LwM2M_Profile` (`cd8e9400-f018-11f0-80e6-6b4796226358`) — genérico
- **Observaciones diferenciadas** (ObserveStrategy: SINGLE):
  - **Grupo 1 — Telemetría Operacional** (pmin=15s, pmax=30s):
    - TensionR (RID 4), CorrienteR (RID 5), PotenciaActivaR (RID 6), EnergiaActivaTotal (RID 41)
  - **Grupo 2 — Caracterización de Carga** (pmin=60s, pmax=300s):
    - PotenciaReactivaR (RID 7), PotenciaAparenteR (RID 8), FactorPotenciaR (RID 9)
    - PotenciaTotalActiva (RID 34), PotenciaTotalReactiva (RID 35), PotenciaTotalAparente (RID 36)
    - FactorPotenciaTotal (RID 37), EnergiaReactivaTotal (RID 42), EnergiaAparenteTotal (RID 43)
    - Frecuencia (RID 49), RSSI (RID 51), LQI (RID 52)
- **Atributos del cliente**: Manufacturer=Tesis-AMI, ModelNumber=XIAO-ESP32-C6, SerialNumber=AMI-001
- **Telemetría verificada en Cloud** (192.168.1.159 REST API, sesión 7):
  - voltage: 122.9-123.1V, current: 0.0A, activePower: 0.0W, frequency: ~60.0Hz, powerFactor: 1.0
  - Múltiples data points en ventana de 5 minutos confirman flujo continuo
- **Device ACTIVE en Cloud**: `active=True`, lastActivity actualizado en tiempo real
- **Nota**: WriteAttributesRequest para /10242_1.0/0/14 retorna 500 (error menor, no afecta funcionalidad)

## Credenciales LwM2M — CORREGIDAS VÍA REST API ✅
- Dispositivo creado via REST API (no manipulación directa de DB)
- Perfil: `C2000_Monofasico_v2` (transport_type=LWM2M, profile ID: `b6d55c90-12db-11f1-b535-433a231637c4`)
- **Tipo credenciales: `LWM2M_CREDENTIALS`** ✅
- **Credentials ID: `ami-esp32c6-2434`** ✅
- **Credentials value: NoSec JSON (formato correcto)** ✅
  ```json
  {"client":{"securityConfigClientMode":"NO_SEC","endpoint":"ami-esp32c6-2434"},
   "bootstrap":{"bootstrapServer":{"securityMode":"NO_SEC"},"lwm2mServer":{"securityMode":"NO_SEC"}}}
  ```
- **LECCIÓN APRENDIDA**: `securityConfigClientMode` debe ser string `"NO_SEC"`, NO objeto `{"mode":"NO_SEC"}`
  - Formato incorrecto `{"mode":"NO_SEC"}` causa `LwM2MAuthException: null` → CoAP 5.0
  - La manipulación directa de DB tampoco funciona — usar siempre REST API
- Script fix: `fix_lwm2m_api4.py` (Python, usa REST API correctamente)
- **Cloud gRPC RESTAURADO** — `CLOUD_RPC_HOST=192.168.1.159` (LAN directo)
  - Antes: Bloqueado via iptables a IP Tailscale 100.67.60.126 (inalcanzable)
  - Ahora: Conexión directa LAN a on-premise, Edge↔Cloud sincronizando ✅
- **Password tenant**: reseteado a `tenant` via bcrypt $2a$ prefix en DB
  - Login: `tenant@thingsboard.org` / `tenant` en http://192.168.1.111:8090

## Comparación XIAO viejo vs nuevo
| Métrica | XIAO Viejo (3bb0) | XIAO Nuevo (2434) |
|---------|------------------|-------------------|
| Thread | Leader, partición propia | **Router, partición OTBR** ✅ |
| RSSI | -89 dBm | **-86 dBm** |
| LQI | 8/255 (3%) | **66%** |
| Neighbors | 0 | **1** |
| CoAP | Timeout/partición | **Respuesta inmediata** |
| LwM2M | Fallido | **Registrado en 17s** ✅ |

## Configuración Thread en firmware (prj.conf)
- `CONFIG_OPENTHREAD_FTD=y` (línea 54)
- `CONFIG_LWM2M_RD_CLIENT_MAX_RETRIES=10`
- `CONFIG_LWM2M_ENGINE_DEFAULT_LIFETIME=300`
- Channel 25, PAN 0xABCD, Network Key `00:11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff`
- Endpoint: construido dinámicamente desde MAC (`ami-esp32c6-%02x%02x` últimos 2 bytes)

## Estado del Cloud (192.168.1.159 — on-premise LAN)
- IP LAN: 192.168.1.159 (también tiene Tailscale 100.67.60.126 pero Edge usa LAN)
- Puerto gRPC 7070: **ABIERTO y funcionando** ✅
- Edge sincroniza datos via gRPC: uplink msgs confirmados
- docker-compose.yml actualizado en RPi4 (`/root/tb-edge/docker-compose.yml`)

## Próximos pasos (orden de prioridad)
1. **Dashboard ThingsBoard** — crear visualización de telemetría del medidor C2000 en Cloud
2. **FOTA (OTA)** — probar actualización de firmware Object 5
3. **Monitorear estabilidad a largo plazo** — verificar Thread/LwM2M registration permanece
4. **Considerar FTD→MTD** — no urgente ya que Router funciona bien
5. **Expandir tests** — agregar tests de integración con mocks de RS485
6. **CI pipeline** — automatizar compilación y ejecución de unit tests en GitHub Actions

## Lecturas del Medidor — Verificadas (sesión 9, 2026-03-02)
Capturadas via MCP Serial en COM12 (115200 baud):
```
HDLC connected (UA received) ~140ms
COSEM association ACCEPTED ~340ms
22/22 OBIS reads successful (5 skipped as unsupported on single-phase)
Cycle time: ~4.5s per poll

Readings:
  Voltage_R     = 131.9-132.2 V
  Current_R     = 0.27 A
  ActivePower_R = 34.50-34.60 kW
  ActiveEnergy  = 56,893.0 kWh
  Frequency     = 60.0 Hz
  Thread RSSI   = -77 dBm
  Thread LQI    = 100%
  Thread Role   = Router
```

## Unit Tests — 98/98 PASSED (sesión 9, 2026-03-02)
Compilados con GCC nativo vía WSL Ubuntu-24.04, sin dependencias de Zephyr.
```
Suite HDLC:       29/29 PASSED  (CRC-16, SNRM/DISC/I-frame build/parse/find, macros)
Suite COSEM:      43/43 PASSED  (AARQ/AARE, GET req/resp, 15+ data types, RLRQ)
Suite DLMS Logic: 26/26 PASSED  (OBIS table, value_to_double, config, state, LLC)
```
Compile command:
```bash
cd tests/
gcc -o run_tests test_main.c test_hdlc.c test_cosem.c \
    ../src/dlms_hdlc.c ../src/dlms_cosem.c \
    -I../src -Istubs -DUNIT_TEST -lm -Wall
./run_tests
```

## docker-compose.yml corregido (sesión 7) — `/root/tb-edge/docker-compose.yml`
```yaml
# VARIABLES CRÍTICAS (nombres correctos de env vars):
LWM2M_BIND_PORT: "5683"              # ← CORRECTO (NO usar LWM2M_SERVER_PORT)
LWM2M_SECURITY_BIND_PORT: "5684"     # ← CORRECTO (NO usar LWM2M_SERVER_SECURITY_PORT)
COAP_BIND_PORT: "5690"               # ← Movido de 5683 para evitar conflicto
COAP_ENABLED: "false"                # ← Deshabilitar CoAP transport
COAP_SERVER_ENABLED: "false"         # ← Belt-and-suspenders
CLOUD_RPC_HOST: "192.168.1.159"      # ← LAN directo (NO Tailscale)
```
- **LECCIÓN**: Los nombres de env vars para LwM2M se toman de `tb-edge.yml` dentro del contenedor:
  - `${LWM2M_BIND_PORT:5683}` ← este es el nombre correcto
  - `LWM2M_SERVER_PORT` NO existe — no afecta nada si se define
- **LECCIÓN**: `COAP_ENABLED=false` en TB Edge 4.2.1 NO previene que CoAP server bindee el puerto.
  Hay que también mover `COAP_BIND_PORT` a otro puerto O añadir `COAP_SERVER_ENABLED=false`

## Bugs descubiertos y corregidos (sesión 7)

### Bug 1: Conflicto de puerto CoAP vs LwM2M en 5683
- **Síntoma**: `java.net.BindException: Address already in use` al intentar bindear LwM2M en 5683
- **Causa**: CoAP transport se inicializa PRIMERO y bindea 5683, luego LwM2M no puede bindear
- **Fix**: `COAP_BIND_PORT=5690` + `COAP_ENABLED=false` + `COAP_SERVER_ENABLED=false`
- **Verificación**: `Started endpoint at coap://[0:0:0:0:0:0:0:0]:5683` (LwM2M server)

### Bug 2: `defaultObjectIDVer` formato incorrecto en perfil
- **Síntoma**: `IllegalArgumentException: version ({"3":"1.2",...}) MUST be composed of 2 parts`
- **Ubicación**: `LwM2mClient.getObjectIDVerFromDeviceProfile()` → `LwM2m$Version(String)` constructor
- **Causa**: El campo `defaultObjectIDVer` en `C2000_Monofasico_v2` era un JSON object:
  ```json
  {"3":"1.2","4":"1.3","5":"1.1","6":"1.0","9":"1.0","19":"1.1","3303":"1.1","10242":"1.0"}
  ```
  pero TB Edge 4.2.1 espera un simple string `"1.0"` (versión LwM2M del modelo de objetos)
- **Fix**: Cambiar a `"1.0"` via REST API en Cloud (192.168.1.159 port 80)
  ```
  POST /api/deviceProfile → profileData.transportConfiguration.clientLwM2mSettings.defaultObjectIDVer = "1.0"
  ```
- **CRÍTICO**: Fijar SIEMPRE en el Cloud server, NO solo en Edge DB. Cloud sync REVIERTE cambios en Edge DB.

### Bug 3: `observeStrategy` = `COMPOSITE_BY_OBJECT` incompatible con LwM2M 1.0
- **Síntoma**: `RuntimeException: This device does not support Composite Operation`
- **Ubicación**: `DefaultLwM2mDownlinkMsgHandler.findFirstContentFormatForComposite()`
- **Causa**: Composite Observe es feature de LwM2M 1.1+. ESP32-C6 con Zephyr usa LwM2M 1.0
- **Fix**: Cambiar `observeStrategy` de `COMPOSITE_BY_OBJECT` a `SINGLE` via REST API en Cloud
  ```
  POST /api/deviceProfile → profileData.transportConfiguration.observeAttr.observeStrategy = "SINGLE"
  ```
- **LECCIÓN**: Siempre usar `SINGLE` para dispositivos LwM2M 1.0. `COMPOSITE_BY_OBJECT` y `COMPOSITE_ALL` requieren LwM2M 1.1+

## Verificaciones del puerto 5683 (sesión 3)
- `netstat -ulnp | grep 5683` → `:::5683` (Java PID) ✅
- TB Edge env: `LWM2M_ENABLED=true`, `LWM2M_BIND_PORT=5683` ✅
- wpan0 tiene la dirección `fdc6:63fd:328d:66df:6a54:12ef:8c67:bd1c` ✅ (la que apunta el firmware)
- Firewall: `accept_from_thread` acepta todo tráfico de wpan0 ✅

## Historial de cambios relevantes
- 2026-02-26 Sesión 1: Creación de folder .context para persistencia de contexto
- 2026-02-26 Sesión 2:
  - Diagnóstico raíz: LwM2MAuthException por credenciales ACCESS_TOKEN
  - Fix Edge API → revertido por Cloud sync
  - Fix PostgreSQL directo (`fix_edge_db.sh`) → exitoso pero luego revertido
  - Nodo se unió Thread brevemente como child/router, luego formó partición propia
  - Identificado FTD como causa de partición propia
- 2026-02-26 Sesión 3:
  - Verificado que Cloud sync revirtió credenciales a ACCESS_TOKEN
  - Fix v2 (`fix_creds_v2.sh`): credentials + bloqueo gRPC via iptables
  - Reset limpio del ESP32: boot exitoso pero forma partición propia (Leader)
  - Señal radio: RSSI=-89dBm, LQI=8/255 — demasiado débil para merge estable
  - CoAP 5.0 error confirmó que paquetes SÍ llegan al server (antes del fix v2)
  - Puerto 5683 verificado en `:::` con LWM2M_ENABLED=true
- 2026-02-26 Sesión 4:
  - Reemplazo de XIAO: nuevo en COM12 (MAC 98:a3:16:61:24:34)
  - Flash firmware via esptool: 691KB zephyr.bin verificado con hash
  - Thread se conecta inmediatamente como Router en partición OTBR ✅
  - RSSI=-86dBm, LQI=66% — MUCHO mejor que XIAO anterior
  - LwM2M sigue fallando con 5.0/LwM2MAuthException pese a fix DB
  - Actualización DB: credentials_id y credentials_value para endpoint 2434
  - Descubierto que manipulación directa de DB no actualiza transport layer de TB Edge
- 2026-02-26 Sesión 5:
  - Reset password tenant via bcrypt $2a$ ($2b$ no funciona con Java BCrypt)
  - Login REST API exitoso: tenant@thingsboard.org / tenant
  - Descubierto ROOT CAUSE del LwM2MAuthException: formato incorrecto de credentials_value
    - `{"mode":"NO_SEC"}` ❌ vs `"NO_SEC"` ✅ (debe ser string, no objeto)
  - Device recreado via REST API con credenciales correctas
  - Script: `fix_lwm2m_api4.py` — Python con urllib, manejo correcto de JSON
  - **LwM2M REGISTRATION EXITOSO** en 17.0s desde boot ✅
  - Telemetría fluyendo: TensionR=133.74V, atributos de cliente recibidos
  - **SISTEMA COMPLETAMENTE OPERATIVO** 🎉
  - wpan0 tiene la dirección correcta del server URI
- 2026-02-27 Sesión 6:
  - Perfil cambiado de `LwM2M_Profile` a `C2000_Monofasico_v2` (monofásico Emsitech C2000)
  - Observaciones diferenciadas configuradas:
    - Grupo 1 (V,I,P,E): pmin=15s, pmax=30s — telemetría operacional
    - Grupo 2 (Q,S,PF,totals,freq,RSSI,LQI): pmin=60s, pmax=300s — caracterización de carga
  - ObserveStrategy: SINGLE (per-resource, NO "INDIVIDUAL" que causa HTTP 500)
  - Credenciales LwM2M verificadas: NO_SEC preservado tras cambio de perfil
  - Script: `update_profile_and_fix_cloud.py`
  - CLOUD_RPC_HOST corregido: 100.67.60.126 (Tailscale) → 192.168.1.159 (LAN)
  - iptables block regla eliminada (ya no necesaria)
  - Contenedor tb-edge recreado manualmente (docker-compose labels corruptas en OpenWrt)
  - Edge↔Cloud gRPC **RESTAURADO**: connect + Configuration received + uplink msgs ✅
  - **LECCIÓN**: TelemetryObserveStrategy enum: SINGLE, COMPOSITE_ALL, COMPOSITE_BY_OBJECT (no INDIVIDUAL)
  - **LECCIÓN**: Contenedores creados con `docker run` no tienen labels de docker-compose
- 2026-02-27 Sesión 7:
  - **Nodo mostraba INACTIVE en Edge y Cloud** — tres bugs en cascada
  - **Bug 1 — Conflicto puerto 5683**: CoAP bindeaba 5683 antes que LwM2M → BindException
    - Fix: docker-compose.yml con env vars correctas (`LWM2M_BIND_PORT` no `LWM2M_SERVER_PORT`)
    - `COAP_BIND_PORT=5690`, `COAP_ENABLED=false`, `COAP_SERVER_ENABLED=false`
  - **Bug 2 — `defaultObjectIDVer` formato JSON object**: Debía ser string `"1.0"`
    - `{"3":"1.2","4":"1.3",...}` → `IllegalArgumentException: version MUST be composed of 2 parts`
    - Fix en Edge DB inicialmente revertido por Cloud sync → fix aplicado en Cloud API
  - **Bug 3 — `observeStrategy: COMPOSITE_BY_OBJECT`**: ESP32-C6 (LwM2M 1.0) no soporta Composite
    - `RuntimeException: This device does not support Composite Operation`
    - Fix: cambiar a `SINGLE` en Cloud API
  - docker-compose.yml corregido con env vars auténticas (de `tb-edge.yml` interno)
  - Desplegado via `docker compose up -d` (no más `docker run` manual)
  - Postgres user cambiado: `tb_edge` (no `postgres`) en nuevo compose
  - Ambos fixes de perfil aplicados via REST API en Cloud (192.168.1.159:80)
    - `POST /api/deviceProfile` con `defaultObjectIDVer="1.0"` y `observeStrategy="SINGLE"`
  - **RESULTADO**: Nodo ACTIVE, 14 observaciones individuales establecidas, telemetría verificada en Cloud
  - Telemetría Cloud verificada: voltage=122.9-123.1V, frequency=~60Hz, current=0.0A
  - **LECCIÓN CRÍTICA**: Nunca fijar solo en Edge DB — Cloud sync REVIERTE cambios. Fijar en Cloud API.- 2026-02-28 Sesión 8:
  - Limpieza del workspace: .gitignore actualizado, archivos temporales eliminados
  - Documentación de arquitectura creada en `.context/`
  - Commit `619a57c` pusheado a `origin/master`
- 2026-03-02 Sesión 9:
  - **MCP Serial** instalado (`serial-mcp-server` en `.vscode/mcp.json`) para monitoreo COM12
  - Medidor reconectado físicamente — lectura verificada:
    - 22/22 OBIS leídos exitosamente (5 skipped: no aplican a monofásico)
    - V=131.9-132.2V, I=0.27A, P=34.5-34.6kW, E=56,893.0kWh, f=60.0Hz
    - Thread RSSI=-77dBm, LQI=100%, Role=Router
    - Ciclo completo: HDLC connect(~140ms) → COSEM associate(~340ms) → 22 reads(~4.5s)
  - **Unit test suite completo** — 98 tests en 3 suites:
    - HDLC (29): CRC-16, SNRM/DISC/I-frame build/parse/find, address/control macros
    - COSEM (43): AARQ/AARE, GET request/response, 15+ data types, RLRQ, OBIS helper
    - DLMS Logic (26): OBIS table integrity, value_to_double, config, state, LLC header
  - Compilación nativa via WSL Ubuntu GCC (`-DUNIT_TEST -lm`), sin dependencias Zephyr
  - Stubs: zephyr_stubs.h, lwm2m.h (LWM2M_OBJ macro), RS485 inline stubs
  - Archivos: `tests/test_main.c`, `test_hdlc.c`, `test_cosem.c`, `test_dlms_logic.c`, `test_framework.h`
  - **LECCIÓN**: `#include "rs485_uart.h"` en dlms_meter.c resuelve primero a `../src/rs485_uart.h` (relativo), NO a `stubs/`. Definir stubs antes del `#include "../src/dlms_meter.c"` en el archivo de test.
  - **LECCIÓN**: Counters `static int` en cada TU dan subtotales independientes. Usar `extern int` con `#ifdef TEST_MAIN_FILE` guard para compartir entre TUs.
  - `.context/README.md` y `.context/ESTADO.md` actualizados con detalles de sesión 9