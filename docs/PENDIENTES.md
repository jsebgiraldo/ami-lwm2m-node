# Pendientes — estado a 2026-08-05

Documento vivo. Recoge lo que queda abierto en la flota AMI (ESP32-C6 / Zephyr /
OpenThread / LwM2M) y por qué importa. Las cifras son medidas, no estimadas; cada
sección dice cómo se obtuvieron para poder repetirlas.

---

## 0. Fotografía de la flota

Censo del 2026-08-05 contra el ThingsBoard Edge de la Pi4 (`192.168.1.111:8090`):

| | nodos |
|---|---|
| registrados | 60 |
| vivos (telemetría < 15 min) | 39 |
| caídos | 21 |

Roles de los vivos: 27 Child, 10 Router. Firmware desplegado: `0.7.17-ami`
(35 de 37 respondieron a `/3/0/3`).

**Nodo del medidor físico real (`ami-esp32c6-25c0`, XIAO C6 + EMSITECH por
DLMS/RS485):** sano. 1816 muestras de `activeEnergy` en 24 h con un único hueco
de 11.7 min. El totalizador subió 63 Wh en 24 h = 2.63 W de media, contra
`activePower` = 2.60 W instantáneo — **cuadran**, que es la comprobación de
coherencia que hace defendibles los datos. No está conectado por USB a ningún
PC: corre autónomo en campo.

---

## 1. Por qué se reinician los nodos (P0)

Censo de `Objeto 33000 RID 37` (ruta de reinicio) sobre los 35 vivos que
respondieron:

```
27 nodos  código 0   — el reset NO pasó por ninguna ruta del firmware
 6 nodos  código 11  — PANIC del kernel   (25c0, c144, 15d8, f794, cc8c, cc7c)
 2 nodos  sin respuesta                    (c5d0, fbe4)
 0 nodos  códigos 1-10 — ninguna ruta de recuperación planificada
```

Dos conclusiones y una corrección importante.

**El 17% de la flota se estrella.** No es alimentación: es
`k_sys_fatal_error_handler`. En 25c0 el contexto es `RID 27 = 0x08`
(RECOVERING) y `RID 23 = 67561 s`, o sea corrió 18.8 h, entró en la ruta de
recuperación de LwM2M y panicó dentro de ella.

**Corrección sobre el brownout.** Se había dicho que la evidencia apuntaba a
brownout de campo. Los datos no lo sostienen — pero tampoco lo desmienten, y la
razón es peor: **el detector está ciego por configuración**. El driver del C6
sí sabe reportar brownout (`hwinfo_esp32.c` mapea `ESP_RST_BROWNOUT →
RESET_BROWNOUT`), pero `prj.conf` documenta que el umbral quedó en **LVL_7 =
2.51 V** porque el parser de Kconfig rechaza el símbolo desde `prj.conf`. El C6
necesita 3.0 V: una caída a 2.8 V lo corrompe sin disparar nada.
**Ausencia de `RESET_BROWNOUT` NO es evidencia de que la fuente esté sana.**

### Firma de la cohorte que cayó junta

Ocho nodos murieron entre las 18:21 y las 19:24 del 2026-08-03, todos con
`total_resets = 1` y **uptime de 27 a 61 segundos** en su último reporte. No es
una flota reiniciándose en bucle: son placas que arrancaron una vez, reportaron
~40 s y callaron para siempre — murieron durante o justo después del registro
LwM2M inicial, que es el pico de TX sostenido de todo el ciclo de vida.

Contraste con los supervivientes: **0 de 36 vivos tienen `total_resets ≤ 1`**.
Los que sobreviven son justamente los que se reinician mucho (c610: 24 resets,
cc8c: 19, cccc: 19). Y 17 de 36 vivos llevan menos de 5 min de uptime: la flota
no está estable, está rotando.

### Hipótesis incómoda, sin verificar

Al pasar de 11 a 22 observaciones LwM2M, la ráfaga de registro se duplicó. Esos
8 nodos murieron el mismo día del cambio. `overlays/brn_fix_coap64.conf` ya
identifica el registro como disparador de brownout. **No está confirmado el
orden temporal exacto** — hay que mirar el audit log del perfil en TB. Es lo
primero que habría que verificar.

### Pendiente concreto

- [ ] Medir el riel bajo carga **durante la ráfaga de registro**, no en reposo.
      Criterio: si VDD baja de 3.0 V en la ventana de 60 s desde el power-on, es
      alimentación; si se mantiene sobre 3.1 V y el nodo muere igual, es
      firmware/radio. Dos condiciones: nodo suelto y nodo con RS485 conectado.
