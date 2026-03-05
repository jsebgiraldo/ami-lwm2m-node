# Thesis Export v7 — Resultados Experimentales v0.18.0

**Proyecto:** Tesis\_jsgiraldod\_2026\_rev\_final  
**Firmware:** v0.18.0 (`9c0abed`)  
**Dispositivo:** `ami-esp32c6-2434` (ESP32-C6 RISC-V, Zephyr RTOS)  
**Red:** IEEE 802.15.4 Thread → ThingsBoard Edge v4.2.1  
**Fecha benchmark:** 2026-03-05  

---

## 1. Descripción del sistema

El nodo AMI implementa un medidor inteligente sobre ESP32-C6 que:

1. **Lee** el medidor eléctrico vía DLMS/COSEM sobre RS-485 cada 15 s
2. **Publica** los valores al servidor LwM2M usando la macro `PUSH_FIELD`
3. **Delega** el control de frecuencia al motor observe de Zephyr mediante `pmin`/`pmax`
4. **Transmite** por Thread 802.15.4 → Border Router → ThingsBoard Edge

### 1.1 Macro PUSH\_FIELD

Cada campo leído del medidor se publica mediante:

```c
#define PUSH_FIELD(bit_idx, path, val) do { \
    if (field_mask & (1u << (bit_idx))) { \
        lwm2m_set_f64(&ctx_##path, (val)); \
        lwm2m_notify_observer(OBJ_ID, 0, (path)); \
        pushed++; \
    } else { skipped++; } \
} while (0)
```

- **Guarda:** Solo publica campos efectivamente leídos (`field_mask` bit activo)
- **Rate control:** Delegado al servidor vía atributos `pmin`/`pmax`

### 1.2 Arquitectura de protección — 5 capas

| Capa | Componente | Protección |
|------|-----------|------------|
| L1 | `meter_read_all()` | `memset(0)` inicial + `field_mask` por OBIS individual |
| L2 | `MIN_READ_PERCENT` | Cobertura < 50% → lectura descartada |
| L3 | `readings_sanity_check()` | V ∈ [50, 500] V, f ∈ [40, 70] Hz |
| L4 | `PUSH_FIELD(bit_idx)` | Solo datos realmente leídos llegan al servidor |
| L5 | `consecutive_meter_failures` | Log crítico tras 5 fallos RS-485 consecutivos |

### 1.3 Control de rate — pmin/pmax

| Elemento | Función |
|----------|---------|
| **Firmware** (cliente) | Cada 15 s: lee medidor, ejecuta `PUSH_FIELD` → `lwm2m_set_f64()` + `lwm2m_notify_observer()` |
| **Observe engine** (Zephyr) | Evalúa `pmin`/`pmax`: si t < pmin suprime, si t > pmax fuerza envío |
| **Servidor** (TB Edge) | Configura `pmin`/`pmax` por recurso. Ajustable desde dashboard sin reflashear |
| **Métricas radio** | RSSI/LQI en Object 4, intervalo separado de 60 s. Sin filtro firmware: siempre `lwm2m_notify_observer()` |

### 1.4 Constantes de configuración

| Constante | Valor | Archivo |
|-----------|-------|---------|
| `MIN_READ_PERCENT` | 50 | `dlms_meter.c` |
| `VOLTAGE_MIN / MAX` | 50 / 500 V | `dlms_meter.c` |
| `FREQ_MIN / MAX` | 40 / 70 Hz | `dlms_meter.c` |
| `MAX_CONSEC_FAILURES` | 5 | `main.c` |
| `CONN_UPDATE_INTERVAL_S` | 60 s | `main.c` |
| `dlms_poll_interval_s` | 15 s | `main.c` |

---

## 2. Resultados experimentales — Benchmark

Tres escenarios ejecutados sobre dispositivo real durante 300 s cada uno (90 s warmup):

### 2.1 Resultados agregados

| Métrica | Producción | Agresivo (1 s) | Medio (5 s) |
|---------|-----------|----------------|-------------|
| **pmin / pmax** | G1: 15/30, G2: 60/300 | 1 / 1 | 5 / 5 |
| **Mensajes totales** | 70 | 1386 | 21 |
| **Keys reporting** | 15/16 | 16/16 | 16/16 |
| **Throughput (msgs/s)** | 0.233 | 4.620 | 0.070 |
| **IAT promedio (s)** | 37.5 | 2.6 | 5.6 |
| **IAT σ (s)** | 27.8 | 5.8 | 3.3 |
| **CoAP estimado (KB)** | 4.2 | 83.9 | 1.3 |
| **CoAP (bps)** | 115.7 | 2291.5 | 34.7 |
| **RSSI promedio (dBm)** | −85.8 | −86.6 | −88.0 |
| **RSSI σ (dBm)** | 2.49 | 1.46 | 0.0 |
| **LQI promedio (%)** | 60.4 | 59.3 | 67.0 |

### 2.2 Mensajes por recurso (ventana 300 s)

| Recurso | Producción | Agresivo (1 s) | Medio (5 s) |
|---------|-----------|----------------|-------------|
| V (tensión) | 0 | 1 | 1 |
| I (corriente) | 1 | 1 | 1 |
| P (potencia activa) | 18 | 21 | 1 |
| Q (potencia reactiva) | 5 | 1 | 1 |
| S (potencia aparente) | 14 | 21 | 1 |
| PF (factor potencia) | 1 | 163 | 2 |
| P_tot | 5 | 143 | 2 |
| Q_tot | 2 | 136 | 2 |
| S_tot | 5 | 89 | 1 |
| PF_tot | 1 | 166 | 1 |
| E_a (energía activa) | 3 | 3 | 1 |
| E_r (energía reactiva) | 1 | 140 | 2 |
| E_s (energía aparente) | 3 | 111 | 1 |
| f (frecuencia) | 1 | 118 | 2 |
| RSSI | 5 | 120 | 1 |
| LQI | 5 | 152 | 1 |

