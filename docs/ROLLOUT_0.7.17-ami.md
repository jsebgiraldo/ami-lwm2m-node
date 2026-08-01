# Rollout runbook — 0.7.17-ami

**Fecha:** 2026-07-16
**Target:** `0.7.17-ami` = `0.7.16-ami` (/diag + /ami IPv6 GET) **+ fix de driver USB** (P1 limpieza de interrupts USJ en init, P2 bound del busy-wait de `poll_out`).
**Edge:** pi4 (`192.168.1.111:8090`) — la flota activa. (Ojo: el default histórico de las tools era `r1000`/192.168.8.111; ya se puede pasar `--mesh pi4`.)

## Qué trae 0.7.17-ami
- `/diag` (estado del nodo) + `/ami` (OBIS activos en vivo) por CoAP IPv6 en `[::]:5685`, independiente de LwM2M.
- Fix de `serial_esp32_usb.c` (patch en `patches/serial_esp32_usb-ami-usbfix.patch`): reduce los spins de 50 ms/byte y limpia estado stale del USJ tras reboot. **Reduce** (no elimina) la dependencia de power-cycles — el "device not functioning" tras soft-reset y el brownout de Thread son de hardware.

## Artefactos
- OTA package en TB: version `0.7.17-ami` (subido, **sin asignar**).
- Binario: `build_prod/ami-lwm2m-node/zephyr/zephyr.signed.bin` (MCUboot-signed).
- `node_doctor.py FW_LATEST = 0.7.17-ami`.

## Estado de la flota (pi4, 2026-07-16)
- **59** dispositivos `ami-esp32c6-*` en TB, **~32 activos** (OTA-ables), ~27 off-mesh (necesitan flash físico).

## Rollout — 2 caminos

### A) OTA escalonado (para los nodos en la malla)
El deployer clasifica por `/diag` (fw real por IPv6, no el atributo TB poco fiable) y usa `lastActivity` para reachability; hace un nodo a la vez con `--settle` para no congestionar la malla.

```bash
# 1) Dry-run: clasifica current / to-update / unreachable (no toca nada)
python tools/deploy_fleet_staged.py --version 0.7.17-ami --all --dry-run --mesh pi4

# 2) Ejecutar el rollout escalonado (nodo a nodo, 90s de settle)
python tools/deploy_fleet_staged.py --version 0.7.17-ami --mesh pi4 \
    --bin C:/Users/User/Documents/ESP32/zephyrproject/build_prod/ami-lwm2m-node/zephyr/zephyr.signed.bin \
    --all --settle 90
```
Requiere el firmware robust-OTA (confirm-on-attach, >= 0.7.14) en los targets — la flota ya lo tiene. TB solo empuja a nodos cuyo fw reportado != 0.7.17-ami.

### B) Flash físico (para los off-mesh / inactivos)
Al rotarlos al PC:
```bash
python tools/node_doctor.py --dry-run          # valida
python tools/node_doctor.py                    # flash 0.7.17-ami + provision + verify (deja operando)
```
**Caveat USB:** boards flaky pueden requerir power-cycle tras el flash (device not functioning). Es hardware; el fix del driver lo mitiga pero no lo elimina.

## Verificación (console-free)
```bash
python tools/diag_get.py --addr <OMR>          # confirma fw=0.7.17-ami
python tools/diag_get.py --ami --addr <OMR>    # OBIS activos
```
La OMR se saca del `transportLog` de TB (sin tocar el serie). El deployer y node_doctor ya lo hacen internamente.

## Recomendación de secuencia
1. Dry-run del deployer → ver cuántos to-update / unreachable.
2. OTA escalonado a los activos en tandas (re-runnable; los ya-actualizados se saltan por `/diag`).
3. Los inactivos → flash físico al rotarlos por el PC.
4. Cerrar con un barrido `diag_get.py` para confirmar fw=0.7.17-ami en la flota.
