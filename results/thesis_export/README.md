# Resultados Experimentales — Tesis AMI Smart Energy Metering

**Tesis**: `Tesis_jsgiraldod_2026_rev_final`  
**Autor**: Juan Sebastián Giraldo  
**Fecha de generación**: 3 de marzo de 2026  
**Repositorio fuente**: `ami-lwm2m-node` (commit `3c932af`)

---

## 1. Descripción del Sistema

Sistema de medición inteligente (AMI — Advanced Metering Infrastructure) que conecta
un medidor eléctrico industrial a una plataforma IoT cloud usando red mesh inalámbrica
**Thread 802.15.4** y el protocolo **LwM2M**.

### Arquitectura (5 capas)

```
┌─────────────────────┐
│   ThingsBoard Cloud  │   Capa 4: Plataforma Cloud (CE 4.2.1.1)
│   192.168.1.159:80   │   Gestión centralizada, dashboards, analytics
└──────────┬──────────┘
           │ gRPC:7070
┌──────────▼──────────┐
│   ThingsBoard Edge   │   Capa 3: Gateway Edge (RPi4, Docker)
│   192.168.1.111:8090 │   Eclipse Leshan (LwM2M server), PostgreSQL
│   LwM2M: 5683/udp   │   Rule engine local, sync bidireccional
└──────────┬──────────┘
           │ IPv6 mesh-local (CoAP/LwM2M NoSec)
┌──────────▼──────────┐
│   OTBR (Border Router)│  Capa 2: Puente Thread ↔ LAN
│   RPi4 (OpenWrt)      │  Thread Ch25, PAN 0xABCD, mesh-local fdc6:63fd:328d:66df::/64
└──────────┬──────────┘
           │ IEEE 802.15.4 (Thread mesh)
┌──────────▼──────────┐
│   XIAO ESP32-C6      │   Capa 1: Nodo Sensor (Zephyr RTOS 4.3.99)
│   Cliente LwM2M      │   OpenThread Router, parser DLMS/COSEM
│   ami-esp32c6-2434   │   Driver RS485 half-duplex
└──────────┬──────────┘
           │ RS485 (9600 8N1, DLMS/COSEM)
┌──────────▼──────────┐
│   Microstar C2000    │   Capa 0: Medidor Eléctrico Monofásico
│   22 registros OBIS  │   Tensión, corriente, potencia, energía, frecuencia
└─────────────────────┘
```

### Hardware

| Componente | Modelo | Función |
|-----------|--------|---------|
| Medidor | Microstar C2000 Monofásico | Fuente de datos DLMS/COSEM |
| Nodo IoT | Seeed XIAO ESP32-C6 | MCU RISC-V con radio 802.15.4 nativa |
| Border Router | Raspberry Pi 4 (OpenWrt) | OTBR + TB Edge (Docker) |
| Cloud | PC on-premise | ThingsBoard CE 4.2.1.1 |

### Software del Nodo (Firmware v0.13.0)

| Componente | Versión |
|-----------|---------|
| Zephyr RTOS | 4.3.99 |
| OpenThread | Incluido en Zephyr |
| LwM2M Client | Eclipse Wakaama (vía Zephyr) |
| Parser DLMS | Custom (HDLC + COSEM) |
| Tests unitarios | 98/98 pasando (HDLC: 29, COSEM: 43, DLMS Logic: 26) |

### Constantes de Protocolo

| Parámetro | Valor |
|-----------|-------|
| Tamaño mensaje CoAP | 62 bytes |
| Trama IEEE 802.15.4 | 127 bytes (MTU) |
| Payload CBOR promedio | ~30 bytes |
| Bitrate 802.15.4 raw | 250 kbps |
| Bitrate 802.15.4 efectivo | ~125 kbps |
| DLMS polling interval | 30 segundos |
| LwM2M Lifetime | 300 segundos |

---

## 2. Resumen de Experimentos

Se realizaron dos tipos de benchmarks:

### 2.1. Benchmark Comparativo (4 escenarios)

**Script**: `tools/benchmark_lwm2m.py` (~1180 líneas)  
**Duración por escenario**: 300s colección + 90s warmup  
**Fecha**: 3 de marzo de 2026  
**Directorio**: `datos/benchmark_4escenarios/`

