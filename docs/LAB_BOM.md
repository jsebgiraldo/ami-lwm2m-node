# Materiales del laboratorio / sandbox AMI

Qué hay, qué falta y **qué pregunta abierta desbloquea cada cosa**. Un
instrumento que no cierra una brecha concreta no entra en esta lista.

Referencias: `docs/BENCH_FINDINGS_2026-08.md` (lo medido) y `docs/PENDIENTES.md`
(lo que sigue abierto en la flota).

---

## 1. Lo que YA tenemos — no volver a comprar

| Equipo | Estado | Qué resolvió |
|---|---|---|
| **Nordic PPK2** | ✅ operativo | 100 kHz y fuente programable. Midió el pico real (246.9 mA en el bus de 5 V) y **el voltaje de muerte (~3.15 V)**. Además **alimenta sin líneas de datos USB** → se acabaron los cuelgues de USB-JTAG, y **da power-cycle por software** (POR verificado) |
| **FNIRSI FNB-C2** | ✅ operativo | Medidor USB en línea, ~100 Hz. Sirve para **vigilancia continua** y medias; para picos usar el PPK2. Ojo: emite muestras basura ocasionales (1290/2097/8388 mA) — las herramientas las filtran |
| **Digilent Analog Discovery 2** | ✅ | Osciloscopio/analizador. `tools/ad2_brownout_capture.py` (ctypes + dwf.dll) |
| **SONOFF ZBDongle-E** | ✅ operativo | RCP del OTBR del banco. Ya venía con ot-rcp/SPINEL 2.5.3.0 |
| **Adaptador USB-TTL (CP2102)** | ✅ operativo | **La herramienta que cazó los dos bugs de firmware.** Consola + shell por UART0 (D6/D7) cuando el USB-JTAG está colgado |
| **Sniffer 802.15.4 propio** | ✅ | `tools/sniffer/` (app Zephyr para C6) + `tools/sniffer_capture.py` |
| **Placas XIAO ESP32-C6** | ✅ ~30 | Flota + banco |
| PC Windows + WSL2 | ✅ | OTBR nativo + ThingsBoard en docker |
| **Hub AGEEN 16 puertos** (B0F2NYJSX3) | ✅ pero **no automatizable** | USB 3.2 Gen 2, 16 puertos, alimentado, con **interruptor físico por puerto**. NO es una regleta inteligente: sin Tuya/Matter/Alexa, sin API. Los interruptores son mecánicos → sirven para aislar a mano, no para que un script haga power-cycle. Ver §4 |

---

## 2. Prioridad 0 — compras que desbloquean trabajo hoy bloqueado

### 2.1 Cables USB de datos, de calidad — 10-15 USD

**El item más subestimado de la lista.** Un cable marginal produce exactamente
el síntoma que costó **seis movimientos de placa** en una sola sesión:
`PermissionError 31 "device not functioning"` y `Write timeout` al flashear, con
Windows reportando `Status=OK`. Lo que finalmente lo resolvió fue **cambiar de
puerto y de cable**.

- Buscar: `"USB C cable data transfer short"` — cortos (30-50 cm), de marca
- Comprar **3-4** y **marcarlos** para distinguirlos de los de carga

### 2.2 Cable/adaptador "solo carga" (data blocker) — 5-10 USD

- Buscar: `"USB data blocker"` · `"USB charge only adapter"`
- Corta D+/D− → la placa recibe energía **sin que enumere el USB-JTAG**
- Para **operación**; con el blocker puesto no se puede flashear

> Con el PPK2 alimentando ya se logra lo mismo para el DUT. El blocker sirve
> para los **otros** nodos del banco cuando se pruebe multi-nodo (§3.2).

### 2.3 Hub USB con PPPS (`uhubctl`) — 40-70 USD

**Ya no es para el nodo** — el PPK2 le da power-cycle por software. Sigue siendo
necesario para **lo demás del banco**, y hay un caso concreto y repetido: el
**dongle del OTBR se desprendió 3 veces** en una sesión, tumbando la malla, y
cada recuperación necesitó una mano.

🚨 **La trampa más cara de esta lista:** muchísimos hubs anuncian
*"per-port power switching"* y **no lo implementan**.

**Modelo recomendado: `Plugable USB3-HUB7BC`** (7 puertos, está en la lista de
compatibilidad y se consigue hoy en Amazon).

⚠️ **NO comprar el `USB3-HUB7C`** — es casi el mismo nombre y el README de
uhubctl advierte que en ese *"solo funciona en 2 puertos de carga"*.

Alternativa: `D-Link DUB-H7` **revisión D o E (carcasa negra)**. Las revisiones
**B, C, F y G no funcionan** — mismo modelo, distinto silicio. Si el anuncio no
dice la revisión, no lo compres.

