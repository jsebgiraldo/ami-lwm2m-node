# Lessons Learned — Sessions 1-7

Document capturing problems found and their solutions during the
development and integration of the AMI system.

---

## 1. Port 5683 shared between LwM2M and CoAP (Session 7)

**Symptom**: The ESP32 node could not register with TB Edge. Port 5683 occupied.

**Root cause**: ThingsBoard uses the same port 5683 for both the LwM2M transport
and the generic CoAP transport. At startup, the first one to bind the port wins,
and the other fails silently.

**Solution**: In Edge docker-compose.yml:
```yaml
LWM2M_BIND_PORT: "5683"        # LwM2M keeps 5683
COAP_BIND_PORT: "5690"         # CoAP moves to another port
COAP_ENABLED: "false"          # Better: disable CoAP if not used
```

**Lesson**: Always check for port conflicts within the same container, especially
with protocols that share a default port.

---

## 2. defaultObjectIDVer format — V vs VER (Session 7)

**Symptom**: TB Edge observes resources but never receives Notify. Observe requests
reach the node but with malformed paths.

**Root cause**: The Device Profile in TB uses `defaultObjectIDVer` to map LwM2M
object IDs. There are two internal formats:
- **"V"** (correct): `"3": "1.2"`, `"10242": "1.0"`
- **"VER"** (incorrect): `"3_1.2"`, `"10242_1.0"`

When Edge syncs with Cloud, Cloud regenerates the profile and may overwrite
the format to VER, breaking the mapping.

**Solution**: Always modify the profile **via Cloud REST API** (port 80),
never directly on Edge. Cloud is the source of truth and propagates to Edge.

```python
# Example: API call to Cloud
PUT http://192.168.1.159:80/api/deviceProfile/{profileId}
X-Authorization: Bearer {jwt_token}
```

**Lesson**: In Edge-Cloud architectures, always modify configurations at the
highest level (Cloud) to avoid reversions due to synchronization.

---

## 3. ObserveStrategy COMPOSITE vs SINGLE (Session 7)

**Symptom**: Edge sends Observe Request but with empty path list. The node
responds with unknown token.

**Root cause**: The profile used `observeStrategy: COMPOSITE_BY_OBJECT`, which
attempts Composite-Observe (RFC 9175). The Zephyr LwM2M client does not support
Composite Observe — it responds with an error and TB Edge drops the session.

**Solution**: Change `observeStrategy` to `SINGLE` on every profile attribute.
This makes TB Edge observe each resource individually, which is compatible with
the Zephyr/Wakaama client.

**Lesson**: Verify LwM2M client capabilities before configuring advanced observe
strategies. SINGLE is the most compatible option.

---

## 4. Cloud connectivity — Tailscale vs LAN (Session 6)

**Symptom**: Edge cannot connect gRPC to Cloud. Connection timeout.

**Root cause**: Cloud was configured with a Tailscale IP (100.67.60.126)
that was not reachable from the RPi4 on the local network. Tailscale was
not installed or configured on the RPi4.

**Solution**: Change `CLOUD_RPC_HOST` to the direct LAN IP `192.168.1.159`.
Verify with `nc -w3 -v 192.168.1.159 7070` from the RPi4.

**Lesson**: For on-premise deployments, use direct LAN IPs. Overlay VPNs like
Tailscale add unnecessary complexity when all components are on the same network.

---

## 5. Hardware — Defective XIAO ESP32-C6 (Sessions 3-5)

**Symptom**: Very weak Thread signal (RSSI < -95dBm), frequent disconnections,
unstable radio.

**Root cause**: The first XIAO ESP32-C6 had a factory defect in the antenna
or radio chip.

**Solution**: Replace with a second XIAO ESP32-C6. The new device maintains
a stable RSSI of -86dBm with 66% LQI.

**Lesson**: Before extensive firmware debugging, consider hardware replacement.
A defective module can waste days of troubleshooting.

---

## 6. Docker Host Networking required for Thread/IPv6 (Session 2)

**Symptom**: TB Edge does not receive LwM2M packets from the Thread node.

**Root cause**: With Docker bridge networking, Thread mesh-local IPv6 packets
do not reach the container. OTBR listens on host interfaces, but Docker bridge
creates an isolated network.

**Solution**: Use `network_mode: host` in Edge docker-compose.yml.

**Lesson**: For services that need access to specific host network interfaces
(Thread, 802.15.4, IPv6 link-local), Docker bridge does not work.
Host networking is required.

---

## 7. PostgreSQL credentials mismatch (Session 5)

**Symptom**: TB Edge fails to start — cannot connect to PostgreSQL.

**Root cause**: docker-compose.yml had `postgres` as the user but the database
was initialized with `tb_edge`.

**Solution**: Align credentials in docker-compose.yml:
```yaml
SPRING_DATASOURCE_USERNAME: "tb_edge"
SPRING_DATASOURCE_PASSWORD: "tb_edge_pwd"
```
And in the postgres service:
```yaml
POSTGRES_USER: "tb_edge"
POSTGRES_PASSWORD: "tb_edge_pwd"
POSTGRES_DB: "tb_edge"
```

**Lesson**: Database credentials must be consistent between the service that
creates them (postgres) and the one that consumes them (tb-edge).

---

## Useful Tools Summary

| Tool | Use |
|------|-----|
| `tools/read_no_reset.py` | Serial monitor without resetting ESP32 (avoids RTS toggle) |
| `tools/quick_diag.py` | Quick diagnostics: Thread status + LwM2M registration |
| `tools/serial_diag.py` | Serial capture with timestamps and filters |
| `nc -w3 -v HOST PORT` | Verify TCP connectivity from RPi4 |
| `docker logs tb-edge --tail 100` | Last Edge logs |
| `ot-ctl state` | OTBR status (leader/router/child) |
| `ot-ctl neighbor table` | Visible Thread neighbors |
