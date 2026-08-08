# Hallazgos del banco — agosto 2026

Qué se midió y qué se arregló en el laboratorio controlado, y qué de eso aplica a
la flota. Complementa `docs/PENDIENTES.md` (que mira la flota desde el servidor):
este documento mira **un nodo instrumentado**, donde las hipótesis se pueden
provocar a voluntad en vez de esperar a que ocurran.

Firmware de referencia: **0.7.19-ami** = `0.7.18-ami` (forensia de la flota) +
los arreglos del banco. Todas las cifras son medidas; cada sección dice cómo.

---

## 0. Qué es el banco ahora

Antes, cada prueba de banco se unía a la **malla de producción** — se perturbaba
el sistema que se intentaba medir (`PENDIENTES.md` §7). Ya no.

| pieza | qué es |
|---|---|
| RCP | SONOFF ZBDongle-E, ya venía con ot-rcp/SPINEL 2.5.3.0 (no hubo que flashearlo) |
| OTBR | `otbr-agent` **nativo** (systemd) dentro de WSL2, no docker |
| red Thread | `GatewaySmartGrid`, canal 11, PAN 0x3940, OMR `fdaf:e549:1751:1::/64` |
| servidor | ThingsBoard CE 4.3.1.3 + postgres en docker, `network_mode: host` |
| descubrimiento | `_lwm2m._udp` publicado por SRP — el nodo lo resuelve igual que en campo |
| nodo | XIAO ESP32-C6, consola+shell en UART0 (D6/D7) |
| potencia | FNIRSI FNB-C2 en línea, ~100 Hz |

Puesta en marcha: `docs/LAB_THINGSBOARD.md`. Restauración tras un reinicio:
`python tools/lab_restore.py`.

**El nodo registra contra un servidor real, con los watchdogs de producción
activos.** Esa es la diferencia que hace que un fallo aquí signifique algo.

---

## 1. El "USB cliff", cuantificado

La discusión de meses sobre si la ráfaga de registro tumba nodos ya no necesita
hipótesis. Medición con el FNB-C2 en línea: **2 864 599 muestras limpias, ~8 h**.

| | mA |
|---|---|
| piso en reposo (mediana) | **55.5** |
| p90 / p99 / p99.9 | 64.8 / 69.6 / 75.5 |
| **pico de ráfaga** | **262.1** |

Eventos de ráfaga: 346 (**0.7 por minuto**), duración mediana **250 ms**.

**La aritmética que explica todo:**

| escenario | nodos que caben en 500 mA (USB 2.0) |
|---|---|
| todos en reposo | **9** |
| todos en ráfaga a la vez | **1** |

Un nodo en ráfaga consume el **52 % de un host entero**. **Dos ráfagas
simultáneas = 524 mA > 500 mA y el host corta la alimentación.** Eso es la
"cliff".

Explica lo que ya se sabía por observación: separar placas entre puertos daba
~100 % de éxito al flashear contra ~29 % agrupadas; la PSU resolvía la
operación; y los disparadores siempre fueron eventos que **sincronizan** nodos
(encendido masivo, reinicio del OTBR o del servidor, cambio de partición).
También valida la lógica de des-correlación que ya existe en el firmware:
`boot-stagger`, jitter de eager-reattach y jitter de REGISTER.

**Regla operativa: presupuestar por la ráfaga (262 mA), no por el reposo.**

Sustenta directamente `PENDIENTES.md` §1: la cohorte que murió con 27-61 s de
uptime murió durante el REGISTER inicial, y pasar de 11 a 22 observaciones
duplicó esa ráfaga.

### Revisión con el PPK2 (2026-08-07) — el inrush, no el REGISTER

Repetida la medición con un Nordic PPK2 a **~100 kHz** (900× el FNB),
alimentando la placa en modo source-meter con **el USB desconectado**. Mismo
punto de medida (bus de 5 V), 9.0 M de muestras:

| | mA @ 5 V |
|---|---|
| idle (mediana) | **52.4** (el FNB decía 55.5 — los dos instrumentos se corroboran) |
| ráfaga de radio TX | **~167** |
| **inrush de encendido** | **246.9** ← el evento mayor de todos |

