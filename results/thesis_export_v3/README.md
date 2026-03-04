# Thesis Export v3 — AMI LwM2M Benchmark Results

**Tesis:** Tesis_jsgiraldod_2026_rev_final  
**Dispositivo:** ami-esp32c6-2434 (ESP32-C6 + Thread)  
**Plataforma:** Zephyr RTOS → Thread mesh → TB Edge (LwM2M)  
**Fecha:** 2026-03-03  

## Estructura

```
thesis_export_v3/
├── README.md
├── comparison_report_v013_v014.txt    # Reporte textual v0.13 vs v0.14
├── comparison_report_v014_v0141.txt   # Reporte textual v0.14 vs v0.14.1 (RSSI/LQI fix)
├── datos/
│   ├── v013_benchmark/                # Firmware v0.13.0 (DLMS poll 30s)
│   │   ├── benchmark_summary.json
│   │   ├── per_key_{scenario}.csv
│   │   ├── raw_ts_{scenario}.csv
│   │   └── thesis_table.txt
│   ├── v014_benchmark/                # Firmware v0.14.0 (DLMS poll 15s)
│   │   ├── benchmark_summary.json
│   │   ├── per_key_{scenario}.csv
│   │   ├── raw_ts_{scenario}.csv
│   │   └── thesis_table.txt
│   ├── v0141_benchmark/               # Firmware v0.14.1 (RSSI/LQI fix)
│   │   ├── benchmark_summary.json
│   │   ├── per_key_{scenario}.csv
│   │   ├── raw_ts_{scenario}.csv
│   │   └── thesis_table.txt
│   └── benchmark_10s_deep/            # Prueba profunda 600s (v0.13)
│       ├── analysis_10s.json
│       ├── per_key_10s.csv
│       └── ...
├── figuras/
│   ├── v013_graficos/                 # 6 PNGs benchmark v0.13.0
│   ├── v014_graficos/                 # 6 PNGs benchmark v0.14.0
│   ├── v0141_graficos/                # 6 PNGs benchmark v0.14.1 (NEW)
│   ├── comparacion_v013_v014/         # 6 PNGs comparativo v0.13→v0.14
│   ├── comparacion_v014_v0141/        # 6 PNGs comparativo v0.14→v0.14.1 (NEW)
│   └── deep_10s/                      # 12 PNGs deep 10s analysis
└── tablas/
    ├── lwm2m_benchmark.tex
    ├── lwm2m_10s_deep.tex
    └── iat_per_key_10s.tex
```

## Firmware Versions

### v0.13.0 (Baseline)
- DLMS poll interval: 30s
- LwM2M observer notification: only on value change
- 14 resources notified after DLMS read
- No force-notify for unchanged values
- No RSSI/LQI reporting (Object 4 v1.0, version mismatch)

### v0.14.0 (Optimizado)
- DLMS poll interval: 15s (configurable 5-300s via shell)
- Force-notify using `nextafter()` trick for unchanged floats
- RSSI/LQI nudge (±1 alternating) for Object 4
- Single-phase pre-skip (`CONFIG_AMI_SINGLE_PHASE`)
- Dedicated DLMS poll thread (`K_THREAD_DEFINE`)
- Notify ALL 27 resources after DLMS read
- **Nota:** RSSI/LQI = 0 muestras por version mismatch Object 4

### v0.14.1 (Fix RSSI/LQI)
- `CONFIG_LWM2M_CONNMON_OBJECT_VERSION_1_0=y` → `_1_3=y`
- **Causa raiz:** TB Edge usa rutas `/4_1.3/0/x` (v1.3), dispositivo
  registraba Object 4 como v1.0 → observe nunca coincide
- **Resultado:** 14/16 → 16/16 llaves reportando
- radioSignalStrength (RSSI): ~-80 dBm
- linkQuality (LQI): ~66-67%

## Benchmark Protocol

Each benchmark consists of 4 "observe interval" scenarios:
1. **Baseline** — Production config: pmin=15/pmax=30 (Grupo1), pmin=60/pmax=300 (Grupo2)
2. **1s** — Aggressive: pmin=1/pmax=1 for all keys
3. **5s** — Moderate: pmin=5/pmax=5 for all keys
4. **10s** — Conservative: pmin=10/pmax=10 for all keys

Each scenario: 30s warmup + 300s data collection. DLMS poll every 15s (v0.14+).

## Key Results

| Version | Keys | Throughput (1s) | RSSI | LQI |
|---------|------|-----------------|------|-----|
| v0.13.0 | 14/16 | 0.303 msgs/s | N/A | N/A |
| v0.14.0 | 14/16 | 0.260 msgs/s | 0 samples | 0 samples |
| v0.14.1 | **16/16** | **1.693 msgs/s** | **-80 dBm** | **67%** |

## Files: 101 total
