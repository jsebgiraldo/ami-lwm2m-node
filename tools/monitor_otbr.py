#!/usr/bin/env python3
"""Monitor OTBR neighbor table for Thread device join attempts."""
import paramiko
import time
import sys

HOST = "192.168.1.111"
USER = "root"
PASS = "root"

def main():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        client.connect(HOST, username=USER, password=PASS, timeout=5)
    except Exception as e:
        print(f"SSH failed: {e}")
        return

    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    print(f"Monitoring OTBR neighbors for {duration}s...")
    print(f"If device boot-loops, we'll see brief neighbor appearances.\n")
    
    start = time.time()
    last_count = -1
    
    while time.time() - start < duration:
        try:
            _, stdout, _ = client.exec_command("ot-ctl neighbor table")
            output = stdout.read().decode().strip()
            lines = [l for l in output.split("\n") if l.strip() and not l.startswith("|") or "RLOC16" not in l]
            # Count data rows (not header/separator/Done)
            data_lines = [l for l in output.split("\n") 
                         if l.strip() and "|" in l and "Role" not in l and "---" not in l and "Done" not in l]
            count = len(data_lines)
            
            elapsed = time.time() - start
            if count != last_count:
                print(f"[{elapsed:6.1f}s] Neighbors: {count}")
                if count > 0:
                    for dl in data_lines:
                        print(f"         {dl.strip()}")
                last_count = count
            else:
                sys.stdout.write(".")
                sys.stdout.flush()
        except Exception as e:
            print(f"\n[{time.time()-start:6.1f}s] SSH error: {e}")
            break
            
        time.sleep(2)
    
    print(f"\n\nMonitoring complete. Device {'appeared' if last_count > 0 else 'never appeared'}.")
    client.close()

if __name__ == "__main__":
    main()
