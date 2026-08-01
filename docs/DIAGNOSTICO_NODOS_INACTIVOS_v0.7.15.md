# Nodos inactivos: causa raíz + toolchain de diagnóstico y fix (v0.7.15-diag)

**Fecha:** 2026-07-15
**Firmware:** `0.7.15-diag` (sube desde `0.7.14-otacfm`)
**Ámbito:** flota AMI ESP32-C6 / LwM2M-sobre-OpenThread → TB Edge (`192.168.1.111:8090`) → TB Server central (stack Docker local `192.168.1.159`).

---

## 1. Causa raíz de "muchos nodos inactivos"

**No es un bug de firmware ni de la infraestructura. Es inestabilidad de ALIMENTACIÓN (brownout) a nivel de flota.**

Evidencia recogida durante varios días de operación:

| Señal | Medición | Lectura |
|---|---|---|
| Uptime de los nodos "activos" | mediana **54 s**, todos < 5 min | se reinician constantemente |
| `reset_cause` de la flota | **POR=27 / SOFTWARE=11 / WATCHDOG=9** | dominan los *power-on reset* (POR = corte de alimentación) |
| Infra central (Kafka, Postgres, tb-core/rule-engine) | sana ≥ 2 días | el uplink NO es el problema |
| Edge + OTBR + malla Thread | routers estables (~10), sin churn de partición | la malla NO es el problema |
| `recover_count` | alto y en aumento | **síntoma** de los reinicios, no la causa |

**Mecanismo:** ~47 nodos comparten PSU. Con ~150 mA/nodo el arranque simultáneo demanda ~7 A → **brownout** → todos hacen POR a la vez → todos re-atacan la malla y re-registran en LwM2M **en el mismo instante** → contención en leader/edge → algunos fallan el primer registro → reboot → se repite la tormenta. El `recover_count` que veíamos subir es la *huella* de esa tormenta, no un fallo de la lógica de recuperación.

**Conclusión:** el arreglo de fondo es de hardware (PSU con margen de corriente / arranque escalonado / desacoplo). El firmware no puede crear energía, pero **sí puede dejar de amplificar la tormenta** — ver §4 (Task 2b).

---

## 2. Task 1 — `tools/node_doctor.py`: diagnóstico 100 % automatizado

Un solo comando: **DETECT → DIAGNOSE → DOCTOR → FIX → VERIFY**, deja los nodos operando.

```bash
python tools/node_doctor.py                 # ciclo completo sobre lo conectado al PC
python tools/node_doctor.py --dry-run        # diagnostica y decide, sin flashear ni provisionar
python tools/node_doctor.py --coms COM76,COM82   # acota a puertos concretos
python tools/node_doctor.py --no-verify      # salta el re-chequeo final contra TB
```

Etapas:

1. **DETECT** — enumera los ESP32-C6 por puerto serie (USB CDC), deriva el sufijo MAC → `ami-esp32c6-<suffix>`.
2. **DIAGNOSE** — abre la consola (`ami status`, `kernel uptime`, `ot state`) y cruza con TB (¿provisionado? ¿`active`? ¿última telemetría?). Robusto a **puertos colgados**: cada sonda serie corre bajo un wrapper con *timeout* por hilo (`_timeout_call`, 14 s) para que un CDC muerto no cuelgue toda la corrida.
3. **DOCTOR** — clasifica priorizando el estado real en TB:
   - `active` en TB → **HEALTHY** (o `ACTIVE_OLD_FW` si el fw no es `FW_LATEST`) — no se toca aunque la consola esté muda.
   - No provisionado → **NOT_PROVISIONED**.
   - Ni consola ni TB → **STUCK_SILENT** → candidato a reflash / power-cycle.
4. **FIX** — reflash `esptool write-flash --erase-all` (mcuboot @0x0 + `zephyr.signed.bin` @0x20000) y/o `tb_edge_provision.py`, según el veredicto.
5. **VERIFY** — reconsulta TB para confirmar `active` tras el fix.

`FW_LATEST` está fijado a `0.7.15-diag` (sincronizado con `CLIENT_FIRMWARE_VER` en `src/main.c`).