Compara 4 intervalos de observación LwM2M:

| Escenario | pmin/pmax | Descripción |
|-----------|-----------|-------------|
| **Baseline** | Grupo1: 15/30s, Grupo2: 60/300s | Configuración de producción |
| **Agresivo (1s)** | 1/1s uniforme | Máxima resolución temporal |
| **Medio (5s)** | 5/5s uniforme | Balance resolución/eficiencia |
| **Relajado (10s)** | 10/10s uniforme | Intervalo conservador |

#### Resultados Principales

| Escenario | Msgs | Compl% | Msgs/s | IAT avg (s) | CoAP (KB) | CoAP (bps) |
|-----------|------|--------|--------|-------------|-----------|------------|
| Baseline | 32 | 61.5% | 0.107 | 38.79 | 1.9 | 52.9 |
| Agresivo (1s) | 264 | 5.5% | 0.880 | 3.63 | 16.0 | 436.5 |
| **Medio (5s)** | **1482** | **154.4%** | **4.940** | **2.50** | **89.7** | **2450.2** |
| Relajado (10s) | 121 | 25.2% | 0.403 | 11.45 | 7.3 | 200.0 |

#### Hallazgo Clave: CoAP Congestion Cliff

El escenario de **5 segundos supera al de 1 segundo** en throughput real.
Con pmin=1s y 16 recursos, el nodo genera 16 CoAP Notify/s. La red Thread
con MTU de 127 bytes y overhead de fragmentación IPv6 no puede sostener este
flujo, causando retransmisiones y pérdida de paquetes.

El punto óptimo está en **5s**: suficiente para que cada notificación se
transmita y confirme antes de la siguiente, maximizando el throughput efectivo.

### 2.2. Análisis Profundo 10s (escenario único)

**Script**: `tools/benchmark_10s_deep.py` (~1563 líneas)  
**Duración**: 600s colección + 90s warmup  
**Fecha**: 3 de marzo de 2026  
**Directorio**: `datos/benchmark_10s_deep/`

Análisis exhaustivo del escenario de 10 segundos con métricas del sistema Edge
recopiladas vía SSH (paramiko) al Raspberry Pi 4.

#### Resultados Principales

| Métrica | Valor | Unidad |
|---------|-------|--------|
| Total mensajes | 724 | msgs |
| Llaves activas | 13/16 | — |
| Throughput | 1.2067 | msgs/s |
| IAT promedio | 11.83 | s |
| IAT mediana | 10.00 | s |
| IAT p95 | 30.13 | s |
| Volumen CoAP | 43.84 | KB |
| Volumen radio (802.15.4) | 89.79 | KB |
| Data rate radio | 1.226 | kbit/s |
| Eficiencia payload | 23.6 | % |
| Utilización del canal | 0.98 | % |
| **Nodos máx. estimados** | **101** | nodos/mesh |

#### Proyección Horaria

| Métrica | Valor |
|---------|-------|
| Mensajes por hora | 4,344 |
| Radio KB por hora | 538.8 |

#### Métricas del Sistema Edge (RPi4)

| Métrica | Rango | Promedio |
|---------|-------|---------|
| CPU load (1min avg) | 0.01 – 0.42 | ~0.15 |
| Memoria usada | 39.2 – 39.6% | 39.4% (~1.4 GB / 3.8 GB) |
| Temperatura CPU | 52.6 – 55.0°C | 53.5°C |
| wpan0 RX (Thread) delta | — | ~54 KB en 600s |
| wpan0 TX (Thread) delta | — | ~32 KB en 600s |
| eth0 RX (LAN) delta | — | ~938 KB en 600s |
| eth0 TX (LAN) delta | — | ~1,272 KB en 600s |

#### Comportamiento por Llave de Telemetría

Tres categorías de comportamiento observadas:

1. **Llaves rápidas (~10s IAT)**: `apparentPower`, `powerFactor`, `totalActivePower`,
   `totalReactivePower`, `totalApparentPower`, `totalPowerFactor`, `activeEnergy`,
   `reactiveEnergy`, `apparentEnergy`, `frequency` — responden al pmin=10s correctamente.
   Completeness >100% indica que el firmware entregó más muestras de las esperadas.