- [ ] Subir el umbral del BOD vía `-DEXTRA_KCONFIG_OPTIONS_FILE=` (workaround ya
      documentado en `prj.conf`) para que los brownouts **se reporten**.
      `light_control_set_brownout_indicator()` ya existe y deja el LED en rojo:
      diagnóstico de campo caminando, sin instrumento.
- [ ] Verificar el orden temporal del cambio de observaciones vs la cohorte.

---

## 2. Lo que cierra la versión 0.7.18-ami (compilada, sin validar en banco)

### 2.1 El punto ciego de los 27 nodos

Cinco `sys_reboot` no estampaban el tag de razón, así que **todos los reinicios
por watchdog hardware y los reinicios por OTA caían en el mismo cubo que una
pérdida de energía real**. Códigos nuevos:

| código | sitio | significado |
|---|---|---|
| 12 | `hw_watchdog.c` | expiró la gracia de arranque, nunca registró |
| 13 | `hw_watchdog.c` | registrado pero cero observadores |
| 14 | `hw_watchdog.c` | el servidor dejó de confirmar REG_UPDATE |
| 15 | `hw_watchdog.c` | un canal del task watchdog quedó mudo |
| 16 | `firmware_update.c` | reinicio para aplicar OTA |

Se respetó la razón original de esos sitios (ser *dependency-free*): siguen sin
llamar a `ami_reboot_drain`, que duerme. `ami_reboot_set_tag` son dos escrituras
a RAM `__noinit`.

Mapa canónico de códigos: comentario en `src/main.c` bajo
`ami_reboot_reason_to_code()`. Decodificación en Python: `tools/reboot_codes.py`.

### 2.2 El PC del crash — RIDs 39, 40, 41

`k_sys_fatal_error_handler` hacía `ARG_UNUSED(esf)` y tiraba el marco de
excepción. Ahora guarda `reason`, `mepc` (instrucción que falló) y `ra` (su
llamador), con guarda de `NULL` porque un `k_panic()` fuera de contexto de
excepción trae `esf` nulo.

```
riscv64-zephyr-elf-addr2line -f -e build_prod/ami-lwm2m-node/zephyr/zephyr.elf <mepc> <ra>
```

**El ELF debe ser el del build exacto que corría.** Hay que archivar los
artefactos de cada versión que se flashee a la flota o las direcciones son ruido.

### 2.3 Coredump a flash

`CONFIG_DEBUG_COREDUMP` + backend de partición. **No hizo falta tocar el
devicetree**: el layout upstream de 4M ya trae `coredump_partition` (0x3ff000,
4 KB) y el backend enlaza por etiqueta de nodo. **slot0 y slot1 no se mueven**,
así que las imágenes firmadas de la flota desplegada siguen siendo válidas.

Verificado que `kernel/fatal.c` llama a `coredump()` **antes** de
`k_sys_fatal_error_handler()`: nuestro manejador propio no lo bloquea.

Los 4 KB obligan a `MEMORY_DUMP_MIN` (el defecto vuelca toda la RAM). Si se
queda corto: MCUboot va en `BOOT_UPGRADE_ONLY`, así que los **124 KB de
`scratch_partition` están muertos** y se pueden reclamar.

### 2.4 Un `BUILD_ASSERT` que no protegía nada

```c
#define THREAD_DIAG_MAX_ID    TD_NUM_FIELDS
BUILD_ASSERT(THREAD_DIAG_MAX_ID == TD_NUM_FIELDS, ...)   // siempre cierto
```

Comparaba un símbolo contra su propia definición. El comentario encima describe
un desbordamiento de buffer que costó horas de depuración y afirma que esta
guarda lo previene — no lo hacía. Sustituido por
`ARRAY_SIZE(thread_diag_fields) == TD_NUM_FIELDS`, que es un conteo
independiente.

### Pendiente

- [x] **Validado en banco 2026-08-07 — y la validación encontró que la función
      no servía.** Ver §2.5. Cerrado en `0.7.20`; `mepc` resuelto a la línea
      exacta con `0.7.21-fault`.
- [ ] Extraer el coredump de la partición y abrirlo con GDB vía
      `zephyr/scripts/coredump/coredump_gdbserver.py`.

### 2.5 La validación de §2.2 encontró la función muerta (2026-08-07)

