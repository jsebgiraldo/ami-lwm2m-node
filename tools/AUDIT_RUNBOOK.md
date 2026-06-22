# Extreme Audit Runbook (2026-06-19) — stabilize the mesh without losing LwM2M timing

Goal: find where to attack the next optimizations. Firmware under test = **v0.7.9-dlv**
(inbound delivery-liveness gate, the fix for the "registered-but-zero-telemetry" stuck
state proven on Lab 17). Audit firmware = **build_audit** (= 0.7.9-dlv + THREAD_ANALYZER_AUTO
+ stack sentinel + HW stack guard + per-thread CPU stats).

Venv python: `C:\Users\jsgir\Documents\ESP32\.venv\Scripts\python.exe`
Mesh: channel **25**, PAN **0x23ED**, netkey **5edebead64405b3e17193646c2942285** (Pi4 OTBR).

---

## Track A — 16-node console: stacks / RAM / overflow / CPU

Flash each of the 16 with build_audit (USB single-shot, the anti-wedge recipe):
```
PY=C:\Users\jsgir\Documents\ESP32\.venv\Scripts\python.exe
B=C:\Users\jsgir\Documents\ESP32\zephyrproject\build_audit
& $PY -m esptool --chip esp32c6 --port COMx --baud 460800 --before default-reset --after no-reset `
  write-flash --erase-all --flash-freq 20m --flash-mode dout `
  0x0 "$B\mcuboot\zephyr\zephyr.bin" 0x20000 "$B\ami-lwm2m-node\zephyr\zephyr.signed.bin"
# then PHYSICAL power-cycle (RTS reset does NOT boot these).
```
Capture all consoles + parse stack high-water + flag overflow/fault:
```
python tools/audit_console_capture.py            # auto-detect, exclude COM68
```
HUNT: any thread >80% stack (Bug #5 was an ISR-stack overflow caught this way),
the ISR0=100% CPU signature, sentinel/HW-stack-guard trips, delivery-stall logs.

## Track OTBR — mesh-level correlation (highest ROI, no bench work)
```
python tools/otbr_correlate.py --host <pi-user>@<pi-ip> [--password X] [--sudo]
```
HUNT: are the stuck boards present as neighbors? eidcache size + retry/0 entries
(the inbound address-resolution collapse signature). SRP advertises TB Edge?
Confirms or refutes the OTBR-side hypothesis vs board-side stuck session.

## Track B — live JTAG/GDB on 1-2 boards (catch a stuck session in the act)
The ESP32-C6 has a BUILT-IN USB-Serial-JTAG: OpenOCD can attach over the same USB
cable as the console (CDC = MI_00, JTAG = MI_02), no ESP-Prog needed.
```
# terminal 1 — OpenOCD (Espressif build)
openocd -f board/esp32c6-builtin.cfg
# terminal 2 — GDB with build_audit symbols
C:\Users\jsgir\zephyr-sdk-0.17.0\riscv64-zephyr-elf\bin\riscv64-zephyr-elf-gdb.exe `
  C:\Users\jsgir\Documents\ESP32\zephyrproject\build_audit\ami-lwm2m-node\zephyr\zephyr.elf
(gdb) target remote :3333
(gdb) monitor halt
(gdb) info threads
(gdb) thread apply all bt        # <-- where is a stuck board hung? engine/socket/observe-list
```
HUNT: when a board is app-silent, halt + backtrace the lwm2m engine + coap_keepalive
threads → is notify_observer returning 0 (no observers) or is the socket wedged?

## Track C — 802.15.4 sniffer (spare C6)
STATUS: the Zephyr `coprocessor` RCP sample fails to compile on this Zephyr/board
(openthread.c). Options to revisit: (a) fix the sample build, (b) esp-idf
`ot_rcp` + `sniffer.py`, (c) nRF52840 + nRF Sniffer. Once an RCP is flashed:
```
python <zephyrproject>\..\pyspinel\sniffer.py -c 25 -u COMx -b 460800 --crc --rssi \
  -o capture.pcap          # then open in Wireshark, set 802.15.4 key = netkey above
```
HUNT: do notifies leave the stuck board? do CON-ACKs return? REGISTER bursts?
→ distinguishes ret==0 (no observers, the gate fixes) vs ret>0-dropped (OTBR-side).

---
Cross-cut: the delivery-gate fix targets the ret==0 mode. Tracks B + C + OTBR
together CONFIRM which mode the stuck boards are actually in → validate/refine the fix.
