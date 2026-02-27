# Arquitectura del Sistema AMI — Smart Energy Metering

## Visión General

Sistema de medición inteligente (AMI — Advanced Metering Infrastructure) que conecta
medidores eléctricos industriales a una plataforma IoT cloud usando una red mesh
inalámbrica Thread 802.15.4 y el protocolo LwM2M.

## Diagrama de Arquitectura

```
┌─────────────────────┐
│   ThingsBoard Cloud  │   Capa 4: Plataforma Cloud
│   192.168.1.159:80   │   - Gestión centralizada de dispositivos
│   (CE 4.2.1.1)       │   - Dashboards, alarmas, analytics
└──────────┬───────────┘   - REST API para gestión de perfiles
           │ gRPC:7070
           │ (bidireccional)
┌──────────▼───────────┐
│   ThingsBoard Edge    │   Capa 3: Gateway Edge
│   RPi4 (OpenWrt)      │   - Procesamiento local (rule engine)
│   192.168.1.111:8090  │   - Persistencia PostgreSQL local
│   LwM2M: 5683/udp    │   - Sync bidireccional con Cloud
│   Docker containers   │   - Eclipse Leshan integrado (LwM2M server)
└──────────┬───────────┘
           │ IPv6 mesh-local
           │ CoAP/LwM2M (NoSec)
┌──────────▼───────────┐
│   OTBR (Border Router)│   Capa 2: Border Router
│   RPi4 (OpenWrt)      │   - Puente IPv6 Thread ↔ LAN
│   192.168.1.111       │   - Servicio nativo OpenWrt
│   Thread Leader       │   - mesh-local: fdc6:63fd:328d:66df::/64
└──────────┬───────────┘
           │ IEEE 802.15.4
           │ Thread mesh (Ch25, PAN 0xABCD)
┌──────────▼───────────┐
│   XIAO ESP32-C6       │   Capa 1: Nodo Sensor (este repositorio)
│   Zephyr RTOS 4.2.0   │   - Cliente LwM2M
│   OpenThread Router    │   - Driver RS485 half-duplex
│   ami-esp32c6-XXXX     │   - Parser DLMS/COSEM
└──────────┬───────────┘
           │ RS485 (9600 8N1)
           │ DLMS/COSEM
┌──────────▼───────────┐
│   Microstar C2000     │   Capa 0: Medidor Eléctrico
│   Medidor Monofásico  │   - Registros OBIS vía DLMS
│   RS485 slave         │   - Tensión, corriente, potencia, energía
└───────────────────────┘
```

## Capas del Sistema

### Capa 0: Medidor Eléctrico (Microstar C2000)
- **Comunicación**: RS485 half-duplex, 9600 baud, 8N1
- **Protocolo**: DLMS/COSEM (IEC 62056)
- **Datos disponibles**: Tensión, corriente, potencia activa/reactiva/aparente,
  factor de potencia, energía activa total, frecuencia
- **Polling**: Cada 30 segundos desde el nodo IoT

### Capa 1: Nodo IoT (ESP32-C6 — este repositorio)
- **MCU**: Espressif ESP32-C6 (RISC-V, radio 802.15.4 nativa)
- **Placa**: Seeed XIAO ESP32-C6
- **SO**: Zephyr RTOS 4.2.0
- **Radio**: IEEE 802.15.4 → Thread mesh → OpenThread Router
- **Protocolo IoT**: LwM2M client (Eclipse Wakaama via Zephyr)
- **Objetos LwM2M expuestos**:
  | Object ID | Nombre | Descripción |
  |-----------|--------|-------------|
  | 0 | Security | URI del servidor, modo NoSec |
  | 1 | Server | Lifetime, binding |
  | 3 | Device | Manufacturer, model, serial, firmware |
  | 4 | ConnMon | Signal strength, link quality, router |
  | 5 | Firmware | OTA update support |
  | 10242 | PowerMeter | Medidor monofásico (custom IPSO) |

### Capa 2: OTBR (OpenThread Border Router)
- **Hardware**: Raspberry Pi 4 con OpenWrt
- **Función**: Puente entre red Thread (802.15.4) y red IPv6/IPv4 LAN
- **Red Thread**: "AMI-Pilot-2025", Canal 25, PAN ID 0xABCD
- **Mesh-local prefix**: fdc6:63fd:328d:66df::/64
- **EID del OTBR**: fdc6:63fd:328d:66df:6a54:12ef:8c67:bd1c
- **OTBR es mismo host** que ThingsBoard Edge (RPi4)

### Capa 3: ThingsBoard Edge
- **Imagen**: `thingsboard/tb-edge:4.2.1EDGE` (Docker, host networking)
- **LwM2M Server**: Eclipse Leshan integrado en puerto 5683/udp
- **API HTTP**: puerto 8090 (8080 ocupado por dppd OpenWrt)
- **Base de datos**: PostgreSQL 15 (contenedor `tb-edge-postgres`)
- **Función**: 
  - Recibe datos LwM2M del nodo
  - Aplica reglas de procesamiento locales
  - Persiste telemetría en PostgreSQL
  - Sincroniza bidireccionalmente con Cloud via gRPC