**Corrección:** se había dicho que 262 mA era un *piso*. No lo era — el FNB leía
un ~6 % **de más**, y el pico real es 246.9 mA. El FNB era más exacto de lo que
se le atribuyó.

**Aritmética corregida (host USB-2.0, 500 mA):** en reposo caben 9 nodos; con
ráfagas de radio simultáneas (167 mA), 3; **con inrush de encendido simultáneo
(246.9 mA), 2** — 494 mA, justo en el borde; tres suman 741 mA y se pasa.

**El inrush es la restricción vinculante, no el REGISTER.** Eso reencuadra la
§1 de `PENDIENTES.md`: el momento peligroso es el **encendido masivo**, antes de
que ningún nodo alcance a registrar — consistente con la cohorte que murió con
27-61 s de uptime tras un evento colectivo.

Medido también en el riel de 3.3 V: los picos son ~227 mA en **todas** las fases
(es el techo de TX del hardware, una constante), mientras el inrush llega a
331 mA. Lo que cambia con el registro es la **densidad** de ráfagas (~10× la de
estado estable: 0.22 % vs 0.02 % de muestras), **no su altura**.

Herramienta: `tools/lab_ppk2_capture.py` — 100 kHz, alimenta el DUT, etiqueta
cada muestra con la fase del firmware leyendo la consola en el mismo reloj, y
hace **power-cycle por software** (POR verificado). Con el USB desenchufado el
USB-Serial-JTAG nunca enumera, lo que además elimina los cuelgues de flasheo.

### Salvedades honestas

- El FNB muestrea a ~100 Hz: **no resuelve transitorios <10 ms**. Sirve para
  medias y para vigilancia continua; para picos, usar el PPK2.
- El medidor emite muestras basura ocasionales, CRC válido incluido (se
  observaron exactamente 1290 / 2097 / 8388 mA contra un piso de 55 mA). Las dos
  herramientas rechazan todo lo que supere 8× la mediana. **Nunca citar un `max`
  crudo del FNB.**

Herramientas: `tools/lab_burst_capture.py` (FNB + consola en un solo reloj, cada
muestra etiquetada con la fase del firmware) y `tools/lab_burst_analyze.py`.

---

## 1bis. El voltaje de muerte, y por qué un nodo en brownout es invisible

`PENDIENTES.md` §1 pedía el voltaje al que un nodo realmente muere. Hacía falta
una fuente programable; el PPK2 lo es. Barrido sobre **VSYS**, con power-cycle en
cada paso (`tools/lab_voltage_sweep.py --mode boot`):

| VSYS | resultado | I mediana | I pico |
|---|---|---|---|
| 5000 mV | arranca y corre | 52.8 mA | 242.9 mA |
| 4000 mV | arranca y corre | 71.3 | 312.1 |
| **3200 mV** | **arranca y corre — el mínimo** | 69.5 | 320.6 |
| **3100 mV** | **bucle de brownout** (237 arranques / 35 s) | — | — |
| 3000 mV | bucle de brownout (625 / 40 s ≈ 15 por segundo) | 23.8 | 108.1 |
| ≤2800 mV | muerto | 0.4 | — |

**El precipicio es filoso: entre 3100 y 3200 mV.** Desde los 5 V nominales eso
es un 36 % de caída antes de que algo falle — el SoC es robusto; lo frágil es lo
que pasa *después*.

### La corriente SUBE cuando la tensión baja

52.8 → 69.5 mA de mediana y 243 → 321 mA de pico al ir de 5000 a 3200 mV. La
placa lleva un conversor **buck**, no un LDO: mantiene la potencia constante.
**Es realimentación positiva** — un riel compartido que se hunde hace que cada
nodo consuma *más*, hundiéndolo más. Junto con el inrush de 246.9 mA (dos nodos
= 494 mA de un host de 500 mA), da un mecanismo completo y medido del colapso
por encendido masivo.

### Un nodo en brownout es forensicamente MUDO

