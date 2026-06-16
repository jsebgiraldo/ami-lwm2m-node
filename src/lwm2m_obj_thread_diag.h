/*
 * LwM2M Object 33000 — Thread + LwM2M Client Diagnostics (Reduced + Golden)
 *
 * Custom object combining:
 *   - v2.0: Thread role, partition ID, MAC counters (RIDs 0-9)
 *   - v2.1 (golden subset): LwM2M client counters for production
 *     observability + correlation with server-side metrics (RIDs 10-16).
 *
 * RIDs 10-16 are designed to validate the PRIO 1-3 fixes empirically:
 *   - register_attempts / register_success → PRIO 1 recovery effectiveness
 *   - notify_emitted / notify_throttled    → PRIO 2 throttle observability
 *   - recover_count / restart_success      → PRIO 1 escalation tracking
 *   - uptime_seconds                       → context for delta calculations
 *
 * All counters are monotonic-from-boot. The server computes deltas across
 * Read intervals — the client never resets.
 */

#ifndef LWM2M_OBJ_THREAD_DIAG_H
#define LWM2M_OBJ_THREAD_DIAG_H

#define THREAD_DIAG_OBJECT_ID     33000

/* === Thread MAC counters (v2.0 — RIDs 0-9) === */
#define TD_ROLE_RID               0   /* String: Disabled/Detached/Child/Router/Leader */
#define TD_PARTITION_ID_RID       1   /* U32: Thread partition ID */
#define TD_TX_TOTAL_RID           2   /* U32: MAC total TX */
#define TD_RX_TOTAL_RID           3   /* U32: MAC total RX */
#define TD_TX_UNICAST_RID         4   /* U32: MAC TX unicast */
#define TD_RX_UNICAST_RID         5   /* U32: MAC RX unicast */
#define TD_TX_BROADCAST_RID       6   /* U32: MAC TX broadcast */
#define TD_RX_BROADCAST_RID       7   /* U32: MAC RX broadcast */
#define TD_TX_ERR_ABORT_RID       8   /* U32: MAC TX errors (abort) */
#define TD_RX_ERR_NOFRAME_RID     9   /* U32: MAC RX errors (no frame) */

/* === LwM2M client counters (v2.1 golden — RIDs 10-16) === */
#define TD_UPTIME_S_RID                10   /* U32: k_uptime_get()/1000 since boot */
#define TD_LWM2M_REG_ATTEMPTS_RID      11   /* U32: total lwm2m_rd_client_start() calls */
#define TD_LWM2M_REG_SUCCESS_RID       12   /* U32: REGISTRATION_COMPLETE events */
#define TD_LWM2M_NOTIFY_EMITTED_RID    13   /* U32: sum of pm_notify_send_count[] */
#define TD_LWM2M_NOTIFY_THROTTLED_RID  14   /* U32: sum of pm_notify_skip_count[] */
#define TD_LWM2M_RECOVER_COUNT_RID     15   /* U32: lwm2m_recover_work_fn invocations */
#define TD_LWM2M_RESTART_SUCCESS_RID   16   /* U32: REG_COMPLETE following recovery */

/* === v2.2 production hardening — diagnostics + watchdog + jitter (RIDs 17-20) ===
 * Added 2026-05-01 after 30-node field validation showed only 27% ACTIVE
 * after 24 h. Each new resource targets a specific failure mode observed:
 *   17/18: PRIO 1.5 — diagnose silent restart failures (which errno?)
 *   19:    PRIO 5   — watchdog rescues from "engine alive but mute" state
 *   20:    PRIO 6   — registration-storm protection (5.03/5.00 backoff x2)
 */
#define TD_LWM2M_LAST_ERROR_CODE_RID   17   /* S32: last errno from rd_client_start (negative or 0) */
#define TD_LWM2M_LAST_ERROR_UPTIME_RID 18   /* U32: uptime_s when last error was recorded */
#define TD_LWM2M_WATCHDOG_COUNT_RID    19   /* U32: times the silence-watchdog forced a recover */
#define TD_LWM2M_STORM_BACKOFF_RID     20   /* U32: times we doubled backoff due to 5.xx/timeout */

/* === v2.3 boot reliability — Bug D + Bug E diagnostics (RIDs 21-22) ===
 * Added 2026-05-05 after the powercycle test where 2/5 nodes failed to boot.
 * Together with PRIO 8 (boot watchdog, sys_reboot if no REGISTER in
 * AMI_BOOT_REGISTER_DEADLINE_S seconds), these RIDs make every reboot
 * observable from the server side without console access.
 */
#define TD_BOOT_LAST_RESET_REASON_RID  21   /* S32: hwinfo_get_reset_cause() bitmap; -1 if read failed */
#define TD_BOOT_TOTAL_RESETS_RID       22   /* U32: monotonic count, persisted in NVS via settings */