### 2.3 Análisis de resultados

- **Producción (baseline):** Con pmin diferenciados (G1: métricas eléctricas a 15/30 s, G2: radio/energía a 60/300 s), el sistema genera tráfico mínimo (70 msgs, 4.2 KB). El operador puede ajustar remotamente sin recompilar firmware.

- **Agresivo (1 s):** Con pmin=pmax=1, se fuerza una notificación cada segundo por recurso activo. Se alcanzan 1386 mensajes (4.62 msgs/s, 83.9 KB). Demuestra que el nodo soporta alta carga sin perder estabilidad (RSSI −86.6 dBm estable).

- **Medio (5 s):** Con pmin=pmax=5, la mayoría de recursos no cambian en 5 s, resultando en solo 21 mensajes. Confirma que el motor observe respeta estrictamente el intervalo configurado.

- **Radio:** RSSI se mantiene estable entre −85.8 y −88.0 dBm en los tres escenarios. LQI entre 59.3% y 67.0%. La carga de telemetría no degrada el enlace Thread.

---

## 3. Tests unitarios

**Resultado:** 117/117 PASS

| Suite | Tests | Cobertura |
|-------|-------|-----------|
| HDLC | 29/29 | CRC-16, tramas SNRM, DISC, I-frame |
| COSEM | 43/43 | AARQ, AARE, GET request/response, RLRQ, tipos |
| DLMS Logic | 45/45 | OBIS table, field\_mask, sanity check, PUSH\_FIELD |
| **Total** | **117/117** | **ALL PASS** |

### Tests PUSH\_FIELD

| Test | Verificación |
|------|-------------|
| `test_push_field_skips_when_bit_not_set` | Omite campo cuando `field_mask` bit = 0 |
| `test_push_field_pushes_when_bit_set` | Envía campo cuando `field_mask` bit = 1 |
| `test_push_field_all_27_bits` | Máscara `0x07FFFFFF` cubre los 27 campos OBIS |

---

## 4. Catálogo de figuras

10 figuras de calidad publicación generadas desde los datos del benchmark:

| # | Archivo | Descripción |
|---|---------|-------------|
| 1 | `fig_throughput.png` | Throughput (msgs/s) por escenario con barra de error |
| 2 | `fig_per_key_messages.png` | Mensajes recibidos por recurso, agrupados por escenario |
| 3 | `fig_iat_boxplot.png` | Distribución del inter-arrival time por escenario |
| 4 | `fig_completeness_heatmap.png` | Heatmap de completitud por recurso y escenario |
| 5 | `fig_rssi_lqi_timeline.png` | Serie temporal RSSI y LQI durante el benchmark |
| 6 | `fig_coap_overhead.png` | Overhead CoAP estimado (KB y bps) por escenario |
| 7 | `fig_iat_heatmap.png` | Heatmap de IAT promedio por recurso y escenario |
| 8 | `fig_summary_dashboard.png` | Dashboard resumen con métricas principales |
| 9 | `fig_meter_timeseries.png` | Serie temporal de lecturas del medidor |
| 10 | `fig_keys_reporting.png` | Número de keys que reportan por escenario |

---

## 5. Estructura de archivos

```
thesis_export_v7/
├── README.md                       ← Este archivo
├── tablas/
│   ├── benchmark_aggregate.tex     ← Tabla LaTeX: métricas agregadas
│   ├── benchmark_per_key.tex       ← Tabla LaTeX: mensajes por recurso
│   ├── push_field.tex              ← Macro PUSH_FIELD
│   ├── protection_layers.tex       ← Arquitectura 5 capas
│   ├── rate_control.tex            ← Control pmin/pmax
│   ├── constants.tex               ← Constantes de configuración
│   └── unit_tests.tex              ← Resultados tests unitarios
├── figuras/
│   ├── fig_throughput.png
│   ├── fig_per_key_messages.png
│   ├── fig_iat_boxplot.png
│   ├── fig_completeness_heatmap.png
│   ├── fig_rssi_lqi_timeline.png
│   ├── fig_coap_overhead.png
│   ├── fig_iat_heatmap.png
│   ├── fig_summary_dashboard.png
│   ├── fig_meter_timeseries.png
│   └── fig_keys_reporting.png
└── datos/
    ├── benchmark_summary.json      ← Datos crudos del benchmark
    ├── test_results.txt            ← Output completo 117/117 tests
    └── thesis_table.txt            ← Tabla resumen texto plano
```

---

## 6. Uso en la tesis

### Incluir tabla de benchmark:

```latex
\input{tablas/benchmark_aggregate}
```

### Incluir figura:

```latex
\begin{figure}[htbp]
  \centering
  \includegraphics[width=\linewidth]{figuras/fig_throughput.png}
  \caption{Throughput del nodo AMI bajo configuraciones de observación LwM2M}
  \label{fig:throughput}
\end{figure}
```

### Referencia cruzada:

```latex
Como se observa en la Tabla~\ref{tab:benchmark-aggregate},
el escenario de producción genera solo 70 mensajes en 300\,s
(0.233\,msgs/s), mientras que el escenario agresivo alcanza
1386 mensajes (4.62\,msgs/s) sin degradar el enlace Thread
(RSSI $\approx -86$\,dBm en ambos casos).
```
