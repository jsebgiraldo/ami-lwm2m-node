"""
AMI Monitor v4 - Waits for DISAPPEAR then REAPPEAR cycle.
This ensures we get a truly fresh USB-CDC port.
"""
import serial
import serial.tools.list_ports
import time

LOG = r"C:\tmp\ami_log.txt"
STATUS = r"C:\tmp\ami_status.txt"
BAUD = 115200

def find_esp32c6():
    for p in serial.tools.list_ports.comports():
        if p.vid == 0x303A and p.pid == 0x1001:
            return p.device
    return None

def main():
    log_f = open(LOG, "w", encoding="utf-8")
    
    def log(text):
        ts = time.strftime('%H:%M:%S')
        line = f"[{ts}] {text}"
        print(line, flush=True)
        log_f.write(line + "\n")
        log_f.flush()
    
    def write_status(msg):
        with open(STATUS, "w") as f:
            f.write(msg + "\n")
    
    log("=== AMI Monitor v4 ===")
    
    # Step 1: Check if device is currently present
    current = find_esp32c6()
    if current:
        log(f"Device currently on {current} - UNPLUG IT NOW!")
        log("Waiting for device to DISAPPEAR...")
        write_status("WAITING_UNPLUG")
        
        for i in range(120):
            if find_esp32c6() is None:
                log("Device GONE! Good.")
                break
            time.sleep(0.5)
            if i % 20 == 0 and i > 0:
                log(f"  Still waiting for unplug ({i//2}s)...")
        else:
            log("TIMEOUT waiting for unplug")
            write_status("TIMEOUT_UNPLUG")
            return
    else:
        log("Device not present.")
    
    # Step 2: Wait for device to APPEAR
    log("PLUG IT IN NOW! Waiting for device...")
    write_status("WAITING_PLUG")
    
    port = None
    for i in range(120):
        port = find_esp32c6()
        if port:
            log(f"Device APPEARED on {port}!")
            break
        time.sleep(0.5)
        if i % 20 == 0 and i > 0:
            log(f"  Still waiting for plug ({i//2}s)...")
    
    if not port:
        log("TIMEOUT waiting for plug")
        write_status("TIMEOUT_PLUG")
        return
    
    # Step 3: Wait for USB driver to settle
    log("Waiting 3s for driver to settle...")
    time.sleep(3)
    
    # Open with pyserial - proper SetCommState
    # DTR MUST be asserted for USB-CDC data to flow!
    # Do NOT pass dsrdtr=False - USB CDC needs DTR=high
    # Do NOT manually toggle DTR/RTS after open
    try:
        ser = serial.Serial(
            port=port,
            baudrate=BAUD,
            timeout=1,
            write_timeout=5
        )
        log(f"Port {port} OPENED!")
        write_status(f"CONNECTED:{port}")
    except Exception as e:
        log(f"OPEN FAILED: {e}")
        write_status(f"ERROR:{e}")
        return
    
    def read_all():
        chunks = []
        while ser.in_waiting > 0:
            chunks.append(ser.read(ser.in_waiting).decode("utf-8", errors="replace"))
            time.sleep(0.05)
        return "".join(chunks)
    
    def send_cmd(cmd, wait=2):
        log(f">>> {cmd}")
        try:
            ser.write(f"\r\n{cmd}\r\n".encode())
        except serial.SerialTimeoutException:
            log(f"  (write timeout)")
            return ""
        except Exception as e:
            log(f"  (write error: {e})")
            return ""
        time.sleep(wait)
        resp = read_all()
        if resp:
            for line in resp.splitlines():
                s = line.strip()
                if s:
                    log(f"    {s}")
        return resp
    
    # Phase 1: Read boot messages (device just booted from power cycle)
    log("--- Boot output (10s capture) ---")
    end = time.time() + 10
    while time.time() < end:
        data = ser.read(max(ser.in_waiting, 1))
        if data:
            text = data.decode("utf-8", errors="replace")
            for line in text.splitlines():
                if line.strip():
                    log(f"BOOT: {line.strip()}")
        time.sleep(0.05)
    
    # Phase 2: Diagnostics
    log("--- Diagnostics ---")
    send_cmd("")
    r = send_cmd("kernel version")
    if "Zephyr" not in r:
        log("WARNING: No response from shell. USB-CDC may not be working.")
        log("Trying again...")
        time.sleep(2)
        send_cmd("")
        send_cmd("kernel version")
    
    send_cmd("ot state")
    send_cmd("ot rloc16")
    send_cmd("ot ipaddr")
    send_cmd("ot channel")
    send_cmd("ot panid")
    send_cmd("ot scan", wait=10)
    
    # Phase 3: Continuous poll
    log("--- Continuous monitoring (every 10s) ---")
    poll = 0
    while True:
        try:
            time.sleep(10)
            poll += 1
            
            try:
                ser.write(b"\r\not state\r\n")
            except:
                log(f"[Poll #{poll}] write failed")
                continue
            
            time.sleep(1)
            resp = read_all()
            
            state = "unknown"
            for line in resp.splitlines():
                c = line.strip()
                if c in ("detached", "child", "router", "leader", "disabled"):
                    state = c
            
            log(f"[Poll #{poll}] OT state: {state}")
            write_status(f"state={state}:poll={poll}")
            
            if state in ("child", "router", "leader"):
                log(f"*** THREAD ATTACHED as {state}! ***")
                send_cmd("ot rloc16")
                send_cmd("ot ipaddr")
                send_cmd("ot neighbor table")
                send_cmd("ot router table")
                time.sleep(30)
            
            extra = read_all()
            if extra:
                for line in extra.splitlines():
                    if line.strip():
                        log(f"    {line.strip()}")
                        
        except KeyboardInterrupt:
            log("Interrupted - port stays open")
            break
        except Exception as e:
            log(f"Error: {e}")
            time.sleep(5)
    
    log("=== Done ===")
    log_f.close()

if __name__ == "__main__":
    main()