En el bucle, el ROM imprime `rst:0xf (LP_BOD_SYS)` en cada reinicio: **el
detector de brownout del hardware sí dispara**. Pero a ~15 arranques por segundo
el nodo **nunca alcanza el código de aplicación**. No registra, no emite
telemetría, no imprime su propia línea de `Reset cause`. A lo largo de ~862
arranques por brownout, `total_resets` avanzó solo ~120: el contador no puede
contar lo que nunca llega a la escritura en NVS. Y en el arranque limpio de
recuperación el firmware reporta `POR=1, BROWNOUT=0` — **ningún rastro**.

**Para el servidor ese nodo es indistinguible de "ausente"** — el síntoma
crónico de la flota.

Esto refina §1: subir el umbral del BOD (`LVL_7 = 2.51 V`, por debajo del punto
real de falla ~3.15 V) sirve para que **la primera caída** sea reportable, pero
una vez arrancado el bucle **nada en el firmware puede reportar nada**. El
rastro duradero es **`boot_burst` (RID 34)** — que sobrevive en NVS y es la
huella de una tormenta de brownout pasada. Este nodo de banco cargaba
`boot_burst = 184` de exactamente esa historia, y **solo se volvió visible en
ThingsBoard tras el trabajo del modelo de 42 RIDs y el recorte de observes**
(§3).

## 2. Dos bugs de firmware que dejaban nodos muertos

### 2.1 El throttle de boot-burst mataba de hambre al watchdog de arranque

**El peor de los dos: producía un bucle de reinicio permanente que ningún OTA
podía arreglar.**

`chan_boot` se arma en `SYS_INIT(POST_KERNEL)` — antes de `main()` — con
`CONFIG_AMI_HW_WATCHDOG_TIMEOUT_S` (300 s), y a propósito se deja sin alimentar
para que un cuelgue en la ruta NVS dispare TG0_WDT. Su primera comida llega de
`hw_watchdog_note_boot_survived()` y del hilo alimentador que crea
`hw_watchdog_init()` — **ambos posteriores** al limitador de boot-burst.

El limitador dormía `k_sleep(CONFIG_AMI_BOOT_BURST_THROTTLE_S)` = 300 s. Con
`THROTTLE >= TIMEOUT`, **el watchdog mordía siempre a mitad del throttle**. Ese
reset contaba como otro arranque inestable, el contador nunca bajaba, y la
protección anti-desgaste de NVS producía exactamente la tormenta de reinicios
que existe para evitar.

**Prueba (captura de consola):** `total_resets 54 -> 55`; `throttling 300s` en
t=1.521 s; luego `rst:0xc (SW_CPU)` con `WDT=0 / BROWNOUT=0`; **cero** líneas de
init posteriores al throttle; post-mortem `uptime=320 s` (= 300 de throttle + el
arranque). Tras el arreglo: **0 reinicios durante el throttle**, `boot survival
confirmed`, `Thread started`, REGISTER completo.

**Arreglo:** `hw_watchdog_feed_boot()` (alimenta `chan_boot` sin marcar
`boot_survived`, porque una pausa deliberada no demuestra que el arranque haya
terminado) y el throttle duerme en trozos de `TIMEOUT/3` alimentando entre
ellos. Es correcto porque `settings_load` y `post_mortem` ya completaron — sus
líneas de log preceden al limitador: **estamos parados a propósito, no
colgados.**

> **Regla para este código: todo bloqueo deliberado de `main()` anterior a
> `hw_watchdog_init()` debe alimentar `chan_boot`.**

**Validado con los valores de producción** (`tools/lab_soak.py --minutes 30`,
30 muestras, nodo en 0.7.19-ami contra el ThingsBoard del banco):

| | |
|---|---|
| `uptime_s` | 218 → **1711 s**, monótono |
| `watchdog_count` | **0** (ningún watchdog disparó) |
| `boot_burst` | **0 → 0** (la condición del deadlock ni se armó) |
| attached | 29/30 muestras |
| keys de telemetría | 54-55, estables |
| `total_resets` | 2 → 4 |

Los dos reinicios ocurrieron **dentro del primer minuto**, durante el transitorio
de republicar SRP; después el nodo estuvo **29 minutos consecutivos sin
reiniciar**. Corría con `BOOT_BURST_THROTTLE_S=300` y `HW_WATCHDOG_TIMEOUT_S=300`
— exactamente la combinación que antes lo condenaba al bucle permanente.

