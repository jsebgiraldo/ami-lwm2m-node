# AMI Node — Guía de Aprovisionamiento de Fábrica

> Cómo sincronizar un nodo nuevo (salido de fábrica) con ThingsBoard Edge y Cloud.

---

## Contexto del Sistema

```
[Medidor]──RS485──[XIAO ESP32-C6]──Thread 802.15.4──[OTBR+TB Edge RPi4]──gRPC──[TB Cloud]
                   (este nodo)                        192.168.1.111:5683/udp     192.168.1.159
```

Cuando el nodo arranca, hace **LwM2M Registration** hacia el servidor Edge en
`coap://[fdc6:63fd:328d:66df:6a54:12ef:8c67:bd1c]:5683`.  
**Si el dispositivo no existe en TB Edge, la conexión es rechazada** (o ignorada), y el
nodo no envía datos.

Por eso, **antes de desplegar cualquier nodo nuevo** hay que registrarlo en ThingsBoard.

---

## Concepto: ¿Cómo se identifica un nodo?

El nodo construye su identidad LwM2M en el arranque con la función `build_endpoint_name()`
de `src/main.c`:

```c
// src/main.c  líneas 341-353
snprintf(endpoint_name, sizeof(endpoint_name),
         "ami-esp32c6-%02x%02x",
         link->addr[link->len - 2],   // MAC byte -2
         link->addr[link->len - 1]);  // MAC byte -1
```

### Ejemplo real
| Campo | Valor |
|-------|-------|
| MAC completa (etiqueta HW) | `98:A3:16:61:24:34` |
| Últimos 2 bytes | `0x24`, `0x34` |
| **Endpoint LwM2M** | **`ami-esp32c6-2434`** |

> La MAC se puede leer:
> - De la **etiqueta física** impresa en el módulo (parte inferior del XIAO)
> - Del **monitor serie** al arrancar: `LOG_INF("Endpoint: %s", endpoint_name)`
> - Usando `esptool.py flash_id --port COMx` antes de flashear el firmware

---

## Lo que contiene el perfil de dispositivo

El nodo usa el perfil **`C2000_Monofasico_v2`** (en TB Edge `b6d55c90-12db-11f1-b535-433a231637c4`).

Este perfil define:
- **Observe** (16 recursos): voltaje, corriente, potencias, energías, frecuencia, RSSI, LQI
- **Attributes** (3): manufacturer, modelNumber, serialNumber — se leen una sola vez
- **Telemetry** (13): los mismos recursos, almacenados como series de tiempo en PostgreSQL
- **Transporte**: LwM2M NoSec (sin cifrado, sin bootstrap)

El Edge sincroniza automáticamente este perfil con TB Cloud vía gRPC — no hay que
replicar el perfil manualmente en Cloud.

---

## Estrategias de aprovisionamiento

### Opción A — Script REST (recomendada para pilotos ≤ 100 nodos)

**Cuándo usarla**: control total, auditoría de qué nodos están registrados.

```
                    ┌────────────────────────────────────┐
Operador            │  python provision_node.py          │
  ──[MAC label]──►  │  --mac 98:a3:16:61:24:34           │
                    └──────────────┬─────────────────────┘
                                   │ REST POST /api/device
                                   ▼
                    ┌────────────────────────────────────┐
                    │  ThingsBoard Edge :8090            │
                    │  Crea device "ami-esp32c6-2434"    │
                    │  Perfil: C2000_Monofasico_v2       │
                    │  Creds: LWM2M NoSec                │
                    └──────────────┬─────────────────────┘
                                   │ gRPC sync automático
                                   ▼
                    ┌────────────────────────────────────┐
                    │  ThingsBoard Cloud :80             │
                    │  Dispositivo replicado             │
                    └────────────────────────────────────┘

Al arrancar el nodo:
  LwM2M Register → Edge acepta → ACTIVE → datos fluyen
```

### Opción B — Auto-provisioning (para despliegues masivos)

ThingsBoard soporta `ALLOW_CREATE_NEW_DEVICES`, donde **cualquier endpoint nuevo que
se registre es auto-creado** bajo el perfil que tiene esta opción habilitada.

