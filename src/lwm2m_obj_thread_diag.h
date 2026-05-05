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

#define TD_NUM_FIELDS             23
#define TD_RES_INST_COUNT         23

#endif /* LWM2M_OBJ_THREAD_DIAG_H */
