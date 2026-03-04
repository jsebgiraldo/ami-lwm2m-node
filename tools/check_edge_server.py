#!/usr/bin/env python3
"""Check Edge server health via SSH."""
import paramiko, sys

HOST = "192.168.1.111"
USER = "root"
PASS = "root"

try:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASS, timeout=10)
    
    commands = [
        ("Docker containers", "docker ps --format '{{.Names}}\t{{.Status}}'"),
        ("Uptime", "uptime"),
        ("Thread BR status", "ot-ctl state 2>/dev/null || echo 'ot-ctl not available'"),
        ("Memory", "free -m | head -3"),
    ]
    
    for label, cmd in commands:
        print(f"\n=== {label} ===")
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=10)
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        if out:
            print(out)
        if err:
            print(f"  (stderr: {err})")
    
    ssh.close()
    print("\nEdge server OK.")
except Exception as e:
    print(f"SSH Error: {e}")
    sys.exit(1)
