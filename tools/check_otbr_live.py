"""SSH to Edge server and check OTBR + Thread status"""
import subprocess
import sys

HOST = "192.168.8.176"
USER = "root"
# Commands to check Thread/OTBR status
commands = [
    "hostname",
    "uptime",
    "ip -6 addr show | grep -E 'fdc6|wpan|thread'",
    "ip addr show wpan0 2>/dev/null || ip addr show Thread0 2>/dev/null || echo 'No wpan0/Thread0 interface'",
    "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null || echo 'docker not available or no containers'",
    "ot-ctl state 2>/dev/null || docker exec -i otbr ot-ctl state 2>/dev/null || echo 'ot-ctl not available'",
    "ot-ctl ipaddr 2>/dev/null || docker exec -i otbr ot-ctl ipaddr 2>/dev/null || echo 'ot-ctl ipaddr not available'",
    "ot-ctl dataset active -x 2>/dev/null || docker exec -i otbr ot-ctl dataset active -x 2>/dev/null || echo 'dataset not available'",
    "ot-ctl neighborinfo 2>/dev/null || docker exec -i otbr ot-ctl neighbor table 2>/dev/null || echo 'neighbor info not available'",
    "ss -ulnp | grep 5683 || netstat -ulnp | grep 5683 || echo 'No listener on UDP 5683'",
    "cat /etc/config/thread 2>/dev/null || echo 'No /etc/config/thread'",
]

combined = " && echo '---SEPARATOR---' && ".join(commands)

# Use ssh with password via sshpass if available, else try key-based
# First try with subprocess and expect-like approach
print(f"Connecting to {USER}@{HOST}...")
print(f"Running commands to check OTBR status...\n")

# Try using Python's subprocess with ssh
# Since we can't easily pass password to ssh, let's try paramiko
try:
    import paramiko
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password="root", timeout=10)
    
    for cmd in commands:
        print(f">>> {cmd}")
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=10)
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        if out:
            print(f"  {out}")
        if err:
            print(f"  (stderr) {err}")
        print()
    
    ssh.close()
    print("SSH session closed.")
except ImportError:
    print("paramiko not installed. Trying with subprocess ssh...")
    # Fallback: try ssh without password (key-based)
    try:
        result = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
             f"{USER}@{HOST}", combined],
            capture_output=True, text=True, timeout=30
        )
        print(result.stdout)
        if result.stderr:
            print(f"STDERR: {result.stderr}")
    except Exception as e:
        print(f"SSH failed: {e}")
        print("\nAlternative: try installing paramiko with:")
        print("  pip install paramiko")
except Exception as e:
    print(f"SSH connection failed: {e}")
