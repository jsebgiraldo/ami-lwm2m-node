# Especificación del stack por capa OSI — AMI LwM2M Node `v0.7.5-prod`

> Valores REALES del build desplegado `build_prod` (FTD + Object 33000 + block 64
> + mesh R1000 + Fix A). Fuente: `build_prod/.config`, `prj.conf`, `src/main.c`
> (runtime), y el OTBR vivo (192.168.8.111). Fecha: 2026-06-16.
>
> Fleet: 30× ESP32-C6 SuperMini, mesh Thread, OTBR Pi4 + TB Edge @192.168.8.111.

---

## 0. Resumen ejecutivo — cadencia y volumen de datos

**¿Cada cuánto envía cada board?**

| Flujo | Cadencia | Disparador / knob |
|---|---|---|
| Telemetría AMI (V, I, P, E, Hz, etc.) | **cada 30–60 s** | pmin=30s (piso) → pmax=60s (garantía) |
| Diagnóstico Object 33000 (uptime, resets, RSSI…) | **cada 60 s** | conn_monitor loop `CONN_UPDATE_INTERVAL_S=60` + jitter |
| LwM2M REG_UPDATE (registro vivo) | **cada 300 s** | `LWM2M_UPDATE_PERIOD=300` (Fix A) |
| CoAP keepalive (notify uptime_s) | **cada 300 s** | `AMI_COAP_KEEPALIVE_PERIOD_S=300` |
| REGISTER completo | al boot / re-attach | — |

**¿Cuánto dato?** CoAP block = **64 B** → cada mensaje LwM2M se fragmenta en bloques de ≤64 B de payload (≈1 frame 802.15.4 por bloque). Estimaciones:

| Mensaje | Payload aprox | Bloques de 64 B |
|---|---|---|
| Notify de 1 recurso (ej. voltage, OMA-TLV) | ~25–45 B | 1 |
| Notify Object 33000 1 RID | ~15–30 B | 1 |
| REG_UPDATE (link-format) | ~80–200 B | 2–4 |
| REGISTER (lista completa de objetos) | ~300–500 B | 5–8 |
| Read full Object 33000 (38 RIDs) | ~220 B | ~4 |

**Carga agregada por board (steady state):** ~1 notify cada 30–60 s (~40 B) + 1 REG_UPDATE + 1 keepalive cada 300 s. ≈ **20–60 B/s por board** → para 30 boards ≈ **0.6–1.8 KB/s** sobre la malla (muy por debajo del techo de ~250 kbps de 802.15.4, pero compartido + routing).

---

## L1 — Física (radio)

| Spec | Valor | Configurable en |
|---|---|---|
| Radio | IEEE 802.15.4 @ 2.4 GHz (ESP32-C6 nativo) | HW |
| Modulación / tasa | O-QPSK, **250 kbps** | fija (802.15.4) |
| **TX power** | **+4 dBm** | `CONFIG_AMI_TX_POWER_DBM` (prj.conf) → `otPlatRadioSetTransmitPower` (main.c) |
| Canal | **25** (real, dataset R1000) | TLV blob en main.c (`CONFIG_AMI_MESH_R1000`); el `CONFIG_OPENTHREAD_CHANNEL` del .config es cosmético |
| CCA | Energy-Detection (`IEEE802154_ESP32_CCA_ED`) | .config |
| RX buffers radio | 20 | `CONFIG_IEEE802154_ESP32_RX_BUFFER_SIZE` |

**Palanca clave de cobertura/estabilidad:** TX power. 4 dBm ≈ ~30 m indoor. Subir (rango -16…+20) mejora alcance pero sube corriente de TX (riesgo de sag de PSU en picos coordinados). Bajar < 0 dBm causó colapsos de fleet (histórico).

---

## L2 — Enlace / MAC (802.15.4 + Thread MAC)

| Spec | Valor | Configurable en |
|---|---|---|
| Frame 802.15.4 (PHY) | **127 B máx** (payload útil ~80–100 B tras headers+MIC) | fijo (estándar) |
| Seguridad L2 | **AES-128-CCM** (network key de Thread) | dataset Thread |
| FCS incluido en L2 pkt | sí | `IEEE802154_L2_PKT_INCL_FCS` |
| Acceso al medio | CSMA/CA + reintentos MAC | 802.15.4 |
| Child timeout (MLE) | **240 s** | `OPENTHREAD_MLE_CHILD_TIMEOUT` |
| Child supervision interval | **129 s** | `OPENTHREAD_CHILD_SUPERVISION_INTERVAL` |
| Child supervision check timeout | **190 s** | `OPENTHREAD_CHILD_SUPERVISION_CHECK_TIMEOUT` |
| CSL (sleepy) | **deshabilitado** (FTD, no sleepy) | `OPENTHREAD_CSL_*` |