---

## 3. Task 2a — GET instantáneo por IPv6 (independiente de LwM2M/CoAP-observe)

**Problema que resuelve:** cuando un nodo cae de TB pero sigue en la malla, no había forma de preguntarle su estado directamente — solo esperar a que la observabilidad LwM2M se recuperase. Ahora el servidor puede **consultar el dispositivo por IPv6 bajo demanda**.

### Firmware — `src/coap_diag.c`
Servidor CoAP minúsculo en `[::]:5685/diag` (auto-arranca con la red, hilo propio). Un GET devuelve un snapshot JSON en vivo, **sin depender de la sesión LwM2M**:

```
coap://[<ipv6-del-nodo>]:5685/diag
  -> {"fw":"0.7.15-diag","role":"Router","uptime_s":1234,"resets":7,
      "reg_ok":3,"recover":5,"wdog":0,"boot_burst":0,
      "partition":123456,"rloc16":"0x4c00"}
```

Wiring: `CMakeLists.txt` (fuente + sección iterable `coap_resource_diag_service` en `sections-ram.ld` y `zephyr_iterable_section`), `prj.conf` (`CONFIG_COAP_SERVER=y`, `CONFIG_ZVFS_OPEN_MAX=16`), y `coap_diag_init(CLIENT_FIRMWARE_VER)` en `main.c` tras `thread_role_init()`.

Distingue **"alcanzable pero no registrado"** de **"fuera de la malla"**: si responde el GET, el nodo vive y está en Thread aunque LwM2M esté caído.

### Servidor — `tools/diag_get.py`
Cliente CoAP crudo (UDP/IPv6, **sin dependencias** más allá de stdlib). Por defecto lanza el GET **desde este host**, que funciona desde cualquier máquina del LAN que reciba la ruta del prefijo OMR que anuncia el OTBR (el servidor central y los portátiles de operación la reciben — verificado: `ping -6` al OMR del nodo responde a ~35 ms).

```bash
# Consultar el/los nodo(s) por su dirección OMR (fdxx:...):
python tools/diag_get.py --addr <omr-nodo-1> --addr <omr-nodo-2>

# Salida JSON (para scripts / health-checks):
python tools/diag_get.py --addr <omr-nodo> --json
```

Detalles clave (aprendidos en la verificación):
- **Apuntar a la dirección OMR** del nodo (mismo prefijo que el server LwM2M). Las mesh-local/RLOC (`fd32:...`) solo enrutan *dentro* de la malla / desde el propio OTBR.
- La petición es **CON** (confirmable) con **retransmisión** (hasta 4 intentos). El `coap_server` de Zephyr responde a CON con ACK piggyback; a NON no responde de forma fiable, y el primer paquete en la malla se pierde de vez en cuando.
- `--via-otbr` / `--enumerate` (SSH al OTBR) existen como alternativa, **pero este OTBR (pi4/EKH01) no trae `python3`** — el camino que funciona aquí es el local con ruta OMR. `ot-ctl coap` del OTBR solo alcanza el puerto CoAP por defecto (5683), no el 5685 del diag.

---

## 4. Task 2b — fix de firmware en el punto débil real

**Punto débil:** el hook *eager-reattach* en `main.c` (`thread_state_changed_cb`) disparaba `recover_work` con `K_NO_WAIT` en cada transición `DETACHED → CHILD/ROUTER`. Es correcto para **un** nodo (aprovechar la malla recién recuperada), pero a escala de flota, tras un brownout compartido **todos** los nodos ven `DETACHED→CHILD` en el mismo segundo y golpean DNS-SD + `rd_client_start` **a la vez** → tormenta de re-registro sincronizada contra la malla/edge que apenas se recuperan (exactamente la firma del brownout de 47 nodos).

**Fix (conservador):** sustituir `K_NO_WAIT` por un jitter aleatorio acotado:

```c
uint32_t eager_jitter_ms = sys_rand32_get() % 3000U;   /* 0–3 s */
k_work_reschedule(&lwm2m_recover_work, K_MSEC(eager_jitter_ms));
```

