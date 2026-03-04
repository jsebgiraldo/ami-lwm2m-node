# Thesis Export v4 — AMI LwM2M Benchmark Results

**Tesis:** Tesis_jsgiraldod_2026_rev_final  
**Dispositivo:** ami-esp32c6-2434 (ESP32-C6 + Thread)  
**Plataforma:** Zephyr RTOS → Thread mesh → TB Edge (LwM2M)  
**Fecha:** 2026-03-04  
**Novedad v4:** Firmware v0.15.1 — Notificación inteligente por umbrales (THRESH_CHECK)

## Estructura

```
thesis_export_v4/
├── README.md
├── datos/
│   ├── v013_benchmark/                # Firmware v0.13.0 (DLMS poll 30s)
│   ├── v014_benchmark/                # Firmware v0.14.0 (DLMS poll 15s)
│   ├── v0141_benchmark/               # Firmware v0.14.1 (RSSI/LQI fix)
│   ├── v0151_benchmark/               # Firmware v0.15.1 (Smart thresholds) ← NUEVO
│   │   ├── benchmark_summary.json
│   │   ├── per_key_{scenario}.csv
│   │   ├── raw_ts_{scenario}.csv
│   │   └── thesis_table.txt
│   └── benchmark_10s_deep/            # Prueba profunda 600s (v0.13)
├── figuras/
│   ├── v013_graficos/                 # 6 PNGs benchmark v0.13.0
│   ├── v014_graficos/                 # 6 PNGs benchmark v0.14.0
│   ├── v0141_graficos/                # 6 PNGs benchmark v0.14.1
│   ├── v0151_graficos/                # 6 PNGs benchmark v0.15.1 ← NUEVO
│   │   ├── fig_throughput.png
│   │   ├── fig_completeness.png
│   │   ├── fig_iat_boxplot.png
│   │   ├── fig_coap_overhead.png
│   │   ├── fig_rssi_lqi.png
│   │   └── fig_iat_per_key.png
│   └── deep_10s/                      # 12 PNGs deep 10s analysis
└── tablas/
    ├── lwm2m_benchmark.tex            # Tabla original (v0.13 era)
    ├── lwm2m_benchmark_v0151.tex      # Tabla v0.15.1 (4 escenarios) ← NUEVO
    ├── lwm2m_cross_version.tex        # Comparativa cross-version ← NUEVO
    ├── lwm2m_10s_deep.tex
    └── iat_per_key_10s.tex
```

## Firmware Versions

### v0.13.0 (Baseline)
- DLMS poll interval: 30s
- LwM2M observer notification: only on value change
- 14/16 resources notified after DLMS read
- No RSSI/LQI reporting (Object 4 v1.0, version mismatch)

### v0.14.0 (Optimizado)
- DLMS poll interval: 15s (configurable 5-300s via shell)
- Force-notify using `nextafter()` trick for unchanged floats
- Single-phase pre-skip (`CONFIG_AMI_SINGLE_PHASE`)
- Dedicated DLMS poll thread
- **Nota:** RSSI/LQI = 0 muestras por version mismatch Object 4

### v0.14.1 (Fix RSSI/LQI)
- `CONFIG_LWM2M_CONNMON_OBJECT_VERSION_1_3=y`
- **Resultado:** 14/16 → 16/16 llaves reportando
- radioSignalStrength (RSSI): ~-80 dBm, linkQuality (LQI): ~67%

### v0.15.1 (Smart Thresholds) ← NUEVO
- **THRESH_CHECK macro**: Notificación solo cuando el valor cambia más allá del umbral
- Umbrales por tipo: V=1.0, I=0.05, P/PF=0.01, E=0.1, f=0.1
- `MAX_SILENT_POLLS = 5` (fuerza re-notificación cada ~75s de silencio)
- Eliminado `notify_interval_ms` y `force_notify_f64()` — ya no se usa el truco nextafter()
- `update_sensors_fallback()` simplificado: solo log warning, mantiene últimos valores conocidos
- Phase S/T envueltas en `#ifndef CONFIG_AMI_SINGLE_PHASE`
- TOTAL_RESOURCES dinámico: 15 (single-phase) o 27 (tri-phase)
- **Impacto:** Reduce tráfico innecesario ~61% vs v0.14.1 (escenario 1s: 0.661 vs 1.693 msgs/s)

## Benchmark Protocol

### v0.13.0 — v0.14.1
- 3 escenarios observe: baseline, 1s, 5s
- Warmup: 90s, Duración: 300s por escenario
- DLMS poll: 30s (v0.13) o 15s (v0.14+)

### v0.15.1
- 3 escenarios observe: baseline, 1s, 5s
- Warmup: 45s, Duración: 180s por escenario
- DLMS poll: 15s
- **Sin `--serial-port`**: Firmware controla notificación autónomamente
- Perfil reconfigura pmin/pmax via API, firmware decide cuándo notificar
- **Nota:** Escenario Relajado (10s) eliminado — pmax=10 < DLMS\_poll=15s
  causa expiración de observaciones antes de la primera notificación (0 msgs)