Por qué importa para la flota: cualquier nodo que acumule
`CONFIG_AMI_BOOT_BURST_MAX` (10) arranques inestables —por un brownout, pérdida
de RF, una caída del servidor— entra en el bucle. **El OTA no puede rescatarlo**
(nunca vive lo suficiente para registrar ni descargar). Es sospechoso principal
de los nodos que se apagan y no vuelven.

### 2.2 El arranque de OpenThread se tragaba `OT_ERROR_INVALID_STATE`

`apply_otbr_dataset()` trataba `INVALID_STATE` de `otIp6SetEnabled` /
`otThreadSetEnabled` como benigno ("ya está en el estado deseado"). **No lo es:
significa que la llamada falló.** Tragárselo dejaba nodos corriendo
indefinidamente con `ot ifconfig` = down y `ot state` = disabled — radio muda,
sin reinicio, sin error, y con el log diciendo `"Thread started"`.

**Arreglo:** verificar el estado real (`otIp6IsEnabled()`,
`otThreadGetDeviceRole()`), reintentar apagando/encendiendo, y solo entonces
escalar. Maneja bien ambos casos: un `INVALID_STATE` genuinamente benigno pasa
sin hacer nada.

---

## 3. Los RIDs 23-36 del objeto 33000, desbloqueados

`PENDIENTES.md` §3 daba por perdidos el post-mortem (23-28), la observabilidad
de deadlock (29-33) y los indicadores de riesgo de brick (34-36), y culpaba al
XML del modelo. **Eran dos capas, y arreglar solo el modelo no habría bastado.**

1. **Modelo**: el XML declaraba 0-22 y 38, así que TB no tenía tipo para 23-37 y
   los entregaba como OPAQUE.
2. **Techo del cliente**: el perfil pedía **44 observes** contra
   `CONFIG_LWM2M_ENGINE_MAX_OBSERVER=36`; el cliente acepta los primeros 36 y
   **rechaza el resto en silencio** — y los rechazados eran justo los RIDs altos.

Y un tercer obstáculo que habría frenado la subida en seco:

3. **Los modelos LwM2M son inmutables en TB.** Subir el XML extendido devuelve
   `HTTP 400 "This type of resource can't be updated"`. Hay que **borrar y
   recrear**, no actualizar.

**Resultado en el banco: telemetría de 41 → 53 keys (y subiendo a 55 conforme
cada RID emite su primer valor), todas enteras, ninguna OPAQUE.** Ahora fluyen `hang_uptime_s`, `hang_heap_free`, `hang_heap_min_free`,
`hang_lwm2m_state`, `hang_reg_age_s`, `hang_thread_role`, `heap_min_free_live`,
`keepalive_emit`, `keepalive_consec_fail`, `last_emit_uptime`, `boot_burst`,
`noreg_boots`. Los RIDs 0-22 y 38 siguieron funcionando y `ObjectVersion` quedó
en **1.0** — no debe cambiar nunca: el firmware compila LwM2M 1.0 y registra un
`</33000>` pelado sin `;ver=`, así que TB solo empareja un modelo 1.0. El riesgo
temido ("TB descarta en silencio todos los observes de 33000 en todo el tenant")
**no se materializó**.

### El recorte, y por qué no se sube el Kconfig

44 = 28 (33000) + 14 (10242) + 2 (3303). Se quitaron **9 paths del medidor**
(potencia/energía reactiva y aparente, y los agregados 3-fase: RIDs 7, 10, 11,
34, 35, 38, 39, 42, 45 de 10242) conservando tensión, corriente, potencia
activa, energía activa y frecuencia → **35 paths, un slot de reserva**. **Se
conservaron todos los diagnósticos de 33000**, que son el valor de depuración.
Lo quitado sigue siendo legible con un `Read` por RPC; solo deja de empujarse.

Subir `MAX_OBSERVER` en vez de recortar sería ir en contra de la evidencia: más
observes = ráfaga de REGISTER mayor, y §1 acaba de medir esa ráfaga en 262 mA =
52 % de un host. `PENDIENTES.md` §4 ya advertía lo mismo.

**Los RIDs 37 (ruta de reinicio) y 39-41 (panic) quedan fuera del observe a
propósito**: solo cambian tras un crash y el censo de flota ya los lee bajo
demanda. Gastar slots de observe en ellos costaría diagnósticos en streaming sin
ganar nada.

