"""Wait for USB power cycle then flash firmware"""
import time
import subprocess
import serial.tools.list_ports

PORT = "COM11"
FLASH_CMD = [
    r".venv\Scripts\python.exe", "-m", "esptool",
    "--port", PORT, "--baud", "460800", "--chip", "esp32c6",
    "--before", "default-reset", "--after", "no-reset",
    "write-flash", "--flash-mode", "dio", "--flash-freq", "80m",
    "--flash-size", "4MB", "0x0", r"build\zephyr\zephyr.bin"
]

def port_exists(name):
    return any(p.device == name for p in serial.tools.list_ports.comports())

print(f"=== Flash Tool ===")
print(f"DESCONECTA el cable USB del XIAO ESP32-C6 ahora...")
print(f"(Esperando que {PORT} desaparezca...)")

# Wait for port to disappear
for i in range(60):
    if not port_exists(PORT):
        print(f"  {PORT} desaparecio!")
        break
    time.sleep(0.5)
else:
    print(f"Timeout: {PORT} no desaparecio. Desconecta el USB!")
    exit(1)

print(f"\nAhora RECONECTA el cable USB...")
print(f"(Esperando que {PORT} reaparezca...)")

# Wait for port to reappear
for i in range(60):
    if port_exists(PORT):
        print(f"  {PORT} detectado! Esperando 3s para estabilizar...")
        time.sleep(3)
        break
    time.sleep(0.5)
else:
    print(f"Timeout: {PORT} no reaparecio.")
    exit(1)

print(f"\nFlasheando firmware...")
result = subprocess.run(FLASH_CMD, capture_output=False, text=True)
print(f"\nFlash exit code: {result.returncode}")
