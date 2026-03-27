"""Monitor AMI node for Thread join and LwM2M registration."""
import serial
import time
import os

PORT = "COM11"
RESULT = r"C:\Users\User\Documents\UNAL\ami-lwm2m-node\monitor_result.txt"

lines = []
def log(msg):
    print(msg, flush=True)
    lines.append(msg)

log("=== Monitoring AMI node (60s) ===")
s = serial.Serial(PORT, 115200, timeout=0.5, write_timeout=1)
log(f"Port open. dtr={s.dtr} rts={s.rts}")

buf = b""
t = time.time()
duration = 60  # Monitor for 60 seconds
while time.time() - t < duration:
    c = s.read(1024)
    if c:
        buf += c
        text = c.decode("utf-8", errors="replace")
        elapsed = time.time() - t
        # Log meaningful lines
        for line in text.split("\n"):
            line = line.strip()
            if line and len(line) > 3:
                log(f"  [{elapsed:.0f}s] {line}")

s.close()

log(f"\nTOTAL: {len(buf)} bytes in {duration}s")
text = buf.decode("utf-8", errors="replace")

# Check for key milestones
checks = {
    "Thread attached": "Thread" in text and ("attached" in text.lower() or "child" in text.lower() or "router" in text.lower()),
    "LwM2M registered": "lwm2m" in text.lower() and "regist" in text.lower(),
    "Shell active": "uart:~$" in text,
    "Thread started": "Thread started" in text,
    "IPv6 address": "ipv6" in text.lower() or "fdf5" in text.lower(),
}
log("\n=== Status ===")
for check, result in checks.items():
    log(f"  {'OK' if result else '--'} {check}")

with open(RESULT, "w") as f:
    f.write("\n".join(lines))
log("Done.")