### Procedimiento validado para los 60 nodos

1. `python tools/tb_edge_upload_models.py --mesh <mesh>` (ya hace DELETE+CREATE)
2. Reiniciar TB para que Leshan recargue el `LwM2mModelProvider`
3. Recortar la lista de observe del perfil por debajo de 36
4. Re-REGISTER de los nodos — un cambio de perfil llega a un dispositivo **solo
   en REGISTER** (o vía un RPC Reboot en los ya registrados)
5. Verificar las keys nuevas

El modelo es **por tenant**: afecta a los 60 nodos a la vez, así que los pasos 1
y 3 deben ir en la misma ventana de mantenimiento.

---

## 4. Trampas de infraestructura que costaron horas

Ninguna es del firmware, todas dan síntomas que parecen del firmware.

**Docker Desktop secuestra `docker` dentro de WSL.** Monta su socket sobre el de
systemd, así que los contenedores corren en **su** VM — otro namespace de red,
sin `wpan0`. El síntoma es brutal: ThingsBoard arranca perfecto (Tomcat en 8080,
`LWM2M server started`, endpoint en `:5683`) y aun así es inalcanzable desde la
malla. Detección: `docker inspect <c> --format '{{.State.Pid}}'` y comprobar si
ese PID existe en `/proc` del distro. Arreglo: `systemctl restart docker.socket
docker.service`.

**`usbipd attach` muere con su proceso.** Cuando el RCP desaparece, `otbr-agent`
sale con código 5 y se lleva la malla. Hay que usar `--auto-attach` y mantenerlo
vivo.

**El OTBR vuelve con `srp server` DESHABILITADO.** Sin el servicio SRP el DNS-SD
del nodo falla con `err=-2` y nunca registra — que con los watchdogs de
producción significa reiniciar cada 180 s.

**WSL en modo `mirrored` rompe usbipd en este equipo.** El loopback host↔WSL
falla en ambos sentidos pese al firewall abierto (sospecha principal: el filtro
WFP de Tailscale). `networkingMode=NAT` lo resuelve. Consecuencia: Windows no
llega a la IP `127.0.0.1` del distro y **la IP de WSL cambia en cada reinicio**,
así que `edge_for_mesh("lab")` la resuelve en caliente — y con un round-trip
HTTP real, porque bajo NAT un `connect()` a `127.0.0.1:8080` **tiene éxito**
aunque no fluya un solo byte.

**Los tres primeros los arregla `python tools/lab_restore.py`.**

**Flasheo:** un XIAO alimentado a través del FNB deja su USB-Serial-JTAG en el
limbo "device not functioning" de Windows y **no se puede flashear**. Ni el
power-cycle, ni el modo download por ROM, ni bajar el baudrate lo resuelven —
lo que sí funcionó fue **cambiar de puerto USB físico**. Para operar, el nodo
puede quedarse en el FNB: habla por Thread y el USB-JTAG colgado no molesta.

**Flasheo, la explicación real (2026-08-07).** Lo anterior describe el síntoma;
la causa se aisló midiendo, y son *dos* fallos distintos que se parecían:

- `PermissionError(31)` **al conectar** = el ESP32-C6 usa USB **nativo**. Al
  resetearse para entrar en modo descarga, el dispositivo USB desaparece y
  vuelve a enumerar, así que el handle abierto de esptool queda inválido.
- `Write timeout` **con el puerto abierto** = con la consola movida a UART0
  (`overlays/console_uart0.overlay`) **nadie lee ni escribe el USB-CDC**, el
  búfer se llena y toda escritura expira. Es comportamiento esperado, no avería.

Y el hallazgo que importa: **el USB nativo no sostiene la transferencia masiva**.
Con stub muere tras el primer bloque de 16 KB; sin stub, tras el tercero de 1 KB.
Determinista, con dos cables distintos — no es el cable.

**La receta que sí funciona, y sin tocar botones:**