> ⚠️  Con LwM2M NoSec sin Bootstrap, esto se habilita en ThingsBoard 4.x configurando
> `provisionType: ALLOW_CREATE_NEW_DEVICES` + una `provisionDeviceKey` en el perfil.
> El nodo NO necesita saber la clave — sólo necesita tener el endpoint correcto.
> **CAVEAT**: cualquier dispositivo con cualquier endpoint puede auto-registrarse si
> conoce el servidor → sólo usar en redes controladas (Thread mesh lo es).

Habilitación vía REST (ver sección "Administración avanzada").

### Opción C — Manual desde la UI Web

Para un nodo puntual: `TB Edge UI` → Entities → Devices → `+` → nombre=`ami-esp32c6-XXXX`,
perfil=`C2000_Monofasico_v2`. Luego ir a Credentials y seleccionar tipo LwM2M.

---

## Pasos detallados — Método A (Script)

### Prerrequisitos

```bash
pip install requests
```

El script está en `tools/provision_node.py`.

### Paso 1 — Obtener la MAC del nodo nuevo

**Desde etiqueta física** (recomendado en manufactura):
El módulo XIAO ESP32-C6 tiene la MAC impresa en la parte inferior (`Wi-Fi/BT addr`).
La MAC del Thread usa los mismos últimos 2 bytes.

**Desde monitor serie** (si ya está flasheado):
```
Conectar con minicom/PuTTY a 115200 baud. Salida al arrancar:
  *** AMI MAIN ENTRY ***
  *** Firmware: v0.16.0 ***
  ...
  [INF] Thread attached! Role=2 after 8s
  [INF] Endpoint: ami-esp32c6-2434      ← usar este valor
```

**Desde esptool** (antes de flashear):
```bash
python -m esptool --port COM11 flash_id
# Muestra: MAC: 98:a3:16:61:24:34
```

### Paso 2 — Ejecutar el script

```bash
# Desde el directorio raíz del repo
python tools/provision_node.py --mac 98:a3:16:61:24:34
```

Salida esperada:
```
============================================================
  AMI Node Provisioner
  Target : http://192.168.1.111:8090
  Profile: C2000_Monofasico_v2
  Nodes  : 1
  Action : PROVISION
============================================================
  [OK] Authenticated as tenant@thingsboard.org

──────────────────────────────────────────────────────────
  Endpoint : ami-esp32c6-2434
  Profile  : C2000_Monofasico_v2  (b6d55c90...)
  [OK] Device created: cc9da070-135b-11f1-80f9-cdb955f2c365
  [OK] Credentials set: LWM2M_CREDENTIALS / NO_SEC / endpoint=ami-esp32c6-2434

============================================================
  SUMMARY: 1 total | 1 created | 0 already existed | 0 errors
============================================================
```

### Paso 3 — Flashear el firmware al nodo

Si no lo has hecho aún:
```powershell
.\build_flash.ps1 -Flash -Port COM11
```

El firmware es el mismo para **todos los nodos** — no hay parametrización por dispositivo.
Las únicas diferencias entre nodos son:
- Endpoint (derivado de MAC, automático)
- Dirección IPv6 derivada del EUI-64 del radio Thread (automática)

### Paso 4 — Encender y verificar

El nodo tardará ~17 segundos en:
1. Unirse a la red Thread (credenciales hardcoded en `prj.conf`)
2. Obtener dirección IPv6 mesh-local
3. Registrarse con LwM2M en el Edge

Verificar con el script:
```bash
python tools/provision_node.py --mac 98:a3:16:61:24:34 --verify
```

Salida esperada:
```
──────────────────────────────────────────────────────────
  Endpoint : ami-esp32c6-2434
  Device ID   : cc9da070-135b-11f1-80f9-cdb955f2c365
  Active      : True
  Profile     : C2000_Monofasico_v2
  Cred type   : LWM2M_CREDENTIALS
  Cred ID     : ami-esp32c6-2434
  Telemetry   : voltage = 124.84
  Telemetry   : current = 0.0
  Telemetry   : activePower = 0.0
```

Si `Active: False` después de 30 segundos, ver sección Troubleshooting.

---

## Aprovisionamiento por lotes (CSV)

Para N nodos al mismo tiempo:

1. Crear archivo `nodos_lote.csv`:
```csv
mac,ubicacion,instalado_por
98:a3:16:61:24:34,Apto-101,JSG
AA:BB:CC:DD:EE:FF,Apto-102,JSG
11:22:33:44:55:66,Apto-103,JSG
```

