# Thesis Export — Datos Experimentales AMI LwM2M
**Tesis:** Tesis_jsgiraldod_2026_rev_final  
**Fecha de generación:** 2026-03-03  
**Firmware:** v0.13.0 (`notify_all_observers()`, configurable `notify_interval`)

## Resumen del Experimento

Sistema AMI completo: **ESP32-C6 → Thread 802.15.4 → RPi4 Border Router → ThingsBoard Edge**

### Benchmark Comparativo (3 escenarios)
- **Duración por escenario:** 300s (5 min), Warmup: 90s
- **Escenarios:** Baseline (pmin=15-60s), Agresivo (1s), Medio (5s)
- **Directorio fuente:** `results/benchmark/20260303_184204/`
- **Nota:** Escenario Relajado (10s) eliminado — pmax < DLMS\_poll causa 0 mensajes en v0.15.1

| Escenario | Msgs | Msgs/s | IAT avg (s) | CoAP (KB) | Keys |
|-----------|------|--------|-------------|-----------|------|
| Baseline  | 76   | 0.253  | 47.30       | 4.6       | 13/16|
| 1s        | 84   | 0.280  | 58.20       | 5.1       | 14/16|
| 5s        | 79   | 0.263  | 50.79       | 4.8       | 13/16|

**Hallazgo clave:** El intervalo DLMS de 30s domina sobre pmin/pmax de LwM2M. El throughput permanece en ~0.26 msgs/s independientemente del intervalo de observación configurado.

### Análisis Profundo (escenario 10s)
- **Duración:** 600s (10 min), Warmup: 90s
- **Incluye:** Métricas de sistema (SSH), Docker stats, 12 gráficas
- **Directorio fuente:** `results/benchmark_10s/20260303_191806/`

| Métrica | Valor |
|---------|-------|
| Total mensajes | 160 |
| Throughput | 0.267 msgs/s |
| IAT promedio | 52.47s |
| IAT p95 | 97.50s |
| Vol. radio | 19.84 KB |
| Utilización canal | 0.22% |
| Nodos máx estimados | 461 |

## Estructura de Archivos

```
thesis_export/
├── README.md                      ← Este archivo
├── figuras/
│   ├── comparativo/               ← 6 gráficas del benchmark comparativo
│   │   ├── fig_throughput.png
│   │   ├── fig_completeness.png
│   │   ├── fig_iat_boxplot.png
│   │   ├── fig_coap_overhead.png
│   │   ├── fig_rssi_lqi.png
│   │   └── fig_iat_per_key.png
│   └── deep_10s/                  ← 12 gráficas del análisis profundo
│       ├── 01_message_rate_timeline.png
│       ├── 02_iat_distribution.png
│       ├── 03_iat_per_key_boxplot.png
│       ├── 04_cumulative_data_volume.png
│       ├── 05_protocol_overhead.png
│       ├── 06_network_utilization.png
│       ├── 07_completeness_per_key.png
│       ├── 08_data_rate_timeline.png
│       ├── 09_jitter_per_key.png
│       ├── 10_docker_resources.png
│       ├── 11_summary_table.png
│       └── 12_system_resources.png
├── datos/
│   ├── benchmark_4escenarios/     ← Datos crudos del comparativo
│   │   ├── per_key_{baseline,1s,5s,10s}.csv
│   │   ├── raw_ts_{baseline,1s,5s,10s}.csv
│   │   ├── benchmark_summary.json
│   │   └── thesis_table.txt
│   └── benchmark_10s_deep/        ← Datos del análisis profundo
│       ├── per_key_10s.csv
│       ├── raw_ts_10s.csv
│       ├── docker_stats.csv
│       ├── system_stats.csv
│       ├── analysis_10s.json
│       └── thesis_summary_10s.txt
└── tablas/                        ← Tablas LaTeX listas para copiar
    ├── lwm2m_benchmark.tex        ← Tabla comparativa 4 escenarios
    ├── lwm2m_10s_deep.tex         ← Tabla métricas escenario 10s
    └── iat_per_key_10s.tex        ← Tabla IAT por recurso OBIS
```

## Configuración del Sistema de Medición

| Componente | Valor |
|------------|-------|
| MCU | ESP32-C6 (RISC-V, 160 MHz) |
| Radio | IEEE 802.15.4 (Thread) |
| MTU 802.15.4 | 127 bytes |
| Protocolo IoT | LwM2M / CoAP |
| Tamaño msg CoAP | 62 bytes |
| Polling DLMS | RS485 9600-8N1, cada 30s |
| Códigos OBIS | 22 (16 como telemetría) |
| Border Router | RPi4 (ot-br-posix) |
| Plataforma Edge | ThingsBoard Edge v4.2.1 |
| OS Firmware | Zephyr RTOS v4.1.0 |

## Cómo Reproducir

```bash
# Activar entorno virtual
.venv\Scripts\activate

# Benchmark comparativo (4 escenarios × 300s)
cd zephyrproject/ami-lwm2m-node
python tools/benchmark_lwm2m.py --duration 300 --warmup 90

# Análisis profundo escenario 10s (600s)
python tools/benchmark_10s_deep.py --duration 600 --warmup 90 --format png

# Regenerar gráficas comparativas
python tools/graph_benchmark.py results/benchmark/<timestamp>
```
