"""Get OTBR dataset cleanly and parse it"""
import json

try:
    import paramiko
except ImportError:
    print("Need paramiko")
    exit(1)

HOST = "192.168.1.111"
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username="root", password="root", timeout=10)

# Get active dataset as hex
_, stdout, _ = ssh.exec_command("ot-ctl dataset active -x", timeout=10)
raw = stdout.read().decode().strip()
# Remove "Done" line and whitespace
lines = [l.strip() for l in raw.split('\n') if l.strip() and l.strip() != 'Done']
dataset_hex = ''.join(lines)
print(f"Raw dataset hex ({len(dataset_hex)} chars, {len(dataset_hex)//2} bytes):")
print(f"  {dataset_hex}")

# Parse TLVs
data = bytes.fromhex(dataset_hex)
print(f"\nParsed TLVs:")
i = 0
tlv_names = {
    0x00: "Channel", 0x01: "PAN ID", 0x02: "Extended PAN ID",
    0x03: "Network Name", 0x04: "PSKc", 0x05: "Network Key",
    0x07: "Mesh-local Prefix", 0x0c: "Security Policy",
    0x0e: "Active Timestamp", 0x35: "Channel Mask",
}
while i < len(data):
    tlv_type = data[i]
    tlv_len = data[i+1]
    tlv_val = data[i+2:i+2+tlv_len]
    name = tlv_names.get(tlv_type, f"Unknown(0x{tlv_type:02x})")
    print(f"  Type 0x{tlv_type:02x} ({name}), len={tlv_len}, val={tlv_val.hex()}")
    
    if tlv_type == 0x00:  # Channel
        ch = (tlv_val[1] << 8) | tlv_val[2]
        print(f"    -> Channel {ch}")
    elif tlv_type == 0x01:  # PAN ID
        pan = (tlv_val[0] << 8) | tlv_val[1]
        print(f"    -> PAN ID 0x{pan:04X}")
    elif tlv_type == 0x03:  # Network Name
        print(f"    -> \"{tlv_val.decode('utf-8')}\"")
    elif tlv_type == 0x07:  # Mesh-local
        parts = [f"{tlv_val[j]:02x}{tlv_val[j+1]:02x}" for j in range(0, 8, 2)]
        print(f"    -> {':'.join(parts)}::/64")
    elif tlv_type == 0x05:  # Network Key
        print(f"    -> {':'.join(f'{b:02x}' for b in tlv_val)}")
    
    i += 2 + tlv_len

# Get OTBR mesh-local EID (the routable one, not RLOC)
_, stdout, _ = ssh.exec_command("ot-ctl ipaddr mleid", timeout=10)
mleid = stdout.read().decode().strip().split('\n')[0].strip()
print(f"\nOTBR Mesh-Local EID: {mleid}")

# Get OTBR RLOC
_, stdout, _ = ssh.exec_command("ot-ctl rloc16", timeout=10)
rloc = stdout.read().decode().strip().split('\n')[0].strip()
print(f"OTBR RLOC16: {rloc}")

# Check if Thread is running
_, stdout, _ = ssh.exec_command("ot-ctl state", timeout=10)
state = stdout.read().decode().strip().split('\n')[0].strip()
print(f"OTBR State: {state}")

# Get all addresses
_, stdout, _ = ssh.exec_command("ot-ctl ipaddr", timeout=10)
addrs = stdout.read().decode().strip()
print(f"\nOTBR IPv6 addresses:\n{addrs}")

# Generate C array
print(f"\n{'='*60}")
print("C byte array for main.c:")
print(f"{'='*60}")
print(f"\tstatic const uint8_t otbr_tlvs[] = {{")
for j in range(0, len(data), 10):
    chunk = data[j:j+10]
    hex_bytes = ', '.join(f'0x{b:02x}' for b in chunk)
    print(f"\t\t{hex_bytes},")
print(f"\t}};")

print(f"\nLwM2M Server should point to: coap://[{mleid}]:5683")

ssh.close()
