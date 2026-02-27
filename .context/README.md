# AMI LwM2M Node — Project Context

> **LEE ESTE FOLDER COMPLETO** al iniciar un nuevo chat para entender el proyecto.

## Qué es este proyecto

Sistema de medición de energía inteligente (AMI — Advanced Metering Infrastructure) que conecta
medidores eléctricos a una plataforma IoT usando Thread mesh + LwM2M.

## Arquitectura end-to-end

```
┌─────────────┐  RS485/DLMS  ┌──────────────┐  Thread 802.15.4  ┌──────────────┐
│  Microstar  │──(9600 8N1)──│  XIAO ESP32  │───────────────────│    OTBR      │
│  3-Phase    │              │  C6 (Zephyr) │   Ch25, PAN ABCD  │  (RPi4)      │
│  Meter      │              │  COM13 (Win) │                   │  192.168.1.111│
└─────────────┘              └──────────────┘                   └──────┬───────┘
                                                                       │ IPv6 local
                                                                ┌──────▼───────┐
                                                                │ ThingsBoard  │
                                                                │ Edge 4.2.1   │
                                                                │ (Docker)     │
                                                                │ LwM2M:5683   │
                                                                │ HTTP:8090    │
                                                                └──────┬───────┘
                                                                       │ gRPC
                                                                ┌──────▼───────┐
                                                                │ ThingsBoard  │
                                                                │ Cloud Server │
                                                                │192.168.1.159 │
                                                                │ (LAN on-prem)│
                                                                └──────────────┘
```

## Componentes clave

### 1. Nodo IoT (este repositorio: `ami-lwm2m-node`)
- **Hardware**: Seeed XIAO ESP32-C6 + módulo RS485
- **SO/Framework**: Zephyr RTOS 4.2.0
- **Radio**: IEEE 802.15.4 (Thread)
- **Protocolo**: LwM2M client (NoSec, puerto 5683)
- **Objetos LwM2M**: 0 (Seguridad), 1 (Servidor), 3 (Dispositivo), 4 (ConnMon),
  5 (FOTA), 10242 (Medidor trifásico custom)
- **Endpoint**: `ami-esp32c6-XXXX` (generado desde MAC)
- **Puerto serial Windows**: COM12 (115200 baud)
- **Firmware version**: 0.12.0

### 2. OTBR (OpenThread Border Router)
- **Host**: RPi4 con OpenWrt (192.168.1.111)
- **Contenedor**: ot-br (si aplica) o servicio nativo OpenWrt
- **Red Thread**: "AMI-Pilot-2025", Canal 25, PAN ID 0xABCD
- **Network Key**: 00:11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff
- **Extended PAN ID**: 12:34:56:78:90:ab:cd:ef
- **Mesh-local prefix**: fdc6:63fd:328d:66df::/64

### 3. ThingsBoard Edge (¡NO Leshan standalone!)
- **Imagen Docker**: `thingsboard/tb-edge:4.2.1EDGE`
- **Contenedor**: `tb-edge` (host networking)
- **Puerto HTTP/API**: 8090 (no 8080, conflicto con dppd en OpenWrt)
- **Puerto LwM2M**: 5683/udp (transporte LwM2M integrado basado en Eclipse Leshan)
- **Puerto LwM2M DTLS**: 5684/udp
- **DB**: PostgreSQL 15 (contenedor `tb-edge-postgres`)
- **Credenciales**: tenant@thingsboard.org / tenant
- **Cloud connection**: gRPC a 192.168.1.159:7070 (LAN directo, NO Tailscale)

### 4. ThingsBoard Cloud Server
- **Host**: 192.168.1.159 (ThingsBoard 4.2.1.1, LAN on-premise)
- **Routing Key**: 1lg060jcvfp2tylc78mt
- **Routing Secret**: o1bcx4arcldnkjirru8n

## Credenciales Thread (prj.conf)
```
Channel: 25
PAN ID: 0xABCD (43981 dec)
Network Key: 00:11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff
Network Name: AMI-Pilot-2025
Extended PAN ID: 12:34:56:78:90:ab:cd:ef
```

## LwM2M Server URI (configurado en firmware)
```
coap://[fdc6:63fd:328d:66df:6a54:12ef:8c67:bd1c]:5683
```
Esta es la dirección mesh-local EID del OTBR. El nodo se conecta al ThingsBoard Edge
a través de esta dirección IPv6 Thread.

## Entorno de desarrollo

### Compilación (WSL Ubuntu-24.04)
```bash
cd /home/sebas/tesis-fw
source .venv/bin/activate
source zephyr/zephyr-env.sh
export ZEPHYR_SDK_INSTALL_DIR=/home/sebas/tesis-fw/zephyr-sdk-0.17.0
west build -b xiao_esp32c6/esp32c6/hpcore tesis/codigo/zephyr-app --pristine
```