### Capa 4: ThingsBoard Cloud
- **Host**: 192.168.1.159 (LAN on-premise)
- **Versión**: ThingsBoard CE 4.2.1.1
- **API**: Puerto 80
- **gRPC**: Puerto 7070 (conexión desde Edge)
- **Función**:
  - Gestión centralizada de dispositivos y perfiles
  - Dashboards de visualización
  - Los cambios de perfil LwM2M deben hacerse aquí (Cloud REST API)
    para que se propaguen al Edge sin reversión

## Protocolo LwM2M — Detalles

### Registro y Observación
1. Nodo arranca → se une a Thread mesh (~11s)
2. Envía LwM2M Register a `coap://[OTBR_EID]:5683` (~17s total)
3. TB Edge acepta registro → marca dispositivo ACTIVE
4. Edge configura Observe en recursos del perfil `C2000_Monofasico_v2`
5. Nodo envía Notify periódicamente según pmin/pmax configurados

### Modelo de Observación (ObserveStrategy: SINGLE)
**IMPORTANTE**: NO usar COMPOSITE_BY_OBJECT — causa Observe vacíos en Zephyr.

| Grupo | Recursos | pmin | pmax | Uso |
|-------|----------|------|------|-----|
| Telemetría Operacional | TensionR, CorrienteR, PotActivaR, EnergiaTotal | 15s | 30s | Monitoreo en tiempo real |
| Caracterización de Carga | PotReactiva, PotAparente, FactorPotencia | 60s | 300s | Análisis de calidad |
| Red y Sistema | Frecuencia, Device info, Connectivity, Firmware | 60s | 300s | Diagnóstico |

### Formato de Object Version (defaultObjectIDVer)
El perfil LwM2M debe usar formato **"V"** (`"1.2"`, `"1.0"`, etc.)  
**NUNCA** formato "VER" (`"3_1.2"`, `"10242_1.0"`) — causa mismatch en el registro.

## Comunicación RS485 / DLMS

### Protocolo
- RS485 half-duplex con control DE/RE via GPIO
- DLMS/COSEM (IEC 62056) sobre HDLC
- Slave address: 1 (medidor), Client: 0x10

### Registros OBIS Leídos
| OBIS Code | Magnitud | Unidad |
|-----------|----------|--------|
| 1.0.32.7.0 | Tensión fase R | V |
| 1.0.31.7.0 | Corriente fase R | A |
| 1.0.21.7.0 | Potencia activa R | W |
| 1.0.23.7.0 | Potencia reactiva R | var |
| 1.0.29.7.0 | Potencia aparente R | VA |
| 1.0.33.7.0 | Factor de potencia R | - |
| 1.0.1.8.0 | Energía activa total | Wh |
| 1.0.14.7.0 | Frecuencia | Hz |

## Deployment — Docker Compose (Edge)

Ver [config_backups/docker-compose.yml](../config_backups/docker-compose.yml) para
el archivo completo. Variables de entorno críticas:

```yaml
environment:
  CLOUD_ROUTING_KEY: "1lg060jcvfp2tylc78mt"
  CLOUD_ROUTING_SECRET: "o1bcx4arcldnkjirru8n"
  CLOUD_RPC_HOST: "192.168.1.159"     # LAN directo (NO Tailscale)
  LWM2M_BIND_PORT: "5683"
  LWM2M_SECURITY_BIND_PORT: "5684"
  COAP_BIND_PORT: "5690"              # Diferente de LwM2M para evitar conflicto
  COAP_ENABLED: "false"               # CoAP deshabilitado (solo usamos LwM2M)
  SPRING_DATASOURCE_USERNAME: "tb_edge"
  SPRING_DATASOURCE_PASSWORD: "tb_edge_pwd"
```

## Credenciales

| Componente | Usuario | Contraseña | Notas |
|------------|---------|------------|-------|
| TB Edge Web | tenant@thingsboard.org | tenant | HTTP 8090 |
| TB Cloud API | tenant@thingsboard.org | tenant | HTTP 80 |
| PostgreSQL Edge | tb_edge | tb_edge_pwd | DB: tb_edge |
| RPi4 SSH | root | (key-based) | 192.168.1.111 |

## Thread Network

| Parámetro | Valor |
|-----------|-------|
| Network Name | AMI-Pilot-2025 |
| Channel | 25 |
| PAN ID | 0xABCD |
| Network Key | 00:11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff |
| Extended PAN ID | 12:34:56:78:90:ab:cd:ef |
| Mesh-local prefix | fdc6:63fd:328d:66df::/64 |