Provocar un fallo a propósito exigió añadir `ami test panic <kind> CONFIRM`
(`CONFIG_AMI_TEST_FAULT`, por defecto `n`, jamás en imagen de flota). Con eso, el
resultado sobre `0.7.19`:

| | medido |
|---|---|
| salida por consola tras el fallo | **cero bytes**, 22 s |
| reinicio | **`TG0_WDT`** — nunca llegó a `sys_reboot` |
| arranque siguiente | `WDT=1`, **sin registro de panic** |
| RIDs 37 / 39 / 40 / 41 | **vacíos** |

Reproducible dos veces. **Causa**: `LOG_PANIC()` era la primera línea de
`k_sys_fatal_error_handler`. El logging diferido drena por un backend UART por
interrupciones; en contexto de fallo las interrupciones están bloqueadas, ese
drenaje no puede completarse y el `LOG_PANIC()` gira para siempre — arrastrando
consigo la escritura del tag, el código de reinicio y el `sys_reboot`.

Es decir: **un nodo que reventaba en campo quedaba indistinguible de un nodo
ausente**, que es el síntoma crónico de la flota.

**Arreglo (`0.7.20`)**: las escrituras a RAM retenida van primero — son almacenes
planos, no pueden bloquearse — y el logging queda como cortesía posterior. Aunque
el drenaje vuelva a colgarse, el reset por watchdog encuentra el tag completo.

Verificado sobre hardware con `0.7.21-fault`:

```
<err> os:  mcause: 2, Illegal instruction
<err> os:    mepc: 42001284
<err> ami_lwm2m: FATAL panic (reason=0) mepc=0x42001284 ra=0x4200127c
rst:0xc (SW_CPU)                                    <- reinicio limpio
<wrn> ami_lwm2m: panic site: reason=0 mepc=0x42001284 ra=0x4200127c
$ python tools/archive_build.py --resolve 0x42001284 0x4200127c --version 0.7.21-fault
cmd_ami_test_panic at src/main.c:2386               <- la linea provocada
```

**Dos trampas sobre la inyección de fallos, que valen para cualquier validación
futura de este tipo:**

- **Escribir en la dirección 0 no genera excepción en este ESP32-C6.** El núcleo
  acaba en un manejador del ROM (`Saved PC` en `0x4002xxxx`) y se cuelga hasta que
  el watchdog lo rescata. No llega al camino de fallos de Zephyr, así que no
  sirve para validar nada.
- **`k_oops()` es un `ecall`** y el compilador lo atribuye a la sentencia
  *siguiente*: su `mepc` cae una línea corrida. Prueba la cadena, no la
  precisión.

Por eso el tipo a usar es `illegal` (instrucción ilegal): la especificación
RISC-V garantiza `mcause=2` con `mepc` apuntando **a** la instrucción.

---

## 3. Observabilidad que existe en el nodo y no llega al servidor

Los RIDs 23-37 del objeto 33000 **se reportan desde hace versiones** pero llegan
a ThingsBoard como `OPAQUE` (bytes crudos) en vez de enteros, así que nunca se
convirtieron en telemetría. Causa: el XML del modelo declara tipos hasta el RID
22 y el 38, saltándose 23-37 — decisión consciente documentada en
`tools/tb_edge_upload_models.py`, pendiente de revalidar desde entonces.

Se pierde: post-mortem (23-28), observabilidad de deadlock (29-33, que
distinguen "vivo pero motor suspendido" de "caída del servidor" de "boot loop"),
indicadores de nodo tendiendo a ladrillo (34-36) y la ruta de reinicio (37).

El generador ya está actualizado para declarar 23-41. **Falta subirlo.**

### Riesgo a evaluar antes de subir

Documentado en `src/thread_conn_monitor.c`: un desajuste entre el modelo subido
y lo que anuncia el firmware ya provocó una vez que **TB descartara
silenciosamente los observes y la telemetría del objeto 33000 entero, sin una
sola línea de log**. El modelo es por tenant: afecta a los 60 nodos a la vez,
incluida la telemetría de energía del medidor real.

A favor de que salga bien: el RID 38 se añadió exactamente igual, sobre el mismo
modelo `1.0`, y funciona. Pero conviene decidirlo conscientemente.

- [ ] Subir el XML 33000 actualizado (beneficio inmediato: la flota que corre
      0.7.17 **ya reporta** 23-37, solo hay que hacerlos legibles).