**Relevante a los drops:** un child que no oye a su padre por `CHILD_TIMEOUT=240s` se desatacha. `SUPERVISION` (keep-alive MAC) cada ~129s lo previene. Si un board RF-marginal pierde supervisión repetidamente → drop/re-attach (el "goteo" que ves).

---

## L2.5/L3 — Red Thread (mesh, 6LoWPAN, IPv6)

| Spec | Valor | Configurable en |
|---|---|---|
| Rol device | **FTD** (router-elegible) | `CONFIG_OPENTHREAD_FTD` (overlays/ftd.conf) |
| Red / PANID / canal | **OpenThread-efeb / 0xefeb / 25** | dataset R1000 (TLV main.c) |
| **Router upgrade threshold** | **10** | `otThreadSetRouterUpgradeThreshold(10)` (main.c) |
| **Router downgrade threshold** | **12** | `otThreadSetRouterDowngradeThreshold(12)` (main.c) |
| Max children por router | 32 | `OPENTHREAD_MAX_CHILDREN` |
| Compresión | 6LoWPAN (OpenThread interno; `NET_6LO` off) | — |
| IPv6 MTU | **1280 B** (fragmentado a frames de 127 B) | `NET_IPV6_MTU` |
| Discovery de TB Edge | **SRP / DNS-SD** (`thingsboard-edge…arpa` advertised por OTBR) | OTBR srp server |
| Routing | Thread (RLOC16, next-hop, path cost) | OpenThread |

**Auto-organización de roles:** todos FTD → entran como child → la malla elige ~**10–11 routers** (umbral 10/12 con histéresis); el resto quedan children/REED. **No se asigna a mano.** Override: Object 33001 (`become_router`/`become_child`).

---

## L3.5 — Buffers de red (presión de memoria del path)

| Spec | Valor | Configurable en |
|---|---|---|
| NET_PKT RX / TX | 64 / 32 | `NET_PKT_RX_COUNT` / `_TX_COUNT` |
| NET_BUF RX / TX | 128 / 64 | `NET_BUF_RX_COUNT` / `_TX_COUNT` |
| NET_BUF data size | 128 B | `NET_BUF_DATA_SIZE` |

Estos limitan cuántos paquetes en vuelo soporta el board. Bajo carga de mass-attach o polling rápido, agotarlos = drops. (Para polling rápido/IA, vigilar aquí.)

---

## L4 — Transporte (UDP / seguridad)

| Spec | Valor | Configurable en |
|---|---|---|
| Transporte | **UDP** (CoAP/UDP) | — |
| Seguridad LwM2M↔Edge | **NoSec** (CoAP plano sobre malla AES-cifrada L2); `LWM2M_DTLS_SUPPORT` no activo | — |
| Cripto (Thread commissioning) | mbedTLS ECDHE-ECDSA, AES; `MBEDTLS_SSL_MAX_CONTENT_LEN=768` | .config |

La confidencialidad la da Thread en L2 (AES-128 con network key). El CoAP a Edge va sin DTLS (más liviano; aceptable porque la malla ya cifra).

---

## L5–L7 — Aplicación (CoAP + LwM2M + modelo de datos)

### CoAP
| Spec | Valor | Configurable en |
|---|---|---|
| **Block size (TX/RX)** | **64 B** | `LWM2M_COAP_BLOCK_SIZE` (overlays/resprobe_lwm2m.conf) ← *palanca eléctrica clave* |
| Max message size | 1232 B | `LWM2M_COAP_MAX_MSG_SIZE` |
| ACK timeout inicial | **5000 ms** | `COAP_INIT_ACK_TIMEOUT_MS` |
| Max retransmisiones | **6** | `COAP_MAX_RETRANSMIT` |
| Extended options len | 40 | `COAP_EXTENDED_OPTIONS_LEN_VALUE` |
| Tipo | CON (confirmable) para notify/keepalive | — |

**Block 64 = el lever anti-cliff:** mensajes grandes (REGISTER, Object 33000) se parten en ráfagas de ≤64 B (~1.2 ms TX c/u) en vez de una ráfaga de ~10 ms (block 512) que disparaba el corte por sobrecorriente USB. En PSU con headroom, se podría subir (256/512) para más throughput — **medir con AD2 antes**.