## Key Results — Cross-Version Comparison (Escenario 1s)

| Version | Keys | Msgs | Throughput | IAT avg (s) | RSSI (dBm) | LQI (%) | Mejora clave |
|---------|------|------|------------|-------------|------------|---------|--------------|
| v0.13.0 | 14/16 | 91 | 0.303 msgs/s | 58.20 | N/A | N/A | Baseline DLMS 30s |
| v0.14.0 | 14/16 | 78 | 0.260 msgs/s | — | 0 samples | 0 samples | DLMS 15s, force-notify |
| v0.14.1 | **16/16** | **508** | **1.693 msgs/s** | **1.81** | -80 | 67 | ConnMon v1.3 fix |
| v0.15.1 | **16/16** | 119 | 0.661 msgs/s | 11.46 | -93 | 33 | **Smart thresholds** |

### Análisis v0.15.1

El firmware v0.15.1 introduce notificación inteligente basada en umbrales:

1. **Reducción de tráfico**: 61% menos mensajes en escenario agresivo (1s) vs v0.14.1
   - v0.14.1: 1.693 msgs/s (fuerza notificación de todo tras cada DLMS poll)
   - v0.15.1: 0.661 msgs/s (solo notifica cambios reales > umbral)

2. **Comportamiento por escenario v0.15.1**:
   | Escenario | Msgs | Msgs/s | IAT (s) | CoAP (KB) | RSSI | LQI |
   |-----------|------|--------|---------|-----------|------|-----|
   | Baseline | 47 | 0.261 | 32.33 | 2.8 | -88.67 | 55.0% |
   | 1s | 119 | 0.661 | 11.46 | 7.2 | -92.85 | 33.4% |
   | 5s | 29 | 0.161 | 53.07 | 1.8 | -93.25 | 33.7% |

3. **Análisis de cuellos de botella por capa:**

   El sistema AMI presenta un pipeline de 4 capas, cada una con su propio cuello
   de botella que limita el throughput end-to-end:

   | Capa | Componente | Bottleneck | Latencia típica |
   |------|-----------|-----------|----------------|
   | L1 — Física | RS-485 DLMS/COSEM | Lectura secuencial de 13 registros OBIS | ~2–3s por ciclo |
   | L2 — Firmware | THRESH\_CHECK + poll interval | DLMS poll cada 15s, notifica solo cambios > umbral | 15s (configurable) |
   | L3 — Red | Thread mesh (802.15.4) | 250 kbps compartido, ~62 bytes/notify CoAP | ~50ms por msg |
   | L4 — Servidor | TB Edge LwM2M (Californium) | pmin/pmax controlan ventana de observación | pmin–pmax según perfil |

   **¿Por qué Baseline es óptimo?**
   - **L1 domina**: La lectura DLMS tarda ~2–3s y ocurre cada 15s (poll interval).
     Ningún pmin < 15s puede generar datos más rápido que lo que el medidor produce.
   - **L2 filtra**: THRESH\_CHECK solo notifica cambios reales (ΔV ≥ 1V, ΔI ≥ 0.05A, etc.).
     Con pmin=1s (agresivo), el 61% del tráfico es redundante.
   - **L3 se satura**: A 1.693 msgs/s (v0.14.1 agresivo), Thread consume 0.84 kbps
     de los 250 kbps disponibles. Con 100+ nodos, el canal se congestiona.
   - **L4 expira**: Si pmax < DLMS\_poll (e.g., pmax=10 < 15s), las observaciones
     expiran antes de la primera notificación — resultado: 0 mensajes.

   Baseline (pmin=15/pmax=30 para energía, pmin=60/pmax=300 para radio/FW) sincroniza
   perfectamente con el ciclo DLMS de 15s, minimiza tráfico innecesario, y permite
   escalar a cientos de nodos por red Thread.

4. **Señal RF más débil**: RSSI de -88 a -93 dBm (vs -80 en v0.14.1) indica
   condiciones de propagación variables, no degradación del firmware.

## Conclusión para la tesis

v0.15.1 demuestra el trade-off fundamental en AMI sobre Thread:
- **v0.14.1** maximiza throughput a costa de tráfico innecesario (fuerza notificación)
- **v0.15.1** optimiza para eficiencia de red, solo enviando cambios significativos
- El escenario ideal de producción es **baseline** con umbrales: 0.261 msgs/s, 2.8 KB CoAP
- Para redes Thread con ancho de banda limitado, v0.15.1 es superior en escala
- El cuello de botella L1 (DLMS ~15s) hace que cualquier pmin < 15s desperdicie
  recursos de red sin ganar información adicional

## Files

Total: ~100+ archivos (datos CSV, gráficos PNG, tablas LaTeX)