/* === v2.4 post-mortem — what was the firmware doing at the last hang? (RIDs 23-28) ===
 * Populated at boot from the NVS snapshot owned by src/post_mortem.c. The
 * snapshot runs periodically (every 60 s) so when the hardware watchdog
 * fires — a HARD reset that runs no software — the most recent record
 * still on flash is a tight description of what the CPU was doing right
 * before the hang. Combined with last_reset_reason (RID 21) the server
 * can tell HW-watchdog hangs from software-initiated reboots and see
 * the firmware's last known state for both.
 *
 * Values stay valid across the lifetime of the boot (the snapshot only
 * captures *previous-boot* context; no in-flight updates).
 */
#define TD_HANG_UPTIME_S_RID           23   /* U32: uptime at the moment of the saved snapshot */
#define TD_HANG_HEAP_FREE_RID          24   /* U32: free heap bytes at snapshot */
#define TD_HANG_HEAP_MIN_FREE_RID      25   /* U32: lifetime min free heap up to snapshot */
#define TD_HANG_REG_AGE_S_RID          26   /* U32: seconds since last REG_UPDATE_COMPLETE at snapshot */
#define TD_HANG_LWM2M_STATE_RID        27   /* U8 : PM_LWM2M_STATE_* bitmap (see post_mortem.h) */
#define TD_HANG_THREAD_ROLE_RID        28   /* U8 : OT role at snapshot */

/* === v2.5 deadlock observability — v0.6.69 audit P3 (RIDs 29-32) ===
 * Added 2026-06-06 after the v0.6.67 soak left 8/30 boards stuck and we
 * could only diagnose them via physical JTAG access. These four RIDs
 * surface the keepalive + recovery state that's currently invisible:
 *   29: keepalive_emit_count        — heartbeat the silence watchdog uses
 *   30: keepalive_consec_fail       — failures the engine is hiding
 *   31: last_emit_uptime_s          — when did the watchdog last see life
 *   32: noreg_boots                 — consecutive boots that never registered
 *   33: in_recovery (bool)          — recover_work_fn currently running
 * Together they let TB Edge dashboards distinguish:
 *   - alive but engine-suspended (high emit / high consec_fail)
 *   - server outage (low emit / 0 consec_fail / high noreg_boots)
 *   - boot loop (low uptime / high total_resets / high noreg_boots)
 */
#define TD_KEEPALIVE_EMIT_RID          29   /* U32: coap_keepalive_get_emit_count() */
#define TD_KEEPALIVE_CONSEC_FAIL_RID   30   /* U32: coap_keepalive_get_consec_fail() */
#define TD_LAST_EMIT_UPTIME_RID        31   /* U32: lwm2m_watchdog_get_last_emit_uptime() */
#define TD_NOREG_BOOTS_RID             32   /* U32: lwm2m_diag_get_noreg_boots() */
#define TD_IN_RECOVERY_RID             33   /* U8 : lwm2m_diag_in_recovery atomic */

/* === v2.6 field-robust observability — v0.6.71 audit P2.7 (RIDs 34-36) ===
 * At-risk indicators that surface a board "trending toward brick" before
 * it actually fails. Operations dashboards can alarm on these crossing
 * thresholds (boot_burst >= 5, detached_total >= 1800, etc).
 *   34: boot_burst        — consecutive unstable boots; 0 = healthy.
 *                          When approaches CONFIG_AMI_BOOT_BURST_MAX, the
 *                          board is about to enter throttle mode.
 *   35: detached_total_s  — cumulative seconds in Thread DETACHED role
 *                          across this boot. Mesh-alone watchdog fires
 *                          when this exceeds CONFIG_AMI_MESH_ALONE_MAX_S.
 *   36: heap_min_free     — lifetime minimum free heap bytes. Drops below
 *                          ~4 KB = OOM imminent.
 */
#define TD_BOOT_BURST_RID              34   /* U32: lwm2m_diag_get_boot_burst() */
#define TD_DETACHED_TOTAL_S_RID        35   /* U32: lwm2m_diag_get_detached_total_s() */
#define TD_HEAP_MIN_FREE_LIVE_RID      36   /* U32: pm_get_live_heap_min_free() */
#define TD_LAST_REBOOT_CODE_RID        37   /* U32: ami_reboot_get_last_code() — which
                                             * reboot path caused THIS boot. 0=power-on/
                                             * external/HW (no SW-drain tag). Codes:
                                             * 1 boot-watchdog, 2 mesh-alone, 3 conn-mon-
                                             * no-first-tick, 4 conn-mon-wedged, 5 max-
                                             * recover-attempts, 6 lwm2m-device-reboot,
                                             * 7 shell, 8 ip6-enable-fail, 9 thread-enable-
                                             * fail, 10 dns-sd-boot-fail, 11 panic, 99 other */

#define TD_NUM_FIELDS             38
#define TD_RES_INST_COUNT         38

#endif /* LWM2M_OBJ_THREAD_DIAG_H */
