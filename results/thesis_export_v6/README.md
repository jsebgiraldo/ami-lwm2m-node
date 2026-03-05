# Thesis Export v6 — Firmware v0.18.0: Push Periódico con Rate Controlado por Servidor

**Tesis:** Tesis_jsgiraldod_2026_rev_final  
**Dispositivo:** ami-esp32c6-2434 (ESP32-C6 RISC-V + Thread 802.15.4)  
**Plataforma:** Zephyr RTOS → Thread mesh → ThingsBoard Edge (LwM2M)  
**Board:** xiao_esp32c6/esp32c6/hpcore  
**Firmware:** v0.18.0  
**Commit:** `0505707`  
**Base:** v0.17.0 (`7f34ab3`)  
**Fecha:** 2026-03-04  

---

## ¿Qué resuelve v0.18.0?

### Problema detectado en benchmark v0.17.0

El benchmark de v0.17.0 (3 escenarios × 300s) reveló que el sistema
**THRESH_CHECK** generaba tráfico excesivo:

| Causa | Impacto |
|-------|---------|
| `THRESH_POWER = 0.01` (10 mW) | Potencias activa/reactiva/aparente siempre superan el umbral → notificación **cada 15s poll** |
| Nudge RSSI/LQI (±1 dBm artificial) | Forzaba notificación de radio **cada poll** sin cambio real |
| En escenario agresivo (1s): | RSSI (73) + LQI (101) = 174 msgs = **70% del tráfico total** |

### Solución: Eliminar thresholds, delegar rate al servidor

v0.18.0 adopta el enfoque estándar LwM2M:

1. **Firmware:** Actualiza el recurso LwM2M y llama `lwm2m_notify_observer()` 
   cada ciclo DLMS poll (15s). No evalúa deltas ni umbrales.
2. **Servidor:** Los atributos observe del servidor (`pmin`/`pmax`) controlan
   la frecuencia real de notificaciones CoAP. TB Edge configura estos atributos
   por recurso.
3. **Resultado:** El firmware es más simple, el rate real lo decide el operador
   desde el servidor sin necesidad de reflashear el dispositivo.

---

## Estructura

```
thesis_export_v6/
├── README.md
├── generate_figures.py             # Generador de figuras v0.18.0
├── datos/
│   └── test_results_v018.txt       # Salida completa 117/117 tests
├── figuras/
│   ├── fig_push_flow_v018.png      # Flujo PUSH_FIELD vs THRESH_CHECK
│   ├── fig_architecture_v018.png   # Arquitectura 5 capas simplificada
│   ├── fig_rate_control.png        # Diagrama rate control server-side
│   ├── fig_test_summary_v018.png   # Resumen suites de tests
│   └── fig_traffic_reduction.png   # Comparación tráfico esperado
└── tablas/
    ├── push_field_v018.tex         # Nuevo macro PUSH_FIELD
    ├── changes_v018.tex            # Resumen de cambios vs v0.17.0
    ├── protection_layers_v018.tex  # Arquitectura defensiva 5 capas
    ├── unit_tests_v018.tex         # Resumen tests (HDLC 29, COSEM 43, Logic 45)
    ├── new_tests_v018.tex          # 3 tests nuevos + 4 eliminados
    ├── constants_v018.tex          # Constantes clave
    └── rate_control_v018.tex       # Mecanismo pmin/pmax
```

---

## Cambios principales (v0.17.0 → v0.18.0)

### 1. Eliminación del sistema THRESH_CHECK (`dlms_meter.c`)

**Eliminado:**
- 6 constantes de umbral: `THRESH_VOLTAGE`, `THRESH_CURRENT`, `THRESH_POWER`,
  `THRESH_POWER_FACTOR`, `THRESH_ENERGY`, `THRESH_FREQUENCY`
- `MAX_SILENT_POLLS` (forzaba notify cada 5 polls silenciosos)
- Variables estáticas: `last_notified[27]`, `first_push`, `polls_without_notify`
- Macro `THRESH_CHECK(field, rid, thresh, bit_idx)` (20+ líneas de lógica delta)

**Reemplazado por:**
```c
#define PUSH_FIELD(field, rid, bit_idx) do {                                  \
    if (!(readings->field_mask & (1u << (bit_idx)))) {                    \
        skipped++;                                                    \
        break;                                                        \
    }                                                                     \
    lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, rid),            \
                  readings->field);                                       \
    lwm2m_notify_observer(POWER_METER_OBJECT_ID, 0, rid);                \
    pushed++;                                                             \
} while (0)
```

**Lógica:** Si el bit del campo está activo en `field_mask` → actualiza recurso
LwM2M + notifica. Si no fue leído → omite. Sin comparación de deltas.

### 2. Eliminación del nudge RSSI/LQI (`thread_conn_monitor.c`)

**Antes (v0.17.0):**
```c
static int rssi_nudge = 0;
rssi_nudge = 1 - rssi_nudge;    /* Alterna 0/1 */
lwm2m_set_s16(..., best_rssi + rssi_nudge);  /* Valor artificial */
```

