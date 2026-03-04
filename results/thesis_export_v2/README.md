# Thesis Export v2 — AMI LwM2M Benchmark Results

**Tesis:** Tesis_jsgiraldod_2026_rev_final  
**Dispositivo:** ami-esp32c6-2434 (ESP32-C6 + Thread)  
**Plataforma:** Zephyr RTOS → Thread mesh → TB Edge (LwM2M)  
**Fecha:** 2026-03-03  

## Estructura

```
thesis_export_v2/
├── README.md
├── comparison_report.txt          # Reporte textual v0.13 vs v0.14
├── datos/
│   ├── v013_benchmark/            # Firmware v0.13.0 (DLMS poll 30s)
│   │   ├── benchmark_summary.json
│   │   ├── per_key_{scenario}.csv
│   │   ├── raw_ts_{scenario}.csv
│   │   └── thesis_table.txt
│   ├── v014_benchmark/            # Firmware v0.14.0 (DLMS poll 15s)
│   │   ├── benchmark_summary.json
│   │   ├── per_key_{scenario}.csv
│   │   ├── raw_ts_{scenario}.csv
│   │   └── thesis_table.txt
│   └── benchmark_10s_deep/        # Prueba profunda 600s (v0.13)
│       ├── analysis_10s.json
│       ├── per_key_10s.csv
│       └── ...
├── figuras/
│   ├── v013_graficos/             # 6 PNGs benchmark v0.13.0
│   ├── v014_graficos/             # 6 PNGs benchmark v0.14.0
│   ├── comparacion_v013_v014/     # 6 PNGs + reporte comparativo
│   └── deep_10s/                  # 12 PNGs deep 10s analysis
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
- No RSSI/LQI reporting

### v0.14.0 (Optimizado)
- DLMS poll interval: 15s (configurable 5-300s via shell)
- Force-notify using `nextafter()` trick for unchanged floats
- RSSI/LQI nudge (±1 alternating) for Object 4
- Single-phase pre-skip (`CONFIG_AMI_SINGLE_PHASE`)
- Dedicated DLMS poll thread (`K_THREAD_DEFINE`)
- Notify ALL 27 resources after DLMS read

## Benchmark Protocol

Each benchmark consists of 4 "observe interval" scenarios:
1. **Baseline** — Production config: pmin=15/pmax=30 (Grupo1), pmin=60/pmax=300 (Grupo2)
2. **Agresivo (1s)** — pmin=1/pmax=1 for all keys
3. **Medio (5s)** — pmin=5/pmax=5 for all keys  
4. **Relajado (10s)** — pmin=10/pmax=10 for all keys

Each scenario: 90s warmup + 300s data collection. Profile is restored to production after each.

## Key Finding

The LwM2M server-side observe interval (~50-55s effective IAT) is the dominant
bottleneck. Reducing the device-side DLMS poll from 30s to 15s had minimal impact
on observed throughput (~0.26 msgs/s in both versions), confirming that the
ThingsBoard Edge LwM2M implementation controls the pacing regardless of how
frequently the device updates its resource values.

## Telemetry Keys (16)

| Key | Description |
|-----|-------------|
| voltage | Tensión de fase (V) |
| current | Corriente de fase (A) |
| activePower | Potencia activa (W) |
| reactivePower | Potencia reactiva (var) |
| apparentPower | Potencia aparente (VA) |
| powerFactor | Factor de potencia |
| totalActivePower | Potencia activa total |
| totalReactivePower | Potencia reactiva total |
| totalApparentPower | Potencia aparente total |
| totalPowerFactor | Factor de potencia total |
| activeEnergy | Energía activa (Wh) |
| reactiveEnergy | Energía reactiva (varh) |
| apparentEnergy | Energía aparente (VAh) |
| frequency | Frecuencia (Hz) |
| radioSignalStrength | RSSI Thread (dBm) |
| linkQuality | LQI Thread (0-255) |