---

## 4. Contadores MAC sin observar

`/33000/0/4..9` (unicast, broadcast, errores) no están en el set de observación:
solo se refrescan al re-registrar, así que la capa L2 del análisis por capas OSI
se congela. `mac_tx_total` y `mac_rx_total` aparecen frescos solo en los nodos
donde el monitor los lee bajo demanda — es un artefacto de la herramienta, no
observación real.

**Condición dura:** si se confirma que la ráfaga de registro mata nodos, añadir
observaciones **agrava** el problema. Medir el coste primero en 2 nodos de banco
contra un grupo de control a 24 h.

---

## 5. Aprovisionamiento — el medidor real no llega al central

`25c0` existe solo como dispositivo local del Edge de la Pi4; nunca se creó en
el ThingsBoard central, así que su telemetría no sube. Migrarlo tiene riesgo de
colisión de credenciales LwM2M: si falla, el nodo del medidor deja de reportar.

- [ ] Ensayar la migración primero en `c5d0` o `fbe4` (nunca han reportado, no se
      pierde nada).
- [ ] **Antes de nada, exportar la serie completa de `activeEnergy` de 25c0.**
      Son 66.6 kWh acumulados e irrecuperables.

---

## 6. Hardware puntual

- [ ] `1494`: USB trabado (error 31 de Windows). El nodo está vivo por radio,
      solo no se deja flashear. Necesita desenchufar/enchufar físico.
- [ ] Una placa que no enumera como puerto COM en absoluto.
- [ ] `c5d0` y `fbe4`: nunca han reportado.

---

## 7. Laboratorio controlado

### Ya existe

- **Digilent Analog Discovery 2** con driver ctypes propio
  (`tools/ad2_brownout_capture.py`, dispara en flanco de bajada a 2.9 V, 1 MS/s).
- **Pipeline de QA de 6 etapas** con veredicto APTO/NO APTO
  (`tools/board_acceptance.py`): flash → provisión → registro → soak → cascada →
  LED.
- **Sniffer 802.15.4 propio** (`tools/sniffer/`, app Zephyr para C6) +
  `tools/sniffer_capture.py`.
- **Tests unitarios en C sobre host** (`tests/`): HDLC, COSEM, OBIS, DLMS con
  stubs de Zephyr.

### Falta, y es lo que bloquea la automatización

Cero control de alimentación por software. Hoy un power cycle exige una mano
humana, lo que hace imposible el estudio de estabilidad desatendido.

| prioridad | equipo | ~USD | qué desbloquea |
|---|---|---|---|
| 0 | Nordic **PPK2** | 100 | fuente programable 0.8-5.0 V + amperímetro 200 nA-1 A a 100 kHz, API Python. Barre el riel hasta encontrar el voltaje exacto de muerte durante el registro. Resuelve la sección 1 entera. |
| 0 | Hub USB con **PPPS** (`uhubctl`) | 40-70 | power cycle por software. Comprar de la lista de compatibilidad verificada: muchos hubs anuncian PPPS y no lo implementan. |
| 0 | **Raspberry Pi de laboratorio** | 80 | red Thread separada. Hoy cada prueba de banco se une a la malla **de producción** — se está perturbando el sistema que se intenta medir. |
| 1 | Dongle **nRF52840** + nRF Sniffer 802.15.4 | 25 | mide cuánto ocupa la ráfaga de registro con 11 vs 22 observaciones. Prueba directa de la hipótesis de la sección 1. |
| 1 | Fuente SCPI (Korad KA3005P) | 120 | perfiles de caída controlados: reproducir el fallo a voluntad. |
| 1 | **INA226/INA219** ×8 | 5 c/u | corriente por nodo en paralelo. El AD2 es mejor pero solo cubre una placa. |
| 2 | Relé USB 8 canales | 20 | reproducir "se cayó la regleta": arranque simultáneo de N nodos. |
| 2 | Caja apantallada + atenuadores SMA | 30 | mallado a RSSI controlado. |
| 2 | Analizador lógico (clon Saleae) | 15 | decodificar HDLC/DLMS en el bus RS485. |

**Compra mínima: PPK2 + hub uhubctl + dongle nRF52840 + Pi de laboratorio ≈ 215
USD.** Con eso un agente puede cortar y restaurar alimentación, barrer el
voltaje hasta el punto de muerte, ver la ráfaga en el aire, y hacerlo **sin
tocar la malla de producción**.

### Software

