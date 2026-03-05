# Thesis Export v5 — Firmware v0.17.0: Validación de Lecturas por Campo

**Tesis:** Tesis_jsgiraldod_2026_rev_final  
**Dispositivo:** ami-esp32c6-2434 (ESP32-C6 RISC-V + Thread 802.15.4)  
**Plataforma:** Zephyr RTOS → Thread mesh → ThingsBoard Edge (LwM2M)  
**Board:** xiao_esp32c6/esp32c6/hpcore  
**Firmware:** v0.17.0  
**Commit:** `7f34ab3`  
**Fecha:** 2026-03-04  

---

## ¿Qué resuelve v0.17.0?

**Problema:** En versiones anteriores, cuando una lectura OBIS fallaba (timeout RS-485,
trama corrupta, etc.), el firmware rellenaba el campo con el último valor conocido
(`last_good`) o con cero, y lo enviaba al servidor LwM2M como si fuera una lectura real.
Esto **contaminaba la base de datos** con valores falsos que no provenían del medidor.

**Solución:** v0.17.0 implementa un sistema de validación por campo (`field_mask`) que
garantiza que **solo datos realmente leídos del medidor** lleguen al servidor.

---

## Estructura

```
thesis_export_v5/
├── README.md
├── generate_figures.py                # Generador figuras arquitectónicas (5 PNGs)
├── generate_benchmark_figures.py      # Generador figuras benchmark (5 PNGs)
├── datos/
│   └── test_results_v017.txt          # Salida completa 118/118 tests
├── figuras/
│   ├── fig_field_mask.png             # Diagrama campo field_mask 27 bits
│   ├── fig_protection_layers.png      # Arquitectura 6 capas defensiva
│   ├── fig_memory_comparison.png      # Flash/RAM comparación por versión
│   ├── fig_validation_flow.png        # Flujo validación completo
│   ├── fig_test_summary.png           # Resumen suites de tests
│   ├── fig_benchmark_throughput.png   # Throughput comparativo 3 escenarios
│   ├── fig_benchmark_iat.png          # IAT por recurso y escenario
│   ├── fig_benchmark_timeline.png     # Timeline scatter telemetría
│   ├── fig_benchmark_coap_rssi.png    # Overhead CoAP + RSSI/LQI
│   └── fig_benchmark_completeness.png # Heatmap completitud por recurso
└── tablas/
    ├── unit_tests_v017.tex            # Resumen tests (HDLC 29, COSEM 43, Logic 46)
    ├── new_tests_v017.tex             # Detalle de 10 tests nuevos/actualizados
    ├── memory_v017.tex                # Flash 15.36%, RAM 64.88%
    ├── field_mask_v017.tex            # Mapa de 27 bits OBIS
    ├── sanity_check_v017.tex          # Parámetros de validación de rangos
    ├── protection_layers_v017.tex     # Arquitectura defensiva 6 capas
    ├── thresholds_v017.tex            # Umbrales THRESH_CHECK con bit_idx
    └── benchmark_scenarios.tex        # Tabla comparativa benchmark 3 escenarios
```

---

## Mecanismo: `field_mask` (Bitmask de Lecturas)

Cada ciclo de lectura DLMS (`meter_read_all()`) opera así:

1. **`memset(readings, 0, ...)`** — Todos los campos inician en cero
2. Para cada código OBIS (27 en trifásico, 15 en monofásico):
   - Si la lectura RS-485/DLMS tiene éxito → almacena valor + activa `field_mask |= (1u << i)`
   - Si falla → campo permanece en 0, bit NO se activa
3. **Validación de cobertura:** Si menos del 50% de los OBIS objetivo fueron leídos,
   `readings->valid = false` → la lectura completa se descarta
4. **Sanity check:** Antes del push LwM2M, verifica rangos físicos:
   - Tensión ∈ [50, 500] V
   - Frecuencia ∈ [40, 70] Hz
   - Al menos tensión O frecuencia presente en `field_mask`