**Ahora (v0.18.0):**
```c
static int16_t prev_rssi = -128;
static int16_t prev_lqi_pct = -1;
if (best_rssi != prev_rssi || lqi_pct != prev_lqi_pct) {
    lwm2m_set_s16(..., best_rssi);    /* Valor real */
    lwm2m_notify_observer(...);
    prev_rssi = best_rssi;
    prev_lqi_pct = lqi_pct;
}
```

**Impacto:** RSSI/LQI solo se notifican cuando cambian realmente. Elimina ~174
mensajes/300s en escenario agresivo.

### 3. Separación de intervalo radio (`main.c`)

**Antes:** Métricas de radio se actualizaban cada poll DLMS (15s)  
**Ahora:** Intervalo separado de 60s (`CONN_UPDATE_INTERVAL_S`)

```c
#define CONN_UPDATE_INTERVAL_S  60   /* RSSI/LQI/Thread update */
```

El loop principal tiene dos timers independientes:
- **DLMS poll:** cada `dlms_poll_interval_s` (15s) — lecturas de medidor
- **Radio update:** cada `CONN_UPDATE_INTERVAL_S` (60s) — RSSI/LQI/Thread status

---

## Flujo de datos (v0.18.0)

```
Medidor DLMS ──RS-485──→ meter_read_all()
                              │
                              ├── OBIS OK  → valor real + bit ON
                              └── OBIS FAIL → valor = 0, bit OFF
                              │
                         ¿≥50% leídos?
                         NO → descarta todo (valid=false)
                         SÍ ↓
                    readings_sanity_check()
                         FAIL → descarta todo
                         OK ↓
                    PUSH_FIELD × 27 campos
                         bit OFF → skip (no envía)
                         bit ON  → set_f64 + notify_observer
                              ↓
                    LwM2M observe engine
                         pmin/pmax → controla CoAP notify rate
                              ↓
                    ThingsBoard Edge ← CoAP notifications
```

**Diferencia clave vs v0.17.0:** No hay comparación delta en el firmware.
El observe engine del stack LwM2M (Zephyr) respeta los atributos `pmin`/`pmax`
configurados desde TB Edge — la notificación CoAP solo sale si el servidor
lo ha solicitado según esos parámetros.

---

## Arquitectura de Protección (5 Capas)

| Capa | Componente | Protección |
|------|-----------|-----------|
| L1 | `meter_read_all()` | `memset(0)` + `field_mask` por OBIS individual |
| L2 | `MIN_READ_PERCENT` | Descarta si cobertura < 50% |
| L3 | `readings_sanity_check()` | Rangos V ∈ [50,500] V, f ∈ [40,70] Hz + presencia mínima |
| L4 | `PUSH_FIELD(bit_idx)` | Omite campos sin bit activo en `field_mask` |
| L5 | `consecutive_meter_failures` | Log crítico tras 5 fallos consecutivos |

**Nota:** Se eliminó la capa "L5: last_good por campo" de v0.17.0 ya que
el sistema `last_good` sigue existente para cache pero no como capa de
protección del push. Y se eliminó `MAX_SILENT_POLLS` (ya no aplica sin
threshold).

---

## Constantes Clave

| Constante | Valor | Archivo | Descripción |
|-----------|-------|---------|-------------|
| `MIN_READ_PERCENT` | 50 | `dlms_meter.c` | % mínimo de OBIS para lectura válida |
| `VOLTAGE_MIN` | 50.0 V | `dlms_meter.c` | Límite inferior tensión |
| `VOLTAGE_MAX` | 500.0 V | `dlms_meter.c` | Límite superior tensión |
| `FREQ_MIN` | 40.0 Hz | `dlms_meter.c` | Límite inferior frecuencia |
| `FREQ_MAX` | 70.0 Hz | `dlms_meter.c` | Límite superior frecuencia |
| `MAX_CONSEC_FAILURES` | 5 | `main.c` | Fallos antes de log crítico |
| `CONN_UPDATE_INTERVAL_S` | 60 s | `main.c` | Intervalo métricas radio (nuevo) |
| `dlms_poll_interval_s` | 15 s | `main.c` | Intervalo lectura DLMS medidor |

**Eliminadas en v0.18.0:**

| Constante eliminada | Valor anterior | Razón |
|---------------------|---------------|-------|
| `THRESH_VOLTAGE` | 1.0 V | Ya no se evalúan deltas |
| `THRESH_CURRENT` | 0.1 A | Ya no se evalúan deltas |
| `THRESH_POWER` | 0.01 kW | Umbral demasiado bajo → notificación perpetua |
| `THRESH_POWER_FACTOR` | 0.01 | Ya no se evalúan deltas |
| `THRESH_ENERGY` | 0.1 kWh | Ya no se evalúan deltas |
| `THRESH_FREQUENCY` | 0.1 Hz | Ya no se evalúan deltas |
| `MAX_SILENT_POLLS` | 5 | Sin threshold no hay polls silenciosos |

