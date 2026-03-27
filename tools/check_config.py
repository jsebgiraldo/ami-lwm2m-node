"""
Read key Kconfig settings from the build .config and write to test_result.txt.
Usage: python tools/check_config.py
"""
import sys, os

CONFIG = r"C:\Users\User\Documents\FW\ESP32C6-XIAO\DLMS-COSEM\build\zephyr\.config"
OUT    = r"C:\Users\User\Documents\UNAL\ami-lwm2m-node\test_result.txt"

KEYS = [
    "SHELL_LOG_BACKEND",
    "LOG_MODE_IMMEDIATE",
    "LOG_MODE_DEFERRED",
    "LOG_PRINTK",
    "UART_LINE_CTRL",
    "SHELL_BACKEND_SERIAL_FORCE_TX_BLOCKING",
    "SHELL_BACKEND_SERIAL_API_POLLING",
    "UART_INTERRUPT_DRIVEN",
    "SERIAL_ESP32_USB",
    "BOOT_DELAY",
    "NET_CONFIG_INIT_TIMEOUT",
    "IEEE802154_ESP32",
    "OPENTHREAD_MANUAL_START",
    "NET_L2_OPENTHREAD",
    "NETWORKING",
]

if not os.path.exists(CONFIG):
    print(f"CONFIG not found: {CONFIG}")
    sys.exit(1)

with open(CONFIG, 'r') as f:
    lines = f.readlines()

results = []
for key in KEYS:
    found = [l.strip() for l in lines if key in l]
    if found:
        for l in found:
            results.append(l)
    else:
        results.append(f"# {key}: NOT FOUND IN CONFIG")

with open(OUT, 'w') as f:
    f.write('\n'.join(results) + '\n')

print("Written to:", OUT)
for r in results:
    print(r)