Por qué importa tanto: el propio README de uhubctl explica que un hub que
anuncia conmutación por puerto sin implementarla *"corta la conexión de datos
USB pero no puede apagar la alimentación"* — que es justo lo que necesitamos.
La regla es verificar **corte real de VBUS**, no lo que dice la caja.

---

## 3. Prioridad 1 — cierran preguntas abiertas concretas

### 3.1 Dongle nRF52840 + nRF Sniffer 802.15.4 — 25 USD

- Buscar: `"nRF52840 Dongle"` (PCA10059). Preferir **Digi-Key/Mouser** (genuino)
- **Desbloquea** `PENDIENTES.md` §1: *"medir cuánto ocupa la ráfaga de registro
  con 11 vs 22 observaciones"* — prueba directa en el aire
- Tenemos sniffer propio, pero el nRF + Wireshark es llave en mano

### 3.2 2-3 XIAO ESP32-C6 adicionales para el banco — 8-12 USD c/u

- Buscar: `"Seeed Studio XIAO ESP32C6"`
- **Desbloquea la prueba directa del cliff**: el inrush medido es 246.9 mA por
  nodo y el presupuesto de un host USB-2.0 es 500 mA → **2 nodos encendiendo a
  la vez ya están en 494 mA**. Con 3 nodos se debería ver caer el host. Hoy eso
  es aritmética, no experimento
- También habilita pruebas de malla real (elección de router, multi-hop)

### 3.3 Analizador lógico (clon Saleae) — 15 USD

- Buscar: `"24MHz 8CH logic analyzer"`
- Decodificar **HDLC/DLMS sobre RS485** — hoy el bus del medidor es una caja
  negra salvo por los logs del firmware

---

## 4. El AGEEN de 16 puertos — resuelto: no sustituye al hub PPPS

Se creyó por un tiempo que era una **regleta inteligente** y se llegó a escanear
la LAN con `tinytuya` buscándola (0 dispositivos, además con el Wi-Fi del PC
caído). La ficha del producto lo aclara: es un **hub USB 3.2 Gen 2 de 16
puertos, alimentado, con interruptor mecánico por puerto**. Sin app, sin Tuya,
sin Matter, **sin API**.

Consecuencias prácticas:

- ✅ **Sí sirve** para dar corriente a muchos nodos y para aislar uno a mano
- ❌ **No sirve** para que un script haga power-cycle → no reemplaza al
  `Plugable USB3-HUB7BC` de §2.3
- ⚠️ **Cuidado con el presupuesto de corriente**: 16 puertos no significan 16
  nodos. El inrush medido es **246.9 mA por nodo**; lo que manda es cuántos
  amperios entrega la fuente del hub, no cuántos conectores tiene

**Sigue haciendo falta** una forma programable de cortar energía para reproducir
el escenario que `BENCH_FINDINGS` §1 señala como disparador real del colapso:
**el arranque simultáneo de N nodos**. Hoy eso lo cubre el PPK2 para *un* nodo;
para varios hace falta el hub PPPS.

---

## 5. Prioridad 2 — cuando el trabajo lo pida

| Equipo | Buscar | ~USD | Para qué |
|---|---|---|---|
| Fuente SCPI | `"Korad KA3005P"` | 120 | Perfiles de caída controlados. **El PPK2 ya cubre 0.8-5 V**, así que solo hace falta para tensiones/corrientes mayores |
| INA226 ×8 | `"INA226 module"` | 5 c/u | Corriente por nodo en paralelo (multi-nodo). El PPK2 solo cubre una placa |
| Caja apantallada + atenuadores SMA | `"RF shielded box"` · `"SMA attenuator kit"` | 30-60 | RSSI controlado y repetible. Hoy "acercar el nodo" es cualitativo |
| Relé USB 8 canales | `"8 channel USB relay"` | 20 | Conmutar cargas AC si la regleta no resulta controlable |

---

## 6. Recomendación

**Comprar ahora (~60-95 USD):** cables de datos buenos + data blockers + el hub
`uhubctl` de la lista de compatibilidad. Los cables son ridículamente baratos
frente a las horas que costaron.

**Después (~50 USD):** 2-3 XIAO extra + el dongle nRF52840. Con eso se puede
**demostrar el cliff experimentalmente** en vez de calcularlo, y **medir la
ráfaga en el aire**.

**Dónde comprar:** PPK2 y nRF52840 → **Digi-Key/Mouser** (genuinos, mejor
precio). Cables, blockers, hub, analizador, XIAO → **Amazon** sin problema.

> El PPK2 ya eliminó la necesidad de varias compras que parecían obligatorias
> (fuente programable para el barrido, power-cycle por software del DUT). Antes
> de comprar, revisar si el PPK2 ya lo cubre.