```bash
# 1) el USB dispara el reset a modo descarga (basta con que entre; puede reportar error)
python -m esptool --chip esp32c6 --port <USB> --after no-reset flash-id
# 2) el ROM escucha TAMBIEN en UART0 -> la escritura va por el FTDI, donde es fiable
python -m esptool --chip esp32c6 --port <FTDI> --before no-reset --after no-reset \
    write-flash --erase-all --flash-mode dio --flash-freq 80m --flash-size 4MB 0x0 zephyr.bin
```

33.9 s con hash verificado. El paso 1 es inestable (hizo falta repetirlo hasta 5
veces); conviene envolverlo en un bucle. Alternativa manual siempre válida:
mantener **BOOT hundido en el instante en que llega la energía** — no antes, no
después — y flashear por UART0.

**Lo que NO funciona:** meter el chip en modo descarga por software escribiendo
`LP_AON_FORCE_DOWNLOAD_BOOT` (bit 30 de `LP_AON_SYS_CFG_REG`). Se probó: el nodo
queda mudo, esptool no sincroniza por ninguna vía y hace falta cortar la
alimentación para revivirlo. El comando se escribió y se retiró; el porqué queda
anotado en `src/main.c` para que nadie lo reintente.

**El PPK2 corta la alimentación del DUT cada vez que una sesión nueva reclama el
instrumento.** Cualquier script que lo abra —aunque sea sólo para medir— hace un
power-cycle silencioso del nodo. Eso destruye todo lo que dependa de estado que
sobrevive: la forensia de panic en RAM retenida, un chip aparcado en el ROM, un
soak de uptime. Se perdieron varias medidas por esto antes de detectarlo. Ahora
`tools/lab_ppk2_hold.py` mantiene la sesión abierta y la salida encendida; nada
más debe abrir el PPK2 mientras corre.

---

## 5. Qué queda abierto

- [x] **Forensia de panic (RIDs 39-41) — VALIDADA 2026-08-07, y estaba muerta.**
      `LOG_PANIC()` abría el manejador de fallos y se colgaba en contexto de
      excepción, así que nunca se estampaba el sitio del crash ni se llegaba al
      `sys_reboot`: el nodo se colgaba 22 s hasta que el watchdog lo rescataba,
      sin dejar rastro. Un nodo reventado se veía igual que un nodo ausente.
      Arreglado en `0.7.20` (evidencia primero, logging después) y verificado
      con `0.7.21-fault`: `mepc` resuelve a la línea exacta provocada. Detalle
      completo y las dos trampas de inyección de fallos en
      `docs/PENDIENTES.md` §2.5.
- [ ] **Extraer el coredump** de la partición y abrirlo con
      `zephyr/scripts/coredump/coredump_gdbserver.py`.
- [ ] **`ISR0` al 100 %** (0 de 8192 B sin tocar, con `CONFIG_INIT_STACKS=y`, o
      sea medición válida). Mismo patrón que el Bug #5 histórico. Sin explicar.
- [ ] **Medir el riel de 3.3 V durante la ráfaga**, no en reposo
      (`PENDIENTES.md` §1). El FNB mide el bus USB de 5 V; el riel interno
      necesita el AD2 o el PPK2.
- [ ] **Confirmar la aritmética del cliff con dos nodos**: dos ráfagas
      simultáneas deberían tumbar un host USB. Es la prueba directa de §1.
- [ ] **Subir el modelo de 42 RIDs a la flota** siguiendo §3, y recortar el
      observe en la misma ventana.

### Instrumentos que desbloquean lo anterior

Coincide con `PENDIENTES.md` §7, al que se llegó por separado:

| prioridad | equipo | ~USD | qué desbloquea |
|---|---|---|---|
| 0 | Nordic **PPK2** | 100 | 100 kHz y 200 nA-1 A: la forma real del transitorio. Además **alimenta la placa sin líneas de datos USB**, lo que elimina de raíz toda la clase de cuelgues de USB-JTAG que bloquearon el flasheo hoy. |
| 0 | Hub USB con **PPPS** (`uhubctl`) | 40-70 | power-cycle por software. Hoy cada ciclo exige una mano humana, lo que hace imposible el estudio desatendido. |
| 1 | Dongle **nRF52840** + nRF Sniffer | 25 | ver la ráfaga de registro en el aire: cuánto ocupa con 11 vs 22 observaciones. |