2. **Llaves lentas (~30s IAT)**: `voltage`, `current`, `activePower` — estas tres
   variables se leen del medidor DLMS cada 30s pero están en el Grupo 1 del perfil
   original con pmin=15s. Aunque el benchmark fuerza pmin=10, el firmware solo notifica
   cuando el **valor cambia**, y estos valores cambian cada 30s (ciclo DLMS).

3. **Llave inactiva (0 muestras)**: `reactivePower` — el medidor Microstar C2000
   retorna 0.0 constante para potencia reactiva, y el LwM2M engine de Zephyr no
   notifica cambios si el valor no cambia.

---

## 3. Catálogo de Figuras

### 3.1. Benchmark Comparativo (4 escenarios)
Directorio: `figuras/comparativo/`

| Archivo | Descripción | Uso en tesis |
|---------|-------------|-------------|
| `fig_throughput.png` | Barras: msgs y msgs/s por escenario | Comparación de rendimiento |
| `fig_iat_boxplot.png` | Boxplot de IAT por escenario | Distribución temporal |
| `fig_iat_per_key.png` | Heatmap IAT por recurso × escenario | Detalle por variable |
| `fig_completeness.png` | Barras: completeness % por escenario | Fiabilidad de entrega |
| `fig_coap_overhead.png` | Barras apiladas: payload vs overhead | Eficiencia de protocolo |
| `fig_rssi_lqi.png` | Barras: RSSI y LQI (si disponible) | Calidad de radio |

### 3.2. Análisis Profundo 10s
Directorio: `figuras/deep_10s/`

| Archivo | Descripción | Uso en tesis |
|---------|-------------|-------------|
| `01_message_rate_timeline.png` | Timeline de tasa de mensajes (ventana 30s) | Comportamiento temporal |
| `02_iat_distribution.png` | Histograma + CDF del inter-arrival time | Distribución de latencia |
| `03_iat_per_key_boxplot.png` | Boxplot de IAT por recurso | Variabilidad por variable |
| `04_cumulative_data_volume.png` | Volumen acumulado CoAP + radio | Tráfico total generado |
| `05_protocol_overhead.png` | Desglose: payload vs CoAP vs 802.15.4 | Eficiencia de protocolo |
| `06_network_utilization.png` | Gauge de utilización + estimación nodos | Capacidad de la red |
| `07_completeness_per_key.png` | Barras: completeness por recurso | Fiabilidad por variable |
| `08_data_rate_timeline.png` | Timeline de data rate (kbit/s) | Carga dinámica de red |
| `09_jitter_per_key.png` | Barras: jitter por recurso | Estabilidad temporal |
| `10_docker_resources.png` | CPU + memoria de Docker (tb-edge) | Carga del Edge |
| `11_summary_table.png` | Tabla visual de métricas agregadas | Resumen ejecutivo |
| `12_system_resources.png` | 4 paneles: CPU, RAM, temp, red (eth0+wpan0) | Recursos del Edge |

### 3.3. Comparación de Latencia (Read/Observe)
Directorio: `figuras/latencia/`

| Archivo | Descripción |
|---------|-------------|
| `cmp_01_success_per_round.png` | Éxito por ronda en test de lectura |
| `cmp_02_failures_by_object.png` | Fallos por objeto LwM2M |
| `cmp_03_latency_by_object.png` | Latencia por objeto LwM2M |
| `cmp_04_summary_table.png` | Tabla resumen de latencia |
| `cmp_05_cdf_comparison.png` | CDF comparativa de latencia |
| `cmp_06_resource_success_rate.png` | Tasa de éxito por recurso |
| Varios `graph_v*.png` | Gráficas adicionales de versiones anteriores |

---

## 4. Catálogo de Datos (CSV/JSON)

### 4.1. Benchmark Comparativo
Directorio: `datos/benchmark_4escenarios/`