### Flash (Windows PowerShell)
```powershell
python -m esptool --chip auto --port COM12 --baud 921600 --before default-reset --after hard-reset write-flash -u --flash_mode dio --flash-freq 80m --flash-size 4MB 0x20000 "C:\Users\jsgir\Documents\ESP32\build_out\zephyr.signed.confirmed.bin"
```

### Monitor serial
```powershell
python -m serial.tools.miniterm COM12 115200
```

### SSH al RPi4 (OTBR + Edge)
```powershell
ssh root@192.168.1.111
```

## Estructura del repositorio
```
ami-lwm2m-node/
├── .context/           ← ESTE FOLDER (contexto para IA)
├── src/
│   ├── main.c          ← Entry point, Thread join + LwM2M register
│   ├── lwm2m_obj_power_meter.c/h  ← Objeto 10242 (medidor trifásico)
│   ├── lwm2m_obj_thread_*.c/h     ← Objetos Thread (diagnóstico)
│   ├── dlms_*.c/h      ← Parser DLMS/COSEM para medidor Microstar
│   ├── rs485_uart.c/h  ← Driver RS485 half-duplex
│   └── firmware_update.c ← OTA (Object 5)
├── docs/               ← Documentación técnica
│   └── config_backups/  ← Perfiles, dashboard, docker-compose de Edge
├── prj.conf            ← Configuración Zephyr/Kconfig
├── CMakeLists.txt      ← Build system
└── boards/             ← Device tree overlays
```

## Device Profile LwM2M (ThingsBoard Edge)
El perfil `C2000_Monofasico_v2` mapea objetos LwM2M a telemetría ThingsBoard.
**ObserveStrategy: SINGLE** (cada recurso individualmente, NO COMPOSITE).

### Grupo 1 — Telemetría Operacional (pmin=15s, pmax=30s)
- `/10242/0/4` → TensionR (~123V)
- `/10242/0/5` → CorrienteR (0A)
- `/10242/0/6` → PotenciaActivaR
- `/10242/0/41` → EnergiaActivaTotal

### Grupo 2 — Caracterización de Carga (pmin=60s, pmax=300s)
- `/10242/0/7` → PotenciaReactivaR
- `/10242/0/8` → PotenciaAparenteR
- `/10242/0/9` → FactorPotenciaR

### Grupo 3 — Red (pmin=60s, pmax=300s)
- `/10242/0/40` → Frecuencia (~60Hz)
- `/3/0/*` → Device info
- `/4/0/*` → Connectivity
- `/5/0/*` → Firmware update

**IMPORTANTE**: defaultObjectIDVer debe ser formato "V" (ej: `1.2`) NO "VER" (ej: `1_1.2`).
Cambios al perfil deben hacerse via **Cloud REST API** (puerto 80) para evitar reversión por sync.

## Problemas resueltos (sesiones 1-7)
1. **Puerto 5683 conflicto**: LwM2M y CoAP default ambos usan 5683/udp → Solución: 
   `COAP_BIND_PORT=5690` + `COAP_ENABLED=false` en docker-compose.yml
2. **defaultObjectIDVer formato**: TB espera `"1.2"` (V), pero sync Cloud→Edge ponía `"3_1.2"` (VER) 
   → Solución: modificar perfil via **Cloud REST API puerto 80** (no Edge API)
3. **ObserveStrategy**: `COMPOSITE_BY_OBJECT` causaba Observe vacíos → Solución: `SINGLE`
4. **Cloud IP Tailscale**: 100.67.60.126 inalcanzable desde RPi4 → Solución: LAN directo 192.168.1.159
5. **Puerto HTTP Edge**: dppd ocupa 8080 → Edge usa **8090**

## Notas operativas
- LwM2M usa **NoSec** (security mode 3) — sin DTLS
- La red Thread usa host networking en Docker (no bridge) para acceso IPv6
- Si OTBR se reinicia, la dirección mesh-local EID puede cambiar → actualizar
  `CONFIG_NET_CONFIG_PEER_IPV6_ADDR` en prj.conf y recompilar
- API ThingsBoard Cloud: puerto **80** (no 8080)
- Credenciales PostgreSQL Edge: user=`tb_edge`, password=`tb_edge_pwd`, db=`tb_edge`

## Docker-compose env vars críticas (Edge)
```yaml
LWM2M_BIND_PORT: "5683"
LWM2M_SECURITY_BIND_PORT: "5684"
COAP_BIND_PORT: "5690"        # ← Evita conflicto con LwM2M
COAP_ENABLED: "false"          # ← Deshabilita CoAP innecesario
CLOUD_RPC_HOST: "192.168.1.159"  # ← LAN directo
```

## Flujo de datos (verificado end-to-end ✅)
1. Medidor → Nodo (RS485/DLMS): lee registros OBIS cada 30s
2. Nodo → OTBR (Thread/CoAP/LwM2M): envía via Object 10242
3. OTBR → TB Edge (IPv6 local): TB Edge recibe CoAP en puerto 5683
4. TB Edge: procesa con motor de reglas, persiste en PostgreSQL
5. TB Edge → Cloud (gRPC:7070): sincroniza telemetría a 192.168.1.159
