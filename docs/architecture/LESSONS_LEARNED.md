# Lecciones Aprendidas — Sesiones 1-7

Documento que captura los problemas encontrados y sus soluciones durante el
desarrollo e integración del sistema AMI.

---

## 1. Puerto 5683 compartido entre LwM2M y CoAP (Sesión 7)

**Síntoma**: El nodo ESP32 no podía registrarse en TB Edge. Puerto 5683 occupied.

**Causa raíz**: ThingsBoard usa el mismo puerto 5683 tanto para el transporte
LwM2M como para el transporte CoAP genérico. Al iniciar, el primero que bindea
el puerto gana, y el otro falla silenciosamente.

**Solución**: En docker-compose.yml del Edge:
```yaml
LWM2M_BIND_PORT: "5683"        # LwM2M mantiene 5683
COAP_BIND_PORT: "5690"         # CoAP se mueve a otro puerto
COAP_ENABLED: "false"          # Mejor aún: deshabilitar CoAP si no se usa
```

**Lección**: Siempre verificar que no hay conflictos de puerto dentro del mismo
contenedor, especialmente con protocolos que comparten puerto default.

---

## 2. Formato defaultObjectIDVer — V vs VER (Sesión 7)

**Síntoma**: TB Edge observa los recursos pero nunca recibe Notify. Los Observe
llegan al nodo pero con paths malformados.

**Causa raíz**: El Device Profile en TB usa `defaultObjectIDVer` para mapear IDs
de objetos LwM2M. Hay dos formatos internos:
- **"V"** (correcto): `"3": "1.2"`, `"10242": "1.0"`
- **"VER"** (incorrecto): `"3_1.2"`, `"10242_1.0"`

Cuando Edge sincroniza con Cloud, el Cloud regenera el perfil y puede
sobreescribir el formato a VER, rompiendo el mapeo.

**Solución**: Modificar el perfil **siempre via Cloud REST API** (puerto 80),
nunca directamente en Edge. El Cloud es la fuente de verdad y propaga al Edge.

```python
# Ejemplo: API call al Cloud
PUT http://192.168.1.159:80/api/deviceProfile/{profileId}
X-Authorization: Bearer {jwt_token}
```

**Lección**: En arquitecturas Edge-Cloud, siempre modificar configuraciones en
el nivel más alto (Cloud) para evitar reversiones por sincronización.

---

## 3. ObserveStrategy COMPOSITE vs SINGLE (Sesión 7)

**Síntoma**: Edge envía Observe Request pero con lista de paths vacía. El nodo
responde con token desconocido.

**Causa raíz**: El perfil usaba `observeStrategy: COMPOSITE_BY_OBJECT`, que
intenta hacer Composite-Observe (RFC 9175). El cliente LwM2M de Zephyr no
soporta Composite Observe — responde con error y TB Edge descarta la sesión.

**Solución**: Cambiar `observeStrategy` a `SINGLE` en cada atributo del perfil.
Esto hace que TB Edge observe cada recurso individualmente, que sí es compatible
con el cliente Zephyr/Wakaama.

**Lección**: Verificar capacidades del cliente LwM2M antes de configurar
estrategias avanzadas de observación. SINGLE es la opción más compatible.

---

## 4. Conectividad Cloud — Tailscale vs LAN (Sesión 6)

**Síntoma**: Edge no puede conectar gRPC al Cloud. Timeout en conexión.

**Causa raíz**: Cloud estaba configurado con IP de Tailscale (100.67.60.126)
que no era accesible desde la RPi4 en la red local. Tailscale no estaba
instalado ni configurado en el RPi4.

**Solución**: Cambiar `CLOUD_RPC_HOST` a la IP LAN directa `192.168.1.159`.
Verificar con `nc -w3 -v 192.168.1.159 7070` desde el RPi4.

**Lección**: Para despliegues on-premise, usar IPs LAN directas. Las VPNs
overlay como Tailscale agregan complejidad innecesaria si todos los componentes
están en la misma red.

---

## 5. Hardware — XIAO ESP32-C6 defectuoso (Sesiones 3-5)

**Síntoma**: Señal Thread muy débil (RSSI < -95dBm), desconexiones frecuentes,
radio inestable.

**Causa raíz**: El primer XIAO ESP32-C6 tenía un defecto de fábrica en la
antena o el chip de radio.

**Solución**: Reemplazar por un segundo XIAO ESP32-C6. El nuevo dispositivo
mantiene RSSI de -86dBm estable con LQI de 66%.

**Lección**: Antes de debug extenso de firmware, considerar reemplazo de
hardware. Un módulo defectuoso puede desperdiciar días de troubleshooting.

---

## 6. Docker Host Networking obligatorio para Thread/IPv6 (Sesión 2)

**Síntoma**: TB Edge no recibe paquetes LwM2M del nodo Thread.

**Causa raíz**: Con Docker bridge networking, los paquetes IPv6 mesh-local
de Thread no llegan al contenedor. El OTBR escucha en interfaces del host,
pero Docker bridge crea una red aislada.

**Solución**: Usar `network_mode: host` en docker-compose.yml del Edge.

**Lección**: Para servicios que necesitan acceso a interfaces de red
específicas del host (Thread, 802.15.4, IPv6 link-local), Docker bridge
no funciona. Host networking es obligatorio.

---

## 7. PostgreSQL credentials mismatch (Sesión 5)

**Síntoma**: TB Edge falla al iniciar — no puede conectar a PostgreSQL.

**Causa raíz**: docker-compose.yml tenía `postgres` como usuario pero la
base de datos se inicializó con `tb_edge`.

**Solución**: Alinear credenciales en docker-compose.yml:
```yaml
SPRING_DATASOURCE_USERNAME: "tb_edge"
SPRING_DATASOURCE_PASSWORD: "tb_edge_pwd"
```
Y en el servicio postgres:
```yaml
POSTGRES_USER: "tb_edge"
POSTGRES_PASSWORD: "tb_edge_pwd"
POSTGRES_DB: "tb_edge"
```

**Lección**: Las credenciales de base de datos deben estar consistentes
entre el servicio que las crea (postgres) y el que las consume (tb-edge).

---

## Resumen de Herramientas Útiles

| Herramienta | Uso |
|-------------|-----|
| `tools/read_no_reset.py` | Monitor serial sin resetear ESP32 (evita RTS toggle) |
| `tools/quick_diag.py` | Diagnóstico rápido: Thread status + LwM2M registration |
| `tools/serial_diag.py` | Captura serial con timestamps y filtros |
| `nc -w3 -v HOST PORT` | Verificar conectividad TCP desde RPi4 |
| `docker logs tb-edge --tail 100` | Últimos logs del Edge |
| `ot-ctl state` | Estado del OTBR (leader/router/child) |
| `ot-ctl neighbor table` | Vecinos Thread visibles |