### LwM2M (engine)
| Spec | Valor | Configurable en |
|---|---|---|
| **Lifetime** | **86400 s (24 h)** | `LWM2M_ENGINE_DEFAULT_LIFETIME` (prj.conf) |
| **Update period (REG_UPDATE)** | **300 s** | `LWM2M_UPDATE_PERIOD` ← **Fix A** |
| Update-early | 17280 s | `LWM2M_SECONDS_TO_UPDATE_EARLY` |
| Max messages / pending / observers | 48 / 32 / 36 | `LWM2M_ENGINE_MAX_MESSAGES` / `_MAX_PENDING` / `_MAX_OBSERVER` |
| Formato de datos | OMA-TLV (`RW_OMA_TLV_SUPPORT`) | .config |
| Queue mode (sleepy) | off (FTD) | `LWM2M_QUEUE_MODE_ENABLED` |
| **pmin (Server /1/0/2)** | **30 s** | = `AMI_LWM2M_NOTIFY_MIN_INTERVAL_MS/1000` |
| **pmax (Server /1/0/3)** | **60 s** | hardcoded main.c v0.7.1 (garantía de cadencia en firmware) |
| Notify min-interval (throttle por recurso) | **30000 ms** | `AMI_LWM2M_NOTIFY_MIN_INTERVAL_MS` |

### Modelo de datos (objetos LwM2M)
| Objeto | Uso |
|---|---|
| /1 Server | lifetime, pmin/pmax |
| /3 Device | fw version (/3/0/3), reboot (/3/0/4) |
| Power Meter (custom) | **AMI: V, I, P, energía, frecuencia, FP, 3-fase** (facturación + calidad) |
| **33000 Thread Diag** | uptime, total_resets, reset_reason, recover/watchdog counts, keepalive, heap, **RID 37 = last_reboot_code**, post-mortem (38 RIDs) |
| 33001 Thread Role Ctrl | become_router / become_child |

---

## Capa "operacional" — watchdogs y arranque (estabilidad)

| Spec | Valor | Configurable en |
|---|---|---|
| HW watchdog timeout | 300 s | `AMI_HW_WATCHDOG_TIMEOUT_S` |
| Real-liveness timeout | 600 s | `AMI_REAL_LIVENESS_TIMEOUT_S` (alimentado por REG_UPDATE 300s → Fix A) |
| Boot grace (hard) | 600 s | `AMI_HW_WATCHDOG_BOOT_GRACE_HARD_S` |
| Recover backoff min/max | 60 / 300 s | `AMI_LWM2M_RECOVER_BACKOFF_*` |
| Recover max attempts | 10 | `AMI_LWM2M_RECOVER_MAX_ATTEMPTS` |
| **Boot stagger** | **0–30000 ms aleatorio** | `AMI_BOOT_STAGGER_MAX_MS` ← *anti mass-power-on storm* |
| Reboot USB drain | 5000 ms | `AMI_REBOOT_USB_DRAIN_MS` |

---

## Palancas para tus dos objetivos

### A) Más estabilidad (cerrar la cola / goteo)
1. **Encender en lotes** (operacional) — evita el storm de 30-attach simultáneo.
2. **Boot stagger** ↑ (30s→60s) — más separación en arranque coordinado.
3. **TX power** ↑ (4→8 dBm) si hay boards RF-marginales por distancia — *medir corriente en PSU*.
4. **Router threshold** — 10/12 ya es el sweet spot probado.

### B) Polling AMI más rápido (para IA)
1. **pmin / NOTIFY_MIN_INTERVAL_MS** ↓ (30s → 5–10s) — sube la tasa de telemetría.
2. **pmax** ↓ (60s → ~10s) — garantía más agresiva.
3. **CoAP block** ↑ (64→256) — menos fragmentos por notify = más eficiente (*medir cliff en PSU con AD2*).
4. **Techos a respetar:** mesh 250 kbps compartida, NET_BUF/PKT counts, e ingestión de TB Edge. Subir gradual + medir delivery-ratio.

---

*Referencias de medición: `tools/verify_fleet.py` (estado fleet por RPC), `tools/fleet_track.py` (cadencia/roles), `tools/ad2_brownout_capture.py` (corriente/cliff eléctrico). Build: `tools/build_prod.py`.*