Efecto: el re-attach sigue siendo prácticamente inmediato por nodo (≤ 3 s), pero la flota se **descorrelaciona** — se rompe la tormenta. No toca la máquina de estados de registro/recuperación (que ya tiene backoff + jitter + mesh-gating maduros); solo desincroniza el arranque en caliente.

---

## 5. Cómo operar de aquí en adelante

1. **Hardware primero:** dar margen de corriente a la PSU o escalonar el encendido. Es la causa raíz.
2. **Flashear la flota** a `0.7.15-diag` (incluye 2a + 2b) con el flujo habitual (`node_doctor.py` por PC, o el deployer OTA por lotes).
3. **Salud continua:** `node_doctor.py --dry-run` para triage por PC; `diag_get.py --enumerate` para preguntar por IPv6 a los que caen de TB.
4. Un nodo que responde `/diag` pero no está `active` en TB = problema de **registro/uplink**, no de nodo muerto. Un nodo que ni responde `/diag` ni consola = candidato real a reflash/power-cycle.

## Verificación end-to-end (2026-07-15)

Con 2 nodos conectados al PC (COM76 = c5d0, COM82 = c5cc):

- **COM76 / c5d0** — flasheado a `0.7.15-diag` (ambos hashes verificados). Arranca y opera: `ami status` = **ALL OK**, Thread `CHILD`, **LwM2M registered**, DLMS leyendo (V=122 V, f≈60 Hz). Activo en TB con telemetría fresca (`recover_count=0`, `watchdog_count=0`). El **GET `/diag` por IPv6 funciona** desde el servidor:
  ```
  $ python tools/diag_get.py --addr fda0:13c7:aa71:1:25ae:b05b:bc98:86f9
  /diag over IPv6  --  1/1 reachable  (port 5685)
    OK   fda0:13c7:aa71:1:25ae:b05b:bc98:86f9
         fw=0.7.15-diag role=Child up=376s resets=3 reg_ok=3 recover=0 wdog=0 boot_burst=0 rloc16=0xe014
  ```
  5/5 corridas consecutivas OK con la retransmisión CON.

- **COM82 / c5cc** — el primer intento de flash falló por su USB-CDC colgado a nivel driver (`esptool` *write timeout* al ROM). Tras un **power-cycle físico** el USB se recuperó y el reflash a `0.7.15-diag` fue limpio (ambos hashes verificados). Arranca y opera: `ami status` = **ALL OK**, Thread `CHILD`, **LwM2M registered**, DLMS leyendo (V=121 V, f≈60 Hz), activo en TB. Demostró de libro el valor del `/diag`: a los ~54 s (aún `reg_ok=0`, sin registrar en TB) **ya respondía el GET por IPv6** con su estado completo; a los ~102 s pasó a `reg_ok=1` (registrado).

  > **Aprendizaje operativo:** un USB-CDC colgado (esptool *write timeout*) se cura con **power-cycle físico** — no hay recuperación por software del lado PC. La dirección OMR de un nodo recién atacado tarda ~15–60 s en volverse enrutable desde el LAN (propagación del OMR al OTBR); hasta entonces el `ping -6`/`/diag` da timeout aunque el nodo ya esté en la malla.

## Archivos tocados / creados

- `src/coap_diag.c`, `src/coap_diag.h` — servidor CoAP `/diag` (nuevo).
- `sections-ram.ld` — sección iterable de recursos del servicio diag (nuevo).
- `CMakeLists.txt` — fuente + `zephyr_linker_sources` + `zephyr_iterable_section`.
- `prj.conf` — `CONFIG_COAP_SERVER=y`, `CONFIG_ZVFS_OPEN_MAX=16`.
- `src/main.c` — `#include "coap_diag.h"`, `coap_diag_init()`, jitter eager-reattach, `CLIENT_FIRMWARE_VER=0.7.15-diag`.
- `tools/node_doctor.py` — diagnóstico/doctor/fix 100 % automatizado (`FW_LATEST=0.7.15-diag`).
- `tools/diag_get.py` — cliente IPv6 `/diag` lado-servidor (nuevo).