| Archivo | Contenido |
|---------|-----------|
| `benchmark_summary.json` | JSON completo con configuración + aggregate + per_key para los 4 escenarios |
| `raw_ts_baseline.csv` | Timestamps crudos — escenario baseline |
| `raw_ts_1s.csv` | Timestamps crudos — escenario 1s |
| `raw_ts_5s.csv` | Timestamps crudos — escenario 5s |
| `raw_ts_10s.csv` | Timestamps crudos — escenario 10s |
| `per_key_baseline.csv` | Estadísticas por llave — baseline |
| `per_key_1s.csv` | Estadísticas por llave — 1s |
| `per_key_5s.csv` | Estadísticas por llave — 5s |
| `per_key_10s.csv` | Estadísticas por llave — 10s |
| `thesis_table.txt` | Tabla comparativa + tabla LaTeX lista para copiar |

### 4.2. Análisis Profundo 10s
Directorio: `datos/benchmark_10s_deep/`

| Archivo | Contenido |
|---------|-----------|
| `analysis_10s.json` | JSON completo: config + aggregate + per_key + docker + system |
| `raw_ts_10s.csv` | Timestamps crudos de 724 muestras |
| `per_key_10s.csv` | Estadísticas por llave (14 variables) |
| `docker_stats.csv` | Timeline de CPU% y MEM% del contenedor tb-edge |
| `system_stats.csv` | Timeline de métricas del RPi4: load, memoria, temp, red (eth0+wpan0) |
| `thesis_summary_10s.txt` | Resumen + tabla LaTeX |

---

## 5. Tablas LaTeX (listas para copiar)

### 5.1. Tabla Comparativa de 4 Escenarios

```latex
\begin{table}[htbp]
\centering
\caption{Rendimiento de transporte LwM2M bajo diferentes intervalos de observación}
\label{tab:lwm2m-benchmark}
\begin{tabular}{lrrrrr}
\toprule
\textbf{Escenario} & \textbf{Msgs} & \textbf{Compl.\%} & \textbf{Msgs/s} & \textbf{IAT avg (s)} & \textbf{CoAP (KB)} \\
\midrule
Baseline (Producción) & 32 & 61.5 & 0.107 & 38.79 & 1.9 \\
Agresivo (1s) & 264 & 5.5 & 0.880 & 3.63 & 16.0 \\
Medio (5s) & 1482 & 154.4 & 4.940 & 2.50 & 89.7 \\
Relajado (10s) & 121 & 25.2 & 0.403 & 11.45 & 7.3 \\
\bottomrule
\end{tabular}
\end{table}
```

### 5.2. Tabla Detallada Escenario 10s

```latex
\begin{table}[htbp]
\centering
\caption{Métricas de rendimiento — Escenario LwM2M Observe 10s}
\label{tab:lwm2m-10s-deep}
\begin{tabular}{lrr}
\toprule
\textbf{Métrica} & \textbf{Valor} & \textbf{Unidad} \\
\midrule
Total mensajes & 724 & msgs \\
Throughput & 1.2067 & msgs/s \\
IAT promedio & 11.83 & s \\
IAT p95 & 30.13 & s \\
Vol. CoAP & 43.84 & KB \\
Vol. radio & 89.79 & KB \\
Data rate radio & 1.226 & kbit/s \\
Eficiencia payload & 23.6 & \% \\
Utilización canal & 0.9808 & \% \\
Nodos máx. est. & 101 & nodos \\
\bottomrule
\end{tabular}
\end{table}
```

### 5.3. Tabla de Recursos del Edge

```latex
\begin{table}[htbp]
\centering
\caption{Recursos del sistema Edge (RPi4) durante escenario 10s}
\label{tab:edge-resources-10s}
\begin{tabular}{lrrr}
\toprule
\textbf{Métrica} & \textbf{Mín} & \textbf{Promedio} & \textbf{Máx} \\
\midrule
CPU load (1 min) & 0.01 & 0.15 & 0.42 \\
Memoria usada (\%) & 39.2 & 39.4 & 39.6 \\
Temperatura CPU (°C) & 52.6 & 53.5 & 55.0 \\
wpan0 RX rate (B/s) & --- & 90 & --- \\
wpan0 TX rate (B/s) & --- & 53 & --- \\
Docker tb-edge CPU (\%) & 1.2 & 6.5 & 12.8 \\
Docker tb-edge MEM (\%) & 32.0 & 34.5 & 36.0 \\
\bottomrule
\end{tabular}
\end{table}
```