---

## Resultados de Tests

| Suite | Tests | Resultado |
|-------|-------|-----------|
| HDLC (CRC-16, tramas, parsing) | 29/29 | PASS |
| COSEM (AARQ, AARE, GET, RLRQ) | 43/43 | PASS |
| DLMS Logic (field_mask, sanity, push) | 45/45 | PASS |
| **Total** | **117/117** | **ALL PASS** |

### Tests nuevos (v0.18.0): 3 agregados

| Test | Descripción |
|------|-------------|
| `test_push_field_skips_when_bit_not_set` | PUSH_FIELD omite cuando `field_mask` bit=0 |
| `test_push_field_pushes_when_bit_set` | PUSH_FIELD envía cuando `field_mask` bit=1 |
| `test_push_field_all_27_bits` | Verifica `0x07FFFFFF` cubre los 27 campos |

### Tests eliminados (v0.18.0): 4 removidos

| Test eliminado | Razón |
|---------------|-------|
| `test_thresh_check_no_notify_within_threshold` | THRESH_CHECK eliminado |
| `test_thresh_check_notify_exceeds_threshold` | THRESH_CHECK eliminado |
| `test_thresh_check_zero_would_exceed_threshold` | THRESH_CHECK eliminado |
| `test_thresh_check_last_good_within_threshold` | THRESH_CHECK eliminado |

**Neto:** 118 − 4 + 3 = **117 tests**

---

## Archivos Modificados (vs v0.17.0)

| Archivo | Cambio principal |
|---------|-----------------|
| `src/dlms_meter.c` | THRESH_CHECK → PUSH_FIELD, elimina 6 defines + statics |
| `src/main.c` | +`CONN_UPDATE_INTERVAL_S=60`, loop dual-timer |
| `src/thread_conn_monitor.c` | Nudge eliminado, notify por cambio real |
| `tests/test_dlms_logic.c` | −4 tests threshold, +3 tests push_field |

**Diff total:** 4 files changed, +117 −141

---

## Mecanismo de control de rate: pmin/pmax

El protocolo LwM2M define atributos observe que el servidor envía al cliente:

| Atributo | Significado | Control |
|----------|-----------|---------|
| `pmin` | Período mínimo entre notificaciones | Evita saturación |
| `pmax` | Período máximo entre notificaciones | Garantiza frescura |

```
TB Edge (servidor LwM2M)
    │
    ├── Configura pmin/pmax por recurso
    │   (via profile → device → observe attributes)
    │
    ▼
Firmware (cliente LwM2M)
    │
    ├── PUSH_FIELD: set_f64() + notify_observer()  ← cada 15s poll
    │
    └── Zephyr LwM2M engine:
        ├── Si t < pmin → suprime notificación
        ├── Si t > pmax → fuerza notificación
        └── Si pmin ≤ t ≤ pmax → envía si hay cambio
```

**Ventaja:** El operador ajusta el rate **desde el dashboard** (TB Edge)
sin necesidad de recompilar ni reflashear el firmware del nodo.

---

## Impacto esperado en tráfico

Basado en el benchmark v0.17.0 (escenario agresivo, pmin=1s):

| Fuente de tráfico | v0.17.0 (246 msgs/300s) | v0.18.0 (estimado) |
|--------------------|------------------------|---------------------|
| RSSI + LQI (nudge) | 174 msgs (70.7%) | ~5 msgs (solo cambios reales) |
| Power (thresh=10mW) | ~60 msgs (24.4%) | Controlado por pmin/pmax |
| Voltage/Freq/Energy | ~12 msgs (4.9%) | Controlado por pmin/pmax |

**Reducción estimada:** De 246 → ~80-100 msgs con pmin=15s (baseline natural).
El rate exacto depende de los atributos observe configurados en TB Edge.

---

## Cómo usar en la tesis

Los archivos `.tex` en `tablas/` se incluyen directamente:

```latex
% En el capítulo de implementación:
\input{thesis_export_v6/tablas/push_field_v018}
\input{thesis_export_v6/tablas/protection_layers_v018}
\input{thesis_export_v6/tablas/constants_v018}
\input{thesis_export_v6/tablas/rate_control_v018}
\input{thesis_export_v6/tablas/changes_v018}

% En el capítulo de resultados:
\input{thesis_export_v6/tablas/unit_tests_v018}
\input{thesis_export_v6/tablas/new_tests_v018}

% Figuras:
\includegraphics[width=\textwidth]{thesis_export_v6/figuras/fig_push_flow_v018}
\includegraphics[width=\textwidth]{thesis_export_v6/figuras/fig_architecture_v018}
\includegraphics[width=\textwidth]{thesis_export_v6/figuras/fig_rate_control}
\includegraphics[width=\textwidth]{thesis_export_v6/figuras/fig_test_summary_v018}
\includegraphics[width=\textwidth]{thesis_export_v6/figuras/fig_traffic_reduction}
```
