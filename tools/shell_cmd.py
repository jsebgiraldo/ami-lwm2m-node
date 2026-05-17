"""Send a Zephyr shell command to a node via USB Serial/JTAG and read the
response. Useful when PuTTY/miniterm cannot send TX (USB Serial CDC ACM
direction issues sometimes need a simpler raw stream).

Usage:
    python tools/shell_cmd.py COM17 'ami brightness'
    python tools/shell_cmd.py COM17 'ami brightness 50'
    python tools/shell_cmd.py COM17 'ami status'
    python tools/shell_cmd.py COM17 'help'

Prints stdout from the firmware until the next shell prompt or 3s of
silence (whichever comes first).
"""
from __future__ import annotations

import sys
import time

import serial


PROMPT = b"uart:~$ "
QUIET_TIMEOUT_S = 3.0
HARD_TIMEOUT_S = 10.0


def send_cmd(port: str, cmd: str, baud: int = 115200) -> int:
    s = serial.Serial(port, baud, timeout=0.2)
    print(f"[shell] opened {port} @ {baud}", flush=True)

    # Drain any pending log lines from the firmware so they do not
    # mix with our command output.
    drained = 0
    deadline = time.time() + 0.5
    while time.time() < deadline:
        chunk = s.read(256)
        if not chunk:
            break
        drained += len(chunk)
    if drained:
        print(f"[shell] drained {drained} pending bytes", flush=True)

    # Send the command. Zephyr shell expects \r or \n; \r\n is also fine.
    line = (cmd + "\r\n").encode("utf-8")
    n = s.write(line)
    s.flush()
    print(f"[shell] sent {n} bytes: {cmd!r}", flush=True)

    # Read until prompt reappears or quiet timeout reached.
    buf = bytearray()
    last_byte_at = time.time()
    hard_deadline = time.time() + HARD_TIMEOUT_S
    while time.time() < hard_deadline:
        chunk = s.read(256)
        now = time.time()
        if chunk:
            buf.extend(chunk)
            last_byte_at = now
            # If we see a prompt, command is done.
            if PROMPT in buf:
                break
        else:
            # Quiet for QUIET_TIMEOUT_S → assume done even without prompt.
            if now - last_byte_at >= QUIET_TIMEOUT_S:
                break

    s.close()

    # Decode + strip ANSI / VT100 escapes for readability.
    text = buf.decode("utf-8", errors="replace")
    # Remove ANSI escapes
    import re
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    text = text.replace("\r", "")

    print("[shell] --- response ---")
    print(text.rstrip())
    print("[shell] --- end ---")
    return 0 if buf else 1


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: shell_cmd.py <COM_PORT> <command>")
        print("Example: shell_cmd.py COM17 'ami brightness 50'")
        return 2
    port = sys.argv[1]
    cmd = " ".join(sys.argv[2:])
    return send_cmd(port, cmd)


if __name__ == "__main__":
    sys.exit(main())