### 5.4. Tabla IAT por Recurso (10s)

```latex
\begin{table}[htbp]
\centering
\caption{Inter-Arrival Time por recurso — Escenario 10s (600s)}
\label{tab:iat-per-key-10s}
\begin{tabular}{lrrrr}
\toprule
\textbf{Recurso} & \textbf{N} & \textbf{IAT avg (s)} & \textbf{IAT p95 (s)} & \textbf{Compl. \%} \\
\midrule
voltage & 22 & 30.16 & 35.80 & 36.7 \\
current & 22 & 30.16 & 30.38 & 36.7 \\
activePower & 22 & 30.15 & 37.27 & 36.7 \\
activeEnergy & 65 & 10.16 & 16.97 & 108.3 \\
apparentPower & 66 & 10.00 & 10.00 & 110.0 \\
powerFactor & 66 & 10.00 & 16.33 & 110.0 \\
totalActivePower & 64 & 10.34 & 17.17 & 106.7 \\
totalReactivePower & 65 & 10.13 & 15.87 & 108.3 \\
totalApparentPower & 66 & 9.90 & 16.38 & 110.0 \\
totalPowerFactor & 66 & 10.00 & 15.67 & 110.0 \\
reactiveEnergy & 67 & 10.00 & 10.00 & 111.7 \\
apparentEnergy & 66 & 10.00 & 10.00 & 110.0 \\
frequency & 67 & 10.00 & 10.00 & 111.7 \\
reactivePower & 0 & --- & --- & 0.0 \\
\bottomrule
\end{tabular}
\end{table}
```

---

## 6. Mapeo OBIS → LwM2M → Telemetría

Referencia completa de las variables medidas:

| OBIS Code | Magnitud | Unidad | LwM2M Path | Telemetry Key |
|-----------|----------|--------|------------|---------------|
| 1-1:32.7.0 | Tensión fase R | V | /10242/0/4 | `voltage` |
| 1-1:31.7.0 | Corriente fase R | A | /10242/0/5 | `current` |
| 1-1:21.7.0 | Pot. activa R | kW | /10242/0/6 | `activePower` |
| 1-1:23.7.0 | Pot. reactiva R | kvar | /10242/0/7 | `reactivePower` |
| 1-1:29.7.0 | Pot. aparente R | kVA | /10242/0/10 | `apparentPower` |
| 1-1:33.7.0 | Factor potencia R | — | /10242/0/11 | `powerFactor` |
| 1-1:1.7.0 | Pot. activa total | kW | /10242/0/34 | `totalActivePower` |
| 1-1:3.7.0 | Pot. reactiva total | kvar | /10242/0/35 | `totalReactivePower` |
| 1-1:9.7.0 | Pot. aparente total | kVA | /10242/0/38 | `totalApparentPower` |
| 1-1:13.7.0 | Factor pot. total | — | /10242/0/39 | `totalPowerFactor` |
| 1-1:1.8.0 | Energía activa | kWh | /10242/0/41 | `activeEnergy` |
| 1-1:1.8.0 | Energía reactiva | kvarh | /10242/0/42 | `reactiveEnergy` |
| 1-1:9.8.0 | Energía aparente | kVAh | /10242/0/45 | `apparentEnergy` |
| 1-1:14.7.0 | Frecuencia | Hz | /10242/0/49 | `frequency` |
| Obj 4/0/2 | Señal radio | dBm | /4/0/2 | `radioSignalStrength` |
| Obj 4/0/3 | Calidad enlace | % | /4/0/3 | `linkQuality` |

---

## 7. Metodología

### 7.1. Configuración de Escenarios

El perfil LwM2M del dispositivo se modifica programáticamente vía la API REST
de ThingsBoard Edge (`http://192.168.1.111:8090/api/deviceProfile/{id}`).
Se ajustan los atributos `pmin` y `pmax` de cada recurso observado. El Edge
propaga los Write-Attributes al nodo LwM2M vía CoAP.