2. Ejecutar:
```bash
python tools/provision_node.py --csv nodos_lote.csv
```

El script es **idempotente** — si el dispositivo ya existe, lo salta (`[SKIP]`).

---

## Estados en ThingsBoard

| Estado | Descripción | Qué significa |
|--------|-------------|---------------|
| `active: false` | No conectado | Dispositivo creado pero sin LwM2M registration activa |
| `active: true` | Conectado | LwM2M registration vigente (lifetime=300s, renueva cada ~270s) |
| No aparece | No aprovisionado | El nodo no puede conectar → ejecutar provision_node.py primero |

---

## Pasos completos de fábrica a producción

```
FÁBRICA                          CAMPO/LABORATORIO
─────────────────────────────    ────────────────────────────────────────────────
1. Soldar/ensamblar XIAO +       4. Conectar adaptador RS485 al medidor
   RS485 expansion board            (A/B/GND, 9600 8N1 half-duplex)

2. Leer MAC del módulo           5. Ejecutar provision_node.py --mac XX:XX...
   (etiqueta o esptool)             → Registra en TB Edge/Cloud

3. Flashear firmware             6. Encender nodo
   build_flash.ps1 -Flash           → OpenThread join (~8s)
   -Port COMx                       → LwM2M register (~17s)
                                    → TB Edge marca ACTIVE

                                 7. Verificar datos en dashboard
                                    o: provision_node.py --verify

                                 8. (Opcional) Asignar a cliente/asset en TB Cloud
```

---

## Parámetros que distinguen un nodo de otro

> **Todo está en el firmware binario compilado una sola vez** salvo:

| Parámetro | Dónde se determina | Nota |
|-----------|-------------------|------|
| Endpoint LwM2M | En runtime, de la MAC | Único por hardware, automático |
| Dirección IPv6 Thread | En runtime, de EUI-64 radio | Único por hardware, automático |
| Thread Network Key | Hardcoded en `prj.conf` | **¡Mismo para todos los nodos del piloto!** |
| LwM2M Server URI | Hardcoded en `prj.conf` | Mismo para todos |
| Perfil de datos | Configurado en TB Edge | Aplica igual a todos |

> Para producción multi-red o multi-cliente: parametrizar el network key y server URI
> via NVS (flash settings) o OTA config push. Ver sección "Roadmap".

---

## Perfil LwM2M — Referencia rápida

### Recursos observados

| Path | Key telemetría | Descripción |
|------|---------------|-------------|
| `/10242_1.0/0/4` | `voltage` | Tensión fase R (V) |
| `/10242_1.0/0/5` | `current` | Corriente fase R (A) |
| `/10242_1.0/0/6` | `activePower` | Potencia activa R (kW) |
| `/10242_1.0/0/7` | `reactivePower` | Potencia reactiva R (kvar) |
| `/10242_1.0/0/10` | `apparentPower` | Potencia aparente R (kVA) |
| `/10242_1.0/0/11` | `powerFactor` | Factor de potencia R |
| `/10242_1.0/0/39` | `totalPowerFactor` | FP total |
| `/10242_1.0/0/41` | `activeEnergy` | Energía activa total (kWh) |
| `/10242_1.0/0/42` | `reactiveEnergy` | Energía reactiva (kvarh) |
| `/10242_1.0/0/45` | `apparentEnergy` | Energía aparente (kVAh) |
| `/10242_1.0/0/49` | `frequency` | Frecuencia (Hz) |
| `/4_1.3/0/2` | `radioSignalStrength` | RSSI 802.15.4 (dBm) |
| `/4_1.3/0/3` | `linkQuality` | LQI enlace Thread |

### Atributos (sin observe, lectura única)

| Path | Key | Descripción |
|------|-----|-------------|
| `/3_1.2/0/0` | `manufacturer` | "Tesis-AMI" |
| `/3_1.2/0/1` | `modelNumber` | "XIAO-ESP32-C6" |
| `/3_1.2/0/2` | `serialNumber` | "AMI-001" |

---

## Administración avanzada

### Habilitar auto-provisioning (Opción B)

Si se quiere que cualquier nodo AMI se auto-registre sin pre-provisioning manual:

