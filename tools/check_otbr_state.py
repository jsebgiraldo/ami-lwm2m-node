#!/usr/bin/env python3
"""Query OTBR state via ubus with proper session handling."""
import requests
import json

BASE = "http://192.168.1.111"

def ubus_raw(sid, obj, method, params=None):
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "call",
        "params": [sid, obj, method, params or {}]
    }
    r = requests.post(f"{BASE}/ubus", json=payload, timeout=10)
    return r.json()

def main():
    # Login
    r = ubus_raw("00000000000000000000000000000000", "session", "login",
                  {"username": "root", "password": "root"})
    sid = r["result"][1]["ubus_rpc_session"]
    print(f"Session: {sid}")

    # Check ACL grants  
    r = ubus_raw(sid, "session", "access", {
        "scope": "ubus", "object": "otbr", "function": "state"
    })
    print(f"ACL check for otbr.state: {json.dumps(r)}")

    # Key OTBR status calls with raw output
    for method in ["state", "channel", "networkname", "panid", "extpanid",
                    "networkkey", "rloc16", "neighbor", "leaderdata",
                    "partitionid", "mode", "interfacename"]:
        r = ubus_raw(sid, "otbr", method, {})
        print(f"otbr.{method}: {json.dumps(r)}")

    # Also try a scan
    print("\nScanning Thread networks...")
    r = ubus_raw(sid, "otbr", "scan", {})
    print(f"otbr.scan: {json.dumps(r)}")

if __name__ == "__main__":
    main()