**Nota importante**: TB Edge NO envía Write-Attributes al firmware. El pmin/pmax
configurado en el perfil solo controla cuándo el servidor **acepta** notificaciones,
pero no las solicita activamente. Para forzar que el nodo notifique a intervalos
exactos, se implementó `notify_interval` en el firmware v0.13.0 — un shell command
que configura un intervalo en ms para llamar `notify_all_observers()` desde el loop
principal.

### 7.2. Recopilación de Datos

1. Se configura el perfil con el intervalo deseado vía API REST
2. Período de **warmup** (90s default) para estabilización del protocolo
3. Se marca `start_ts` y `end_ts` como timestamps UTC
4. Durante la colección, cada 30s se recopilan:
   - Estadísticas Docker (`docker stats --no-stream`)
   - Métricas del sistema Edge vía SSH (`/proc/loadavg`, `free -b`, `/proc/net/dev`, CPU temp)
5. Al finalizar, se consulta la API de telemetría de TB Edge:
   `GET /api/plugins/telemetry/DEVICE/{id}/values/timeseries?keys=...&startTs={start}&endTs={end}`
6. Se calcula IAT (inter-arrival time) entre muestras consecutivas por llave

### 7.3. Métricas Calculadas

| Métrica | Fórmula |
|---------|---------|
| Throughput | total_msgs / duration_s |
| Completeness | (samples / expected) × 100 |
| Expected samples | duration / pmin |
| IAT | timestamp[i] - timestamp[i-1] para muestras consecutivas |
| Jitter | \|IAT - pmin\| promedio |
| CoAP bytes | msgs × 62 (header + CBOR payload estimado) |
| Radio bytes | msgs × 127 (trama 802.15.4 completa) |
| Data rate | radio_bytes × 8 / duration_s |
| Utilización canal | data_rate / 125000 × 100 |
| Nodos máx. est. | floor(70% utilización / utilización_1_nodo) |

### 7.4. Herramientas

| Script | Líneas | Función |
|--------|--------|---------|
| `tools/benchmark_lwm2m.py` | ~1180 | Orquestador 4 escenarios + gráficas comparativas |
| `tools/graph_benchmark.py` | ~419 | Generador de 6 gráficas comparativas |
| `tools/benchmark_10s_deep.py` | ~1563 | Análisis profundo 10s + métricas SSH + 12 gráficas |
| `tools/restore_and_test.py` | — | Restauración del perfil baseline |

---

## 8. Conclusiones de los Datos

### 8.1. CoAP Congestion Cliff (Hallazgo Principal)

El escenario de 5s produce **5.6× más throughput** que el de 1s, pese a tener
una frecuencia de notify 5× menor. Esto se explica por la saturación de la capa
de enlace IEEE 802.15.4:

- Con pmin=1s: 16 recursos × 1 notify/s = 16 CoAP msgs/s → cada mensaje necesita
  127B × ~2 (fragmentación IPv6) = ~254 bytes → ~32.5 kbps → 26% del canal
- Las retransmisiones CoAP y colisiones 802.15.4 causan timeouts, duplicados y
  pérdida masiva → completeness 5.5%
- Con pmin=5s: 3.2 msgs/s → ~6.5 kbps → 5.2% del canal → sin congestión

### 8.2. Capacidad de la Red Thread

Con un nodo a 10s, la utilización del canal es **0.98%**. Asumiendo un techo
conservador de 70% de utilización (dejando margen para ACKs, beacons, routing):

- **~101 nodos** podrían operar simultáneamente en la misma red Thread con
  intervalos de 10s
- Con intervalos de 5s, se podrían soportar ~50 nodos
- Con intervalos de 1s, la red se satura con **~3-4 nodos**

### 8.3. Impacto en el Edge

El RPi4 opera con holgura extrema:
- CPU load < 0.5 con un nodo (de 4 cores disponibles)
- Memoria 39% usada (incluyendo PostgreSQL + Docker)
- Temperatura estable a 53°C
- El cuello de botella NO es el Edge, sino la **red 802.15.4**