5. **Push selectivo:** Cada `THRESH_CHECK(field, rid, thresh, bit_idx)` verifica
   `field_mask & (1u << bit_idx)` — campos no leídos se omiten silenciosamente

### Flujo de datos

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
                    THRESH_CHECK × 27 campos
                         bit OFF → skip (no envía)
                         bit ON  → evalúa umbral → notify LwM2M
```

---

## Arquitectura de Protección (6 Capas)

| Capa | Componente | Protección |
|------|-----------|-----------|
| L1 | `meter_read_all()` | `memset(0)` + `field_mask` por OBIS individual |
| L2 | `MIN_READ_PERCENT` | Descarta si cobertura < 50% |
| L3 | `readings_sanity_check()` | Rangos V/f + presencia mínima |
| L4 | `THRESH_CHECK(bit_idx)` | Omite campos sin bit activo |
| L5 | `last_good` por campo | Solo actualiza campos realmente leídos |
| L6 | `consecutive_meter_failures` | Log crítico tras 5 fallos consecutivos |

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
| `MAX_SILENT_POLLS` | 5 | `dlms_meter.c` | Polls sin notify antes de forzar |

---

## Campos Nuevos en `struct meter_readings`

```c
/* Metadata — v0.17.0 */
bool     valid;          /* True si enough readings succeeded */
int      read_count;     /* Lecturas OBIS exitosas */
int      error_count;    /* Lecturas OBIS fallidas */
int      read_target;    /* OBIS no-skip intentados */
uint32_t field_mask;     /* Bitmask: bit i = obis_table[i] leído OK */
int64_t  timestamp_ms;   /* Uptime al momento de la lectura */
```

---

## Resultados de Tests

| Suite | Tests | Resultado |
|-------|-------|-----------|
| HDLC (CRC-16, tramas, parsing) | 29/29 | PASS |
| COSEM (AARQ, AARE, GET, RLRQ) | 43/43 | PASS |
| DLMS Logic (field_mask, sanity, THRESH_CHECK) | 46/46 | PASS |
| **Total** | **118/118** | **ALL PASS** |

Tests nuevos en v0.17.0 (7 nuevos + 3 actualizados):
- `test_failed_reads_stay_zero` — Campos fallidos permanecen en 0
- `test_last_good_updated_per_field` — Solo campos leídos actualizan cache
- `test_field_mask_initially_zero` — Bitmask inicia en 0
- `test_field_mask_bit_for_each_obis` — Cada OBIS exitoso activa su bit
- `test_min_read_percent_threshold` — Constante MIN_READ_PERCENT = 50
- `test_valid_requires_min_coverage` — valid=false si cobertura < 50%
- `test_sanity_check_rejects_voltage_out_of_range` — V fuera de [50,500] → rechazo
- `test_sanity_check_rejects_frequency_out_of_range` — f fuera de [40,70] → rechazo
- `test_sanity_check_requires_field_coverage` — Cobertura insuficiente → rechazo
- `test_sanity_check_voltage_only_no_frequency` — V sin f → aceptado

---

## Uso de Memoria

| Región | Usado | Disponible | % |
|--------|-------|-----------|---|
| Flash | 644,200 B | 4,194,176 B | 15.36% |
| RAM | 317,248 B | 488,976 B | 64.88% |
| IROM | 499,124 B | 4,194,304 B | 11.90% |
| DROM | 54,504 B | 4,194,304 B | 1.30% |

---

## Archivos Modificados (vs v0.16.0)

| Archivo | Cambio principal |
|---------|-----------------|
| `src/dlms_meter.h` | `+read_target`, `+field_mask` en struct metadata |
| `src/dlms_meter.c` | `memset(0)`, `field_mask` tracking, `MIN_READ_PERCENT`, sanity check reforzado, `THRESH_CHECK` con `bit_idx`, `last_good` por campo |
| `src/main.c` | `MAX_CONSEC_FAILURES=5`, contador `consecutive_meter_failures` |
| `tests/test_dlms_logic.c` | +7 tests nuevos, 5 actualizados (39→46 tests) |

**Diff total:** 6 files changed, +514 −142

---

## Benchmark LwM2M — Rendimiento de Transporte

Benchmark ejecutado contra dispositivo real `ami-esp32c6-2434` conectado via Thread
802.15.4 a ThingsBoard Edge v4.2.1. Tres escenarios con 300s de recolección + 90s de
warmup por escenario. 16 llaves de telemetría, DLMS poll cada 15s.

**Fecha:** 2026-03-04 20:23:50  
**Datos fuente:** `results/benchmark/20260304_202350/`

### Resultados Comparativos

| Escenario | Mensajes | Throughput | IAT avg | CoAP | RSSI | LQI |
|-----------|----------|-----------|---------|------|------|-----|
| Baseline (pmin=15s) | 88 | 0.293 msgs/s | 32.11 s | 5.3 KB | -86.80 dBm | 60.40% |
| Agresivo (pmin=1s) | 246 | 0.820 msgs/s | 10.56 s | 14.9 KB | -86.99 dBm | 52.77% |
| Moderado (pmin=5s) | 81 | 0.270 msgs/s | 35.84 s | 4.9 KB | -86.25 dBm | 67.00% |

### Análisis

- **Agresivo (1s):** 2.8× más mensajes que baseline pero con IAT real de ~10.6s
  (bottleneck en el poll DLMS de 15s). LQI baja a 52.8% por saturación del canal.
- **Moderado (5s):** Similar al baseline — el intervalo de 5s queda dominado
  por el ciclo DLMS de 15s, sin beneficio real sobre baseline.
- **Baseline:** Mejor equilibrio consumo/información: LQI 60.4%, throughput
  suficiente para monitoreo de red eléctrica.

### Figuras Generadas

| Figura | Archivo | Descripción |
|--------|---------|-------------|
| Throughput | `fig_benchmark_throughput.png` | Barras comparativas msgs, msgs/s, completitud |
| IAT | `fig_benchmark_iat.png` | IAT promedio agrupado por recurso y escenario |
| Timeline | `fig_benchmark_timeline.png` | Distribución temporal scatter de telemetría |
| CoAP/RSSI | `fig_benchmark_coap_rssi.png` | Overhead CoAP + RSSI/LQI dual-axis |
| Completitud | `fig_benchmark_completeness.png` | Heatmap muestras por recurso y escenario |

---

## Cómo usar en la tesis

Los archivos `.tex` en `tablas/` se incluyen directamente:

```latex
% En el capítulo de implementación:
\input{thesis_export_v5/tablas/field_mask_v017}
\input{thesis_export_v5/tablas/protection_layers_v017}
\input{thesis_export_v5/tablas/sanity_check_v017}
\input{thesis_export_v5/tablas/thresholds_v017}

% En el capítulo de resultados:
\input{thesis_export_v5/tablas/unit_tests_v017}
\input{thesis_export_v5/tablas/new_tests_v017}
\input{thesis_export_v5/tablas/memory_v017}
\input{thesis_export_v5/tablas/benchmark_scenarios}

% Figuras de arquitectura:
\includegraphics[width=\textwidth]{thesis_export_v5/figuras/fig_field_mask}
\includegraphics[width=\textwidth]{thesis_export_v5/figuras/fig_protection_layers}
\includegraphics[width=\textwidth]{thesis_export_v5/figuras/fig_validation_flow}

% Figuras de benchmark:
\includegraphics[width=\textwidth]{thesis_export_v5/figuras/fig_benchmark_throughput}
\includegraphics[width=\textwidth]{thesis_export_v5/figuras/fig_benchmark_iat}
\includegraphics[width=\textwidth]{thesis_export_v5/figuras/fig_benchmark_timeline}
\includegraphics[width=\textwidth]{thesis_export_v5/figuras/fig_benchmark_coap_rssi}
\includegraphics[width=\textwidth]{thesis_export_v5/figuras/fig_benchmark_completeness}
```