```bash
# 1. Leer el perfil actual
curl -s -H "Authorization: Bearer $TOKEN" \
  http://192.168.1.111:8090/api/deviceProfile/b6d55c90-12db-11f1-b535-433a231637c4 \
  > /tmp/profile.json

# 2. Editar: cambiar provisionType y agregar provisionDeviceKey
#    "provisionType": "ALLOW_CREATE_NEW_DEVICES",
#    "provisionDeviceKey": "ami-lwm2m-provision-key-2025",

# 3. Actualizar vía PUT
curl -s -X PUT -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d @/tmp/profile_updated.json \
  http://192.168.1.111:8090/api/deviceProfile
```

> Con `ALLOW_CREATE_NEW_DEVICES` el nodo se crea automáticamente al primer registro
> LwM2M. El dispositivo hereda el perfil configurado como DEFAULT (o el primero que
> tiene provisioning habilitado). Para asegurar el perfil correcto, marcar
> `C2000_Monofasico_v2` como perfil DEFAULT (`"default": true`).

### Exportar la lista de nodos aprovisionados

```bash
python tools/provision_node.py --csv - --verify  # Futura: --list all
```

Por ahora, via API:
```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://192.168.1.111:8090/api/tenant/deviceInfos?pageSize=100&page=0" \
  | python -c "import sys,json; d=json.load(sys.stdin); [print(x['name'],x.get('active')) for x in d['data']]"
```

### Borrar un nodo del sistema (retiro o reemplazo)

```bash
python tools/provision_node.py --mac 98:a3:16:61:24:34 --delete
```

---

## Troubleshooting

| Síntoma | Causa probable | Solución |
|---------|----------------|---------|
| `active: false` después de 60s | Nodo no conectó a Thread | Verificar canal/PAN/NetworkKey en prj.conf |
| `active: false` + Thread OK | LwM2M registration falló | Verificar endpoint en TB; revisar logs serie del nodo |
| Endpoint aparece pero sin datos | Perfil mal configurado | Verificar profileData → observeAttr en TB |
| `Timeout` al provisionar | Edge no accesible | Verificar Docker: `docker ps` en RPi4 |
| `Profile not found` | Perfil borrado o renombrado | Recrear perfil desde `docs/config_backups/c2000_monophase_profile.json` |
| Datos numéricos = 0 | Meter RS485 no conectado | Verificar cableado A/B/GND y dirección HDLC |

### Ver logs del nodo en tiempo real

```powershell
# En monitor serie (115200, COM11)
python -m serial.tools.miniterm COM11 115200
```

Secuencia de arranque normal:
```
*** AMI MAIN ENTRY ***
*** Firmware: v0.16.0 ***
[INF] Waiting for Thread network...
[INF] Thread attached! Role=2 after 8s
[INF] Extra 5s wait for IPv6 addresses...
[INF] Endpoint: ami-esp32c6-2434
[INF] LwM2M objects configured
[INF] Server: coap://[fdc6:63fd:328d:66df:6a54:12ef:8c67:bd1c]:5683
[INF] LwM2M RD client started
[INF] LwM2M client registered (session 0x...)
[INF] DLMS meter poll OK: V=124.8 I=0.00 P=0.0W
```

### Ver estado del Edge

```bash
# SSH al RPi4
ssh root@192.168.1.111
docker ps  # tb-edge y tb-edge-postgres deben estar UP
docker logs tb-edge 2>&1 | tail -50
```

---

## Roadmap — Para producción multi-cliente

Cuando el piloto escale a múltiples redes Thread (diferentes edificios/clientes):

1. **Parametrizar Thread Network Key por instalación**: Usar NVS en flash para
   almacenar/sobrescribir el dataset Thread sin recompilar el firmware.

2. **Parametrizar LwM2M Server URI**: El servidor Edge puede cambiar per-deployment;
   leer URI desde NVS en lugar de `prj.conf`.

3. **Mecanismo de comisionamiento Thread**: En lugar de un network key universal,
   usar Thread Commissioner (otbr-agent + ot-ctl) para comisionar cada nodo
   individualmente con una credencial temporal.

4. **TB Cloud multi-tenant**: Cada cliente tiene su propio tenant en TB Cloud;
   el aprovisionamiento script debe recibir `--tenant` como argumento.

5. **OTA (Object 5)**: El firmware ya soporta Object 5 (Firmware Update). Subir
   imágenes a TB OTA Package y hacer push desde el perfil del dispositivo.