### 8.4. Anomalías Notables

1. **3 llaves a 30s**: `voltage`, `current`, `activePower` solo se actualizan cada
   30s (ciclo DLMS) aunque el observe sea a 10s. Solución: reducir DLMS poll interval
   o agrupar estos recursos con pmin >= 30s.

2. **`reactivePower` = 0 muestras**: El medidor retorna 0.0 constante → LwM2M no
   notifica valores sin cambio. No es un error; es carga puramente resistiva.

3. **Completeness > 100%**: Algunas llaves tienen más muestras de las esperadas
   (e.g., 111.7%). Esto ocurre porque `notify_all_observers()` en el firmware
   fuerza notificación cada ciclo, y hay leves diferencias de timing.

---

## 9. Estructura de Archivos de Este Export

```
thesis_export/
├── README.md                          ← Este archivo
├── figuras/
│   ├── comparativo/                   ← 6 gráficas del benchmark 4 escenarios
│   ├── deep_10s/                      ← 12 gráficas del análisis profundo 10s
│   └── latencia/                      ← 6 gráficas de tests de latencia
├── datos/
│   ├── benchmark_4escenarios/         ← JSONs + CSVs del benchmark comparativo
│   └── benchmark_10s_deep/            ← JSONs + CSVs del análisis profundo
└── tablas/
    ├── tabla_comparativa_4esc.tex     ← LaTeX tabla comparativa
    ├── tabla_10s_detalle.tex          ← LaTeX tabla 10s
    ├── tabla_edge_recursos.tex        ← LaTeX recursos del Edge
    └── tabla_iat_por_recurso.tex      ← LaTeX IAT por recurso
```

---

## 10. Notas para el Agente de la Tesis

### Contexto Importante

- Todos los datos son **reales**, medidos en un setup físico completo (no simulado)
- El medidor Microstar C2000 está leyendo una carga real conectada a la red eléctrica
- La red Thread es real con un solo nodo (ESP32-C6) + Border Router (RPi4)
- Las estimaciones de nodos máximos son teóricas basadas en utilización de canal medida
- El firmware fue desarrollado completamente (no es un SDK de ejemplo)
- Los tests unitarios (98/98) validan el parser DLMS/COSEM custom

### Limitaciones Conocidas

1. **Un solo nodo**: Los benchmarks son con 1 nodo. El comportamiento con múltiples
   nodos es estimado, no medido.
2. **reactivePower no reporta**: Carga puramente resistiva → potencia reactiva = 0
   constante → no hay Notify.
3. **Serial write broken**: El puerto COM12 no acepta escrituras después del flash.
   Esto NO afecta los datos — toda la telemetría se recopila vía API REST de TB Edge.
4. **No hay RSSI/LQI en benchmarks**: Los recursos `radioSignalStrength` y
   `linkQuality` no retornaron datos en las ventanas de medición.
5. **DLMS poll de 30s es un cuello de botella hardware**: No se puede reducir por
   limitaciones del RS485/DLMS del medidor. Afecta a voltage, current, activePower.

### Cómo Reproducir

```powershell
# 1. Activar venv
cd C:\Users\jsgir\Documents\ESP32\zephyrproject\ami-lwm2m-node
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
& C:\Users\jsgir\Documents\ESP32\.venv\Scripts\Activate.ps1

# 2. Benchmark comparativo (4 escenarios, ~35 min total)
python tools\benchmark_lwm2m.py --duration 300 --warmup 90 --serial-port COM12

# 3. Análisis profundo 10s (~12 min)
python tools\benchmark_10s_deep.py --duration 600 --warmup 90 --format png

# 4. Solo regenerar gráficas desde datos existentes
python tools\benchmark_10s_deep.py --analyze-only results\benchmark_10s\20260303_151212 --format png
```

### Commits Git Relevantes

| Commit | Descripción |
|--------|-------------|
| `619a57c` | Sesión 8: cleanup, .gitignore, ARCHITECTURE.md |
| `7647f0c` | v0.13.0: benchmark suite, configurable notify_interval, thesis graphs |
| `3c932af` | Deep 10s analysis: system metrics via SSH, 12 thesis graphs |
