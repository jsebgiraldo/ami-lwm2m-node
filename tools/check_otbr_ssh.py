#!/usr/bin/env python3
"""Check OTBR Thread state via SSH."""
import sys
try:
    import paramiko
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "paramiko", "-q"])
    import paramiko

HOST = "192.168.1.111"
USER = "root"
PASS = "root"

def ssh_cmd(client, cmd):
    _, stdout, stderr = client.exec_command(cmd)
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    return out if out else err

def main():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        client.connect(HOST, username=USER, password=PASS, timeout=5)
    except Exception as e:
        print(f"SSH connection failed: {e}")
        return

    print("=== OTBR Thread State ===")
    cmds = [
        ("State", "ot-ctl state"),
        ("Channel", "ot-ctl channel"),
        ("PAN ID", "ot-ctl panid"),
        ("Network Name", "ot-ctl networkname"),
        ("Extended PAN ID", "ot-ctl extpanid"),
        ("Network Key", "ot-ctl networkkey"),
        ("RLOC16", "ot-ctl rloc16"),
        ("Neighbors", "ot-ctl neighbor table"),
        ("Child Table", "ot-ctl child table"),
        ("Router Table", "ot-ctl router table"),
        ("IP Addresses", "ot-ctl ipaddr"),
        ("Leader Data", "ot-ctl leaderdata"),
    ]
    
    for label, cmd in cmds:
        result = ssh_cmd(client, cmd)
        print(f"{label}: {result}")
    
    client.close()

if __name__ == "__main__":
    main()
