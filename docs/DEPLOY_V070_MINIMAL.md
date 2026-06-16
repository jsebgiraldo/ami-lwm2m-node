# Deploy v0.7.0 — runbook 30 nodos + PSU soak

## ⚗️ EXPERIMENTO ACTIVO (2026-06-10): SED vs FTD en SuperMini @ block 256

Decisión del usuario: en vez del deploy conservador (block 64), correr los 30
SuperMini como experimento A/B a escala:

- **Grupo A (16 boards)**: SED minimal + block 256 (`build_sed256`)
- **Grupo B (16 boards)**: FTD minimal + block 256 (`build_ftd256`)
- Versión: `0.7.0-exp256` en ambos. Asignación determinística por índice par/impar
  del MAC ordenado en fleet_map.csv (ver `--alternate` en bulk_flash_minimal.py).

Preguntas que responde:
1. ¿El SuperMini aguanta rol FTD con el firmware minimal? (celda nunca probada)
2. ¿El residual ~0.7/h de block 256 es tolerable a escala con auto-recovery?
3. ¿Rate Tipo-2 (LDO) difiere entre SED (radio dormida) y FTD (RX siempre on)?

Métrica: REREGs/board/día por grupo en `psu_fleet_watch.py` (logs/psu_soak.csv).

Comando de flasheo por batch:
```bash
python tools/bulk_flash_minimal.py --alternate --sed-build build_sed256 --ftd-build build_ftd256 --dry-run
python tools/bulk_flash_minimal.py --alternate --sed-build build_sed256 --ftd-build build_ftd256
```

---

## Plan conservador original (block 64) — fallback si el experimento falla

## Qué es

Firmware production-candidate derivado del análisis AD2 del 2026-06-09/10:
- `CONFIG_AMI_MINIMAL_AMI=y` — solo Power Meter (V/I/P) + OTA + Device
- `CONFIG_LWM2M_COAP_BLOCK_SIZE=64` — bursts TX ~1.2 ms (cero cliffs comprobado)
- SED 60 s poll + TX 0 dBm + LED quiet + child timeout 30 min (SuperMini)
- FTD para routers (XIAO / WROOM)

## Builds

| Build | Rol | Boards |
|-------|-----|--------|
| `zephyrproject/build_minimal` | SED | 30× SuperMini (OUI `10:51:DB`) |
| `zephyrproject/build_minimal_ftd` | FTD router | XIAO + WROOM (resto de OUIs) |

## Paso 1 — Flash (batches por USB)

La PSU no tiene data lines: flashear en batches conectados al PC/hub.

```bash
# Conecta un batch (5-10 boards) al hub del PC, luego:
python tools/bulk_flash_minimal.py --dry-run   # revisa el plan (rol por OUI)
python tools/bulk_flash_minimal.py             # flashea el batch
# Repite hasta cubrir los 30+routers. Resultados: tools/bulk_flash_results.csv
```

- Fallos `USB_FAIL`/`TARGET_FAIL`: replug del board y reintenta (el script ya hace 2 retries).
- Para forzar rol: `--ftd MAC` / `--sed MAC`.

## Paso 2 — Provision (solo boards nuevos)

Los 30 SuperMini ya existen en TB Edge. Routers nuevos:

```bash
python tools/provision_node.py --mac <MAC> --host 192.168.8.111
```

## Paso 3 — Mover a PSU + soak

1. Desconecta los boards del PC, conéctalos a las 2 PSUs.
2. Lanza el watch (token-refresh y staleness ya manejados):

```bash
python tools/psu_fleet_watch.py --duration 86400 --interval 60
```

Salud por board = `active` + `lastActivityTime < 600 s`. Cada power-cycle
aparece como `REREG` (re-REGISTER en transportLog) — ese es el indicador de
ciclo, NO total_resets (Object 33000 está deshabilitado en minimal).

CSV: `logs/psu_soak.csv`.

## Criterio de éxito (24 h)

- alive ≥ 28/30 sostenido
- REREG por board ≤ 2/día (los Tipo-2 LDO residuales del SuperMini)
- V/I/P fluyendo en TB para todos

## Si un SuperMini cicla mucho (Tipo 2)

Es el colapso transitorio del LDO (ver BRIEFING_2026-06-10.md). Mitigación
HW pendiente de validar: cap 100–470 µF low-ESR en el **rail 3V3** (salida
del LDO), no en VBUS.

## Rollback

Los boards conservan MCUboot: cualquier versión anterior se puede empujar
por OTA con `tools/ota_push_direct.py` (verificado funcionando con block 64,
~30 s por imagen).