- [ ] Migrar `tests/` a **Twister + ztest**: hoy se compila contra stubs escritos
      a mano, así que solo se prueba lógica pura. Con ztest los mismos tests
      corren en `native_sim` **y en el C6 real**, contra las APIs verdaderas.
- [ ] **CI**: no existe `.github/workflows/`. Nada garantiza que un commit no
      rompa el build.
- [ ] **HIL**: `board_acceptance.py` ya es el 80% de un harness. Envolverlo en
      pytest y dispararlo cada noche sobre el banco vía el hub conmutado.
- [ ] Consolidar **157 scripts en `tools/`**. Nadie sabe cuál es el canónico; eso
      es deuda de QA tanto como la falta de instrumentos.

---

## 8. ¿Migrar a ESP-IDF?

**No con la evidencia actual.** No se puede atribuir el panic a Zephyr, al HAL
de Espressif, a OpenThread o al código propio, porque hasta 0.7.18 se estaba
tirando el `esf`. Migrar sería apostar meses a una hipótesis no medida.

Además Zephyr **ya trae** la función que se iba a construir: coredump con
backend de partición flash, y `config RISCV` selecciona
`ARCH_SUPPORTS_COREDUMP`, `_THREADS if !SMP` y `_STACK_PTR`. Nada que parchear.

Coste real de migrar: ESP-IDF **no tiene cliente LwM2M** (habría que meter Anjay
o Wakaama y reescribir toda la capa de objetos: 33000 con 42 recursos, 10242,
33001, 3303, más observe/notify/registro, todo contra la API `lwm2m_engine` de
Zephyr). Pero lo que de verdad se pierde es el endurecimiento de campo:
`boot_burst`, `storm_backoff`, las rutas de recuperación, el drain USB, el
keepalive watchdog, el post-mortem, el indicador LED de brownout. Eso salió de
ver caerse estos 60 nodos.

**Regla de decisión: sacar el backtrace con 0.7.18 y entonces decidir.** Si cae
en Zephyr o el HAL, hay un bug concreto y reportable (y ya se mantienen parches
fuera de árbol, ver `tools/ZEPHYR_PATCHES.md`). Si cae en código propio — lo
estadísticamente más probable, por ser el más nuevo y menos probado — migrar se
habría llevado el bug consigo.

Argumento legítimo a favor de IDF, para no perderlo: `esp_core_dump` es de
primera mano, el detector de brownout está integrado nativamente (y ahí hay
fricción real, ver sección 1), y las erratas del silicio las maneja Espressif
directamente.

---

## 9. Infraestructura y versionado

- [ ] `docker-compose.resources.yml` **no está cableado** en
      `docker-start-services.sh`. Se dejó así a propósito para que ningún
      reinicio recreara Kafka sin supervisión: el log KRaft vive en
      `/tmp/kafka-logs` (capa escribible) y recrear el contenedor destruye los
      topics `tb_edge*`. Los límites están aplicados en vivo y sobreviven
      reinicios de contenedor, pero no una recreación por script.
- [ ] Toda la personalización de ThingsBoard vive **solo en la distro WSL**
      (`/home/sebas/thingsboard`, clon del repo oficial: no se puede hacer push).
      Copiar a `deploy/thingsboard/` en este repo.
- [ ] `C:\Users\User\Documents\r1000` **no es repo git**; el arreglo de
      `research_report.py` está sin versionar.
- [ ] Las cuatro herramientas `flapper_watch`, `overnight_soak`, `rbtag_watch` y
      `verify_fleet` apuntan a `http://192.168.8.111:8090` — subred **8**, no la
      **1** del edge actual. Están rotas desde algún cambio de red. No se tocó
      por no saber si fue intencional.

---

## 10. Menores

- [ ] Series de tasa por capa OSI con solo 2 puntos; el monitor horario las irá
      llenando.
- [ ] Para un ratio de overhead honesto haría falta leer el RID 38 de toda la
      flota, no solo de 25c0.
- [ ] El R1000 sigue parado por decisión del usuario; el monitor HaLow está a
      oscuras mientras tanto.
- [ ] Prueba de flash a 40 MHz en una placa con boot-loop: nunca se hizo.
- [ ] `prj.conf` tiene dos overrides de banco que producción debería revertir:
      `CONFIG_AMI_OTA_CONFIRM_DELAY_S=60` (el defecto son 600 s) y
      `CONFIG_AMI_DEMO_MODE=y`.
