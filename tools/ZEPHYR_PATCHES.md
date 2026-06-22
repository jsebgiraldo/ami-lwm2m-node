# Out-of-tree Zephyr patches

These patches live **outside** the firmware repo (in the Zephyr tree under
`$ZEPHYR_BASE`, i.e. `ESP32/zephyrproject/zephyr`). `west update` or a fresh
checkout **reverts** them — re-apply after any Zephyr sync.

## `zephyr_lwm2m_txbytes.patch` — exact LwM2M wire-byte counter (Object 33000 RID 38)

Adds a weak hook `ami_lwm2m_note_tx(bytes)` at the engine's single data-send
chokepoint (`subsys/net/lib/lwm2m/lwm2m_engine.c`, `socket_send_message()` →
`zsock_send(... msg->cpkt.offset ...)`). The strong symbol is in
`src/thread_conn_monitor.c`; it accumulates exact LwM2M/CoAP wire bytes and
publishes them as Object 33000 RID 38. The weak default keeps upstream Zephyr
samples building unchanged when the app doesn't provide the hook.

Re-apply (from the Zephyr root):

```sh
cd "$ZEPHYR_BASE"          # ESP32/zephyrproject/zephyr
git apply /path/to/ami-lwm2m-node/tools/zephyr_lwm2m_txbytes.patch
# verify:
git diff --stat -- subsys/net/lib/lwm2m/lwm2m_engine.c
```

If `git apply` rejects (engine refactored upstream), apply by hand: add the weak
`ami_lwm2m_note_tx()` above `socket_send_message()`, and call
`ami_lwm2m_note_tx((uint32_t)msg->cpkt.offset);` in that function's success
branch (right after `engine_update_tx_time();`).
