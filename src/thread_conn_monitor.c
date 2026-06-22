/*
 * Thread Connectivity Monitor — Populates LwM2M Objects 4 and 33000
 *
 * Object 4 (Connectivity Monitoring) — Zephyr built-in:
 *   RID 0: Network Bearer = 21 (IEEE 802.15.4)
 *   RID 1: Available Network Bearers
 *   RID 2: Radio Signal Strength — REAL best-neighbor RSSI (dBm)
 *   RID 3: Link Quality — mapped from Thread LQI (0→0%, 1→33%, 2→66%, 3→100%)
 *   RID 4: IP Addresses — all IPv6 from OpenThread (ML-EID, RLOC, OMR/SLAAC)
 *   RID 5: Router IP Addresses — Thread Leader ALOC / parent RLOC address
 *   RID 8: Cell ID = Thread Partition ID
 *
 * Object 33000 (Thread MAC Diagnostics) — Custom (reduced v2.0):
 *   RID 0: Thread Role (string: "Disabled"/"Detached"/"Child"/"Router"/"Leader")
 *   RID 1: Partition ID
 *   RID 2-9: MAC Counters (TX/RX Total, Unicast, Broadcast, Errors)
 *
 * Note: RLOC16, Channel, Parent RSSI/LQI/RLOC moved to Objects 10483/10485.
 */

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/lwm2m.h>
#include <zephyr/net/net_if.h>
#include <openthread.h>
#include "dlms_meter.h"   /* meter_get_notify_*_total() for v2.1 golden */
#include <openthread/thread.h>
#include <openthread/link.h>
#include <openthread/instance.h>
#include <openthread/ip6.h>
#include <openthread/dataset.h>

#include <math.h>

#include "lwm2m_obj_thread_diag.h"

/* Internal headers for custom object creation */
#include "lwm2m_object.h"
#include "lwm2m_engine.h"

LOG_MODULE_REGISTER(thread_conn, LOG_LEVEL_INF);

/* ================================================================
 * Object 33000 — Thread MAC Diagnostics (Reduced v2.0)
 * ================================================================ */

/* v0.6.22 fix: array MUST hold ALL resource slots that thread_diag_create()
 * touches via INIT_OBJ_RES_DATA. v0.6.19 added RIDs 23-28 (hang_* post-mortem
 * snapshot) — the field array thread_diag_fields[] was extended to 29 entries
 * (TD_NUM_FIELDS), but THIS array stayed at 23. Every create_cb invocation
 * therefore wrote 6 entries past the end of thread_diag_res[] /
 * thread_diag_ri[], corrupting adjacent .bss. Symptom: the entire Object
 * 33000 instance was silently malformed and Zephyr's engine omitted it from
 * the REGISTER payload — operators saw no 33000/* observe activity in
 * TB Edge for v0.6.6 - v0.6.21, and we burned hours hunting the wrong
 * suspects (model version mismatch, profile cache, defaultObjectIDVer, etc).
 * The 0.6.20 version-major/minor fix was necessary but not sufficient: it
 * made TB Edge stop logging "absent in the list" by side-effect of the wire
 * version change, but the underlying buffer-overflow root cause stayed.
 *
 * Must equal TD_NUM_FIELDS. Keep this assertion in step with the header. */
#define THREAD_DIAG_MAX_ID    TD_NUM_FIELDS  /* 29 — RIDs 0..28 */
BUILD_ASSERT(THREAD_DIAG_MAX_ID == TD_NUM_FIELDS,
	     "thread_diag_res[] must cover every RID create_cb touches");
#define THREAD_DIAG_MAX_INST  1

/* Static data buffers — reduced: only Role, Partition ID, MAC counters */
static char     role_str[12];
static uint32_t partition_id_val;
static uint32_t tx_total;
static uint32_t rx_total;
static uint32_t tx_unicast;
static uint32_t rx_unicast;
static uint32_t tx_broadcast;
static uint32_t rx_broadcast;
static uint32_t tx_err_abort;
static uint32_t rx_err_no_frame;

/* === v2.1 golden subset: LwM2M client counters (RIDs 10-16) === */
/* Storage backing the LwM2M resources. Updated by update_thread_diag()
 * from atomic counters maintained in main.c (lwm2m_diag_*).
 */
static uint32_t lwm2m_uptime_s;
static uint32_t lwm2m_reg_attempts;
static uint32_t lwm2m_reg_success;
static uint32_t lwm2m_notify_emitted;
static uint32_t lwm2m_notify_throttled;
static uint32_t lwm2m_recover_count_val;
static uint32_t lwm2m_restart_success;
/* v2.2 — production hardening counters */
static int32_t  lwm2m_last_error_code;
static uint32_t lwm2m_last_error_uptime;
static uint32_t lwm2m_watchdog_count;
static uint32_t lwm2m_storm_backoff;
/* v2.3 — boot reliability counters */
static int32_t  boot_last_reset_reason;   /* hwinfo bitmap; -1 = unknown */
static uint32_t boot_total_resets;        /* persisted via settings */

/* v2.4 — post-mortem snapshot from previous boot (filled in by
 * thread_conn_monitor_publish_pm() at startup; static thereafter). */
static uint32_t hang_uptime_s;
static uint32_t hang_heap_free;
static uint32_t hang_heap_min_free;
static uint32_t hang_reg_age_s;
static uint8_t  hang_lwm2m_state;
static uint8_t  hang_thread_role;

/* v2.5 (v0.6.69 audit P3) — deadlock observability counters. */
static uint32_t keepalive_emit_val;
static uint32_t keepalive_consec_fail_val;
static uint32_t last_emit_uptime_val;
static uint32_t noreg_boots_val;
static uint8_t  in_recovery_val;
/* v2.6 (v0.6.71 audit P2.7) — field-robust at-risk indicators. */
static uint32_t boot_burst_val;
static uint32_t detached_total_s_val;
static uint32_t heap_min_free_live_val;
/* v0.7.4 — which reboot path caused this boot (RID 37). */
static uint32_t last_reboot_code_val;
/* v2.7 — exact LwM2M wire-byte accounting (RID 38). The Zephyr engine's single
 * send chokepoint (lwm2m_message_handling.c: zsock_send of msg->cpkt.offset)
 * calls ami_lwm2m_note_tx() on every successful send. atomic_t because it is
 * written from the LwM2M/workqueue send context and read here in the
 * conn-monitor thread; lwm2m_tx_bytes_val is the buffer the engine resource
 * exposes. */
static atomic_t ami_lwm2m_tx_bytes = ATOMIC_INIT(0);
static uint32_t lwm2m_tx_bytes_val;

/* Called by the Zephyr LwM2M engine on every successful wire send (weak hook in
 * lwm2m_message_handling.c resolves to this strong definition). */
void ami_lwm2m_note_tx(uint32_t bytes)
{
	atomic_add(&ami_lwm2m_tx_bytes, (atomic_val_t)bytes);
}

/* LwM2M object structures */
static struct lwm2m_engine_obj thread_diag_obj;

static struct lwm2m_engine_obj_field thread_diag_fields[] = {
	OBJ_FIELD_DATA(TD_ROLE_RID, R, STRING),
	OBJ_FIELD_DATA(TD_PARTITION_ID_RID, R, U32),
	OBJ_FIELD_DATA(TD_TX_TOTAL_RID, R, U32),
	OBJ_FIELD_DATA(TD_RX_TOTAL_RID, R, U32),
	OBJ_FIELD_DATA(TD_TX_UNICAST_RID, R, U32),
	OBJ_FIELD_DATA(TD_RX_UNICAST_RID, R, U32),
	OBJ_FIELD_DATA(TD_TX_BROADCAST_RID, R, U32),
	OBJ_FIELD_DATA(TD_RX_BROADCAST_RID, R, U32),
	OBJ_FIELD_DATA(TD_TX_ERR_ABORT_RID, R, U32),
	OBJ_FIELD_DATA(TD_RX_ERR_NOFRAME_RID, R, U32),
	/* v2.1 golden — LwM2M client counters */
	OBJ_FIELD_DATA(TD_UPTIME_S_RID, R, U32),
	OBJ_FIELD_DATA(TD_LWM2M_REG_ATTEMPTS_RID, R, U32),
	OBJ_FIELD_DATA(TD_LWM2M_REG_SUCCESS_RID, R, U32),
	OBJ_FIELD_DATA(TD_LWM2M_NOTIFY_EMITTED_RID, R, U32),
	OBJ_FIELD_DATA(TD_LWM2M_NOTIFY_THROTTLED_RID, R, U32),
	OBJ_FIELD_DATA(TD_LWM2M_RECOVER_COUNT_RID, R, U32),
	OBJ_FIELD_DATA(TD_LWM2M_RESTART_SUCCESS_RID, R, U32),
	/* v2.2 — production hardening: diagnostics + watchdog + jitter */
	OBJ_FIELD_DATA(TD_LWM2M_LAST_ERROR_CODE_RID, R, S32),
	OBJ_FIELD_DATA(TD_LWM2M_LAST_ERROR_UPTIME_RID, R, U32),
	OBJ_FIELD_DATA(TD_LWM2M_WATCHDOG_COUNT_RID, R, U32),
	OBJ_FIELD_DATA(TD_LWM2M_STORM_BACKOFF_RID, R, U32),
	/* v2.3 — boot reliability (Bug D + Bug E diagnostics) */
	OBJ_FIELD_DATA(TD_BOOT_LAST_RESET_REASON_RID, R, S32),
	OBJ_FIELD_DATA(TD_BOOT_TOTAL_RESETS_RID, R, U32),
	/* v2.4 — post-mortem snapshot (filled from NVS by post_mortem module) */
	OBJ_FIELD_DATA(TD_HANG_UPTIME_S_RID, R, U32),
	OBJ_FIELD_DATA(TD_HANG_HEAP_FREE_RID, R, U32),
	OBJ_FIELD_DATA(TD_HANG_HEAP_MIN_FREE_RID, R, U32),
	OBJ_FIELD_DATA(TD_HANG_REG_AGE_S_RID, R, U32),
	OBJ_FIELD_DATA(TD_HANG_LWM2M_STATE_RID, R, U8),
	OBJ_FIELD_DATA(TD_HANG_THREAD_ROLE_RID, R, U8),
	/* v2.5 (v0.6.69 audit P3) — deadlock observability */
	OBJ_FIELD_DATA(TD_KEEPALIVE_EMIT_RID, R, U32),
	OBJ_FIELD_DATA(TD_KEEPALIVE_CONSEC_FAIL_RID, R, U32),
	OBJ_FIELD_DATA(TD_LAST_EMIT_UPTIME_RID, R, U32),
	OBJ_FIELD_DATA(TD_NOREG_BOOTS_RID, R, U32),
	OBJ_FIELD_DATA(TD_IN_RECOVERY_RID, R, U8),
	/* v2.6 (v0.6.71 audit P2.7) — field-robust at-risk indicators */
	OBJ_FIELD_DATA(TD_BOOT_BURST_RID, R, U32),
	OBJ_FIELD_DATA(TD_DETACHED_TOTAL_S_RID, R, U32),
	OBJ_FIELD_DATA(TD_HEAP_MIN_FREE_LIVE_RID, R, U32),
	/* v0.7.4 — reboot-path code that caused this boot */
	OBJ_FIELD_DATA(TD_LAST_REBOOT_CODE_RID, R, U32),
	/* v2.7 — exact LwM2M wire bytes since boot */
	OBJ_FIELD_DATA(TD_LWM2M_TX_BYTES_RID, R, U32),
};

static struct lwm2m_engine_obj_inst thread_diag_inst;
static struct lwm2m_engine_res thread_diag_res[THREAD_DIAG_MAX_ID];
static struct lwm2m_engine_res_inst thread_diag_ri[THREAD_DIAG_MAX_ID];

static struct lwm2m_engine_obj_inst *thread_diag_create(uint16_t obj_inst_id)
{
	int i = 0, j = 0;

	init_res_instance(thread_diag_ri, ARRAY_SIZE(thread_diag_ri));

	INIT_OBJ_RES_DATA(TD_ROLE_RID, thread_diag_res, i, thread_diag_ri, j,
			  role_str, sizeof(role_str));
	INIT_OBJ_RES_DATA(TD_PARTITION_ID_RID, thread_diag_res, i, thread_diag_ri, j,
			  &partition_id_val, sizeof(partition_id_val));
	INIT_OBJ_RES_DATA(TD_TX_TOTAL_RID, thread_diag_res, i, thread_diag_ri, j,
			  &tx_total, sizeof(tx_total));
	INIT_OBJ_RES_DATA(TD_RX_TOTAL_RID, thread_diag_res, i, thread_diag_ri, j,
			  &rx_total, sizeof(rx_total));
	INIT_OBJ_RES_DATA(TD_TX_UNICAST_RID, thread_diag_res, i, thread_diag_ri, j,
			  &tx_unicast, sizeof(tx_unicast));
	INIT_OBJ_RES_DATA(TD_RX_UNICAST_RID, thread_diag_res, i, thread_diag_ri, j,
			  &rx_unicast, sizeof(rx_unicast));
	INIT_OBJ_RES_DATA(TD_TX_BROADCAST_RID, thread_diag_res, i, thread_diag_ri, j,
			  &tx_broadcast, sizeof(tx_broadcast));
	INIT_OBJ_RES_DATA(TD_RX_BROADCAST_RID, thread_diag_res, i, thread_diag_ri, j,
			  &rx_broadcast, sizeof(rx_broadcast));
	INIT_OBJ_RES_DATA(TD_TX_ERR_ABORT_RID, thread_diag_res, i, thread_diag_ri, j,
			  &tx_err_abort, sizeof(tx_err_abort));
	INIT_OBJ_RES_DATA(TD_RX_ERR_NOFRAME_RID, thread_diag_res, i, thread_diag_ri, j,
			  &rx_err_no_frame, sizeof(rx_err_no_frame));

	/* v2.1 golden — LwM2M client counters */
	INIT_OBJ_RES_DATA(TD_UPTIME_S_RID, thread_diag_res, i, thread_diag_ri, j,
			  &lwm2m_uptime_s, sizeof(lwm2m_uptime_s));
	INIT_OBJ_RES_DATA(TD_LWM2M_REG_ATTEMPTS_RID, thread_diag_res, i, thread_diag_ri, j,
			  &lwm2m_reg_attempts, sizeof(lwm2m_reg_attempts));
	INIT_OBJ_RES_DATA(TD_LWM2M_REG_SUCCESS_RID, thread_diag_res, i, thread_diag_ri, j,
			  &lwm2m_reg_success, sizeof(lwm2m_reg_success));
	INIT_OBJ_RES_DATA(TD_LWM2M_NOTIFY_EMITTED_RID, thread_diag_res, i, thread_diag_ri, j,
			  &lwm2m_notify_emitted, sizeof(lwm2m_notify_emitted));
	INIT_OBJ_RES_DATA(TD_LWM2M_NOTIFY_THROTTLED_RID, thread_diag_res, i, thread_diag_ri, j,
			  &lwm2m_notify_throttled, sizeof(lwm2m_notify_throttled));
	INIT_OBJ_RES_DATA(TD_LWM2M_RECOVER_COUNT_RID, thread_diag_res, i, thread_diag_ri, j,
			  &lwm2m_recover_count_val, sizeof(lwm2m_recover_count_val));
	INIT_OBJ_RES_DATA(TD_LWM2M_RESTART_SUCCESS_RID, thread_diag_res, i, thread_diag_ri, j,
			  &lwm2m_restart_success, sizeof(lwm2m_restart_success));

	/* v2.2 — production hardening counters */
	INIT_OBJ_RES_DATA(TD_LWM2M_LAST_ERROR_CODE_RID, thread_diag_res, i, thread_diag_ri, j,
			  &lwm2m_last_error_code, sizeof(lwm2m_last_error_code));
	INIT_OBJ_RES_DATA(TD_LWM2M_LAST_ERROR_UPTIME_RID, thread_diag_res, i, thread_diag_ri, j,
			  &lwm2m_last_error_uptime, sizeof(lwm2m_last_error_uptime));
	INIT_OBJ_RES_DATA(TD_LWM2M_WATCHDOG_COUNT_RID, thread_diag_res, i, thread_diag_ri, j,
			  &lwm2m_watchdog_count, sizeof(lwm2m_watchdog_count));
	INIT_OBJ_RES_DATA(TD_LWM2M_STORM_BACKOFF_RID, thread_diag_res, i, thread_diag_ri, j,
			  &lwm2m_storm_backoff, sizeof(lwm2m_storm_backoff));

	/* v2.3 — boot reliability */
	INIT_OBJ_RES_DATA(TD_BOOT_LAST_RESET_REASON_RID, thread_diag_res, i, thread_diag_ri, j,
			  &boot_last_reset_reason, sizeof(boot_last_reset_reason));
	INIT_OBJ_RES_DATA(TD_BOOT_TOTAL_RESETS_RID, thread_diag_res, i, thread_diag_ri, j,
			  &boot_total_resets, sizeof(boot_total_resets));

	/* v2.4 — post-mortem snapshot fields */
	INIT_OBJ_RES_DATA(TD_HANG_UPTIME_S_RID, thread_diag_res, i, thread_diag_ri, j,
			  &hang_uptime_s, sizeof(hang_uptime_s));
	INIT_OBJ_RES_DATA(TD_HANG_HEAP_FREE_RID, thread_diag_res, i, thread_diag_ri, j,
			  &hang_heap_free, sizeof(hang_heap_free));
	INIT_OBJ_RES_DATA(TD_HANG_HEAP_MIN_FREE_RID, thread_diag_res, i, thread_diag_ri, j,
			  &hang_heap_min_free, sizeof(hang_heap_min_free));
	INIT_OBJ_RES_DATA(TD_HANG_REG_AGE_S_RID, thread_diag_res, i, thread_diag_ri, j,
			  &hang_reg_age_s, sizeof(hang_reg_age_s));
	INIT_OBJ_RES_DATA(TD_HANG_LWM2M_STATE_RID, thread_diag_res, i, thread_diag_ri, j,
			  &hang_lwm2m_state, sizeof(hang_lwm2m_state));
	INIT_OBJ_RES_DATA(TD_HANG_THREAD_ROLE_RID, thread_diag_res, i, thread_diag_ri, j,
			  &hang_thread_role, sizeof(hang_thread_role));

	/* v2.5 (v0.6.69 audit P3) — deadlock observability */
	INIT_OBJ_RES_DATA(TD_KEEPALIVE_EMIT_RID, thread_diag_res, i, thread_diag_ri, j,
			  &keepalive_emit_val, sizeof(keepalive_emit_val));
	INIT_OBJ_RES_DATA(TD_KEEPALIVE_CONSEC_FAIL_RID, thread_diag_res, i, thread_diag_ri, j,
			  &keepalive_consec_fail_val, sizeof(keepalive_consec_fail_val));
	INIT_OBJ_RES_DATA(TD_LAST_EMIT_UPTIME_RID, thread_diag_res, i, thread_diag_ri, j,
			  &last_emit_uptime_val, sizeof(last_emit_uptime_val));
	INIT_OBJ_RES_DATA(TD_NOREG_BOOTS_RID, thread_diag_res, i, thread_diag_ri, j,
			  &noreg_boots_val, sizeof(noreg_boots_val));
	INIT_OBJ_RES_DATA(TD_IN_RECOVERY_RID, thread_diag_res, i, thread_diag_ri, j,
			  &in_recovery_val, sizeof(in_recovery_val));

	/* v2.6 (v0.6.71 audit P2.7) — field-robust at-risk indicators */
	INIT_OBJ_RES_DATA(TD_BOOT_BURST_RID, thread_diag_res, i, thread_diag_ri, j,
			  &boot_burst_val, sizeof(boot_burst_val));
	INIT_OBJ_RES_DATA(TD_DETACHED_TOTAL_S_RID, thread_diag_res, i, thread_diag_ri, j,
			  &detached_total_s_val, sizeof(detached_total_s_val));
	INIT_OBJ_RES_DATA(TD_HEAP_MIN_FREE_LIVE_RID, thread_diag_res, i, thread_diag_ri, j,
			  &heap_min_free_live_val, sizeof(heap_min_free_live_val));
	INIT_OBJ_RES_DATA(TD_LAST_REBOOT_CODE_RID, thread_diag_res, i, thread_diag_ri, j,
			  &last_reboot_code_val, sizeof(last_reboot_code_val));

	/* v2.7 — exact LwM2M wire bytes */
	INIT_OBJ_RES_DATA(TD_LWM2M_TX_BYTES_RID, thread_diag_res, i, thread_diag_ri, j,
			  &lwm2m_tx_bytes_val, sizeof(lwm2m_tx_bytes_val));

	thread_diag_inst.resources = thread_diag_res;
	thread_diag_inst.resource_count = i;

	LOG_DBG("Created Thread Diagnostics instance %u", obj_inst_id);
	return &thread_diag_inst;
}

void init_thread_diag_object(void)
{
	struct lwm2m_engine_obj_inst *obj_inst = NULL;

	thread_diag_obj.obj_id = THREAD_DIAG_OBJECT_ID;
	/* v0.6.20: pin object version to 1.0 to match the wire protocol.
	 *
	 * The firmware compiles with CONFIG_LWM2M_VERSION_1_0=y, so the
	 * REGISTER payload's link-format CANNOT encode per-object ;ver=X.Y
	 * attributes (that's a LwM2M 1.1 feature). When this object had
	 * version_major=2; version_minor=2; the Zephyr engine emitted
	 * `</33000>;ver=2.2` into a 1.0-protocol REGISTER payload. TB Edge /
	 * Leshan rejected the malformed link-format silently for this single
	 * object — Leshan's logs showed "Specified object id 33000 absent in
	 * the list supported objects of the client". As a consequence TB Edge
	 * never issued Observe requests for /33000_* and the entire fleet's
	 * diagnostic story (recover_count, watchdog_count, last_reset_reason,
	 * total_resets, etc. on RIDs 11-22) was invisible at the operator
	 * layer for the full lifetime of v0.6.6-v0.6.19. We only found this
	 * during the 2026-05-18 stability push.
	 *
	 * The TB Edge LWM2M_MODEL resource is uploaded as `33000_1.0` (see
	 * tools/tb_edge_upload_models.py:xml_33000) and the profile observes
	 * are at `/33000_1.0/0/N`.
	 *
	 * Future: only bump back to 2.x if/when we enable CONFIG_LWM2M_VERSION_1_1
	 * in firmware *and* validate that the 1.1 wire path + per-object versions
	 * + TB Edge model lookup all line up end-to-end on an isolated node.
	 * Post_mortem fields (RIDs 23-28) remain present in `thread_diag_res[]`
	 * and are populated, but the 1.0 model only advertises RIDs 0-22, so
	 * Leshan will treat 23-28 as opaque (writable but not observed). That's
	 * fine for now; once we move to model 1.1+ we can re-add them.
	 */
	thread_diag_obj.version_major = 1;
	thread_diag_obj.version_minor = 0;
	thread_diag_obj.is_core = false;
	thread_diag_obj.fields = thread_diag_fields;
	thread_diag_obj.field_count = ARRAY_SIZE(thread_diag_fields);
	thread_diag_obj.max_instance_count = THREAD_DIAG_MAX_INST;
	thread_diag_obj.create_cb = thread_diag_create;
	lwm2m_register_obj(&thread_diag_obj);

	int ret = lwm2m_create_obj_inst(THREAD_DIAG_OBJECT_ID, 0, &obj_inst);
	if (ret < 0) {
		LOG_ERR("Failed to create Thread Diag instance: %d", ret);
	}

	/* Set initial role */
	lwm2m_set_string(&LWM2M_OBJ(THREAD_DIAG_OBJECT_ID, 0, TD_ROLE_RID),
			 "Detached");
}

/* Public hook for main.c: copy the post-mortem record loaded from NVS
 * into the static backing storage so the values surface as LwM2M
 * resources. Idempotent. Pass NULL or an invalid record (magic check
 * already done in post_mortem.c) to leave the fields zero — that is
 * the correct "first boot, no history" state. */
void thread_diag_publish_post_mortem(uint32_t uptime_s, uint32_t heap_free,
				     uint32_t heap_min_free, uint32_t reg_age_s,
				     uint8_t lwm2m_state, uint8_t thread_role)
{
	hang_uptime_s     = uptime_s;
	hang_heap_free    = heap_free;
	hang_heap_min_free = heap_min_free;
	hang_reg_age_s    = reg_age_s;
	hang_lwm2m_state  = lwm2m_state;
	hang_thread_role  = thread_role;
}

/* ================================================================
 * Object 4 initialization — set Thread-specific defaults
 * ================================================================ */

void init_connmon_thread(void)
{
	/* Set network bearer to IEEE 802.15.4 (21) */
	lwm2m_set_u8(&LWM2M_OBJ(4, 0, 0), 21);

	/* Available bearers: instance 0 = 802.15.4 */
	lwm2m_create_res_inst(&LWM2M_OBJ(4, 0, 1, 0));
	uint8_t bearer = 21;
	lwm2m_set_res_buf(&LWM2M_OBJ(4, 0, 1, 0), &bearer,
			  sizeof(bearer), sizeof(bearer), 0);

	/* Pre-create IP address resource instances 0-3 (Object 4 RID 4).
	 * Instance 0 already exists (Zephyr connmon init), others are new.
	 * Done here once to avoid noisy "already exists" errors every 60s.
	 */
	for (int i = 0; i < 4; i++) {
		lwm2m_create_res_inst(&LWM2M_OBJ(4, 0, 4, i));
	}

	/* Pre-create router IP instance 0 (Object 4 RID 5) */
	lwm2m_create_res_inst(&LWM2M_OBJ(4, 0, 5, 0));

	LOG_INF("Object 4 (Connectivity Monitoring) initialized for Thread");
}

/* ================================================================
 * Periodic update — called from main loop
 * ================================================================ */

static const char *role_to_str(otDeviceRole role)
{
	switch (role) {
	case OT_DEVICE_ROLE_DISABLED: return "Disabled";
	case OT_DEVICE_ROLE_DETACHED: return "Detached";
	case OT_DEVICE_ROLE_CHILD:    return "Child";
	case OT_DEVICE_ROLE_ROUTER:   return "Router";
	case OT_DEVICE_ROLE_LEADER:   return "Leader";
	default:                      return "Unknown";
	}
}

/*
 * Compute best RSSI across all neighbors in the Thread neighbor table.
 * Returns the highest (least negative) average RSSI.
 * For a Child, this is typically the parent. For a Router, the best peer.
 */
static int16_t compute_best_neighbor_rssi(struct otInstance *ot, uint8_t *best_lqi)
{
	otNeighborInfoIterator iter = OT_NEIGHBOR_INFO_ITERATOR_INIT;
	otNeighborInfo ninfo;
	int16_t best_rssi = -128;  /* worst possible */
	uint8_t best_lqi_val = 0;

	while (otThreadGetNextNeighborInfo(ot, &iter, &ninfo) == OT_ERROR_NONE) {
		if (ninfo.mAverageRssi > best_rssi) {
			best_rssi = ninfo.mAverageRssi;
			best_lqi_val = ninfo.mLinkQualityIn;
		}
	}

	if (best_lqi) {
		*best_lqi = best_lqi_val;
	}
	return best_rssi;
}

/*
 * Map Thread Link Quality (0-3) to percentage (0-100).
 * Thread LQI: 0=unknown/no-link, 1=weak, 2=medium, 3=strong.
 */
static int16_t lqi_to_percent(uint8_t lqi)
{
	switch (lqi) {
	case 3:  return 100;
	case 2:  return 66;
	case 1:  return 33;
	default: return 0;
	}
}

void update_connectivity_metrics(void)
{
	struct otInstance *ot = openthread_get_default_instance();
	if (!ot) {
		return;
	}

	openthread_mutex_lock();

	/* ---- Thread role ---- */
	otDeviceRole role = otThreadGetDeviceRole(ot);
	const char *role_name = role_to_str(role);
	lwm2m_set_string(&LWM2M_OBJ(THREAD_DIAG_OBJECT_ID, 0, TD_ROLE_RID),
			 role_name);
	/* v0.6.65 A1 fix: notify on every role transition (Detached/Child/Router/
	 * Leader). Without this the server's dashboard shows stale role until
	 * the next REG_UPDATE, which masks Child↔Router transitions during
	 * mesh re-balancing — exactly what we need to observe live. */
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_ROLE_RID);

	/* ---- Partition ID ---- */
	partition_id_val = otThreadGetPartitionId(ot);

	/* ---- REAL RSSI for Object 4 ---- */
	/* Scan ALL neighbors to find the best RSSI (works for Router AND Child) */
	uint8_t best_lqi = 0;
	int16_t best_rssi = compute_best_neighbor_rssi(ot, &best_lqi);

	/* Fallback to parent RSSI if neighbor table is empty (e.g. just attached) */
	if (best_rssi <= -128) {
		int8_t p_rssi = 0;
		if (otThreadGetParentAverageRssi(ot, &p_rssi) == OT_ERROR_NONE &&
		    p_rssi != 0) {
			best_rssi = (int16_t)p_rssi;
			otRouterInfo pi;
			if (otThreadGetParentInfo(ot, &pi) == OT_ERROR_NONE) {
				best_lqi = pi.mLinkQualityIn;
			}
		}
	}

	/* ---- MAC Counters ---- */
	const otMacCounters *mac = otLinkGetCounters(ot);
	if (mac) {
		tx_total     = mac->mTxTotal;
		rx_total     = mac->mRxTotal;
		tx_unicast   = mac->mTxUnicast;
		rx_unicast   = mac->mRxUnicast;
		tx_broadcast = mac->mTxBroadcast;
		rx_broadcast = mac->mRxBroadcast;
		tx_err_abort = mac->mTxErrAbort;
		rx_err_no_frame = mac->mRxErrNoFrame;
	}

	/* ---- Collect IPv6 addresses from OpenThread directly ---- */
	/* This is more reliable than Zephyr net_if for Thread addresses */
	static char ip_strs[4][48];  /* up to 4 addresses, 48 chars each */
	int ip_count = 0;
	const otNetifAddress *addr = otIp6GetUnicastAddresses(ot);

	for (; addr != NULL && ip_count < 4; addr = addr->mNext) {
		if (!addr->mValid) {
			continue;
		}
		otIp6AddressToString(&addr->mAddress, ip_strs[ip_count],
				     sizeof(ip_strs[ip_count]));
		ip_count++;
	}

	/* ---- Build Router/Gateway IP (Leader ALOC) ---- */
	static char router_ip_str[48];
	const otIp6Address *leader_rloc = otThreadGetRloc(ot);
	if (leader_rloc) {
		/* Construct Leader ALOC: mesh-local prefix + 0000:00ff:fe00:fc00 */
		const otMeshLocalPrefix *mlp = otThreadGetMeshLocalPrefix(ot);
		if (mlp) {
			otIp6Address leader_aloc;
			memcpy(&leader_aloc, mlp->m8, 8);
			leader_aloc.mFields.m8[8]  = 0x00;
			leader_aloc.mFields.m8[9]  = 0x00;
			leader_aloc.mFields.m8[10] = 0x00;
			leader_aloc.mFields.m8[11] = 0xff;
			leader_aloc.mFields.m8[12] = 0xfe;
			leader_aloc.mFields.m8[13] = 0x00;
			leader_aloc.mFields.m8[14] = 0xfc;
			leader_aloc.mFields.m8[15] = 0x00;
			otIp6AddressToString(&leader_aloc, router_ip_str,
					     sizeof(router_ip_str));
		}
	}

	openthread_mutex_unlock();

	/* ---- Update Object 4 with REAL Thread data ---- */
	lwm2m_set_u32(&LWM2M_OBJ(4, 0, 8), partition_id_val);           /* Cell ID */

	/* ---- Update Object 4 IP addresses (RID 4) from OpenThread ---- */
	/* Instances 0-3 were pre-created in init_connmon_thread() */
	for (int i = 0; i < ip_count; i++) {
		lwm2m_set_res_buf(&LWM2M_OBJ(4, 0, 4, i),
				  ip_strs[i], sizeof(ip_strs[i]),
				  strlen(ip_strs[i]) + 1, 0);
	}

	/* ---- Update Object 4 Router IP Addresses (RID 5) ---- */
	/* Instance 0 was pre-created in init_connmon_thread() */
	if (router_ip_str[0] != '\0') {
		lwm2m_set_res_buf(&LWM2M_OBJ(4, 0, 5, 0),
				  router_ip_str, sizeof(router_ip_str),
				  strlen(router_ip_str) + 1, 0);
	}

	/* ---- Update & notify all observers (v0.18.0) ---- */
	/* Always set values and call notify_observer(). The Zephyr LwM2M
	 * observe engine enforces pmin/pmax: if the server has set pmin=15s,
	 * no CoAP notification will be emitted faster than that, even if
	 * we call notify_observer() every 60s. If pmax is set, the engine
	 * will force a notification even if the value hasn't changed.
	 *
	 * Previous versions used firmware-side filtering (nudge in v0.17.0,
	 * prev_rssi/prev_lqi_pct initially in v0.18.0), but that violated
	 * pmax: a stable RSSI would never trigger a notification, even if
	 * the server expected periodic updates via pmax.
	 */
	int16_t lqi_pct = lqi_to_percent(best_lqi);

	lwm2m_set_s16(&LWM2M_OBJ(4, 0, 2), (int16_t)best_rssi);
	lwm2m_set_s16(&LWM2M_OBJ(4, 0, 3), lqi_pct);

	lwm2m_notify_observer(4, 0, 2);   /* RSSI */
	lwm2m_notify_observer(4, 0, 3);   /* Link Quality */
	lwm2m_notify_observer(4, 0, 4);   /* IP Addresses */
	lwm2m_notify_observer(4, 0, 5);   /* Router IP */
	lwm2m_notify_observer(4, 0, 8);   /* v0.6.65 A1: Cell ID / Thread partition */
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_TX_TOTAL_RID);
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_RX_TOTAL_RID);

	/* === v2.1 golden: snapshot LwM2M client counters from main.c =====
	 * The atomic counters live in main.c (lwm2m_diag_*); we copy them
	 * into the static u32 backings the LwM2M engine sees, then notify
	 * any observers. Engine pmin/pmax controls actual emission rate.
	 */
	extern uint32_t lwm2m_diag_get_reg_attempts(void);
	extern uint32_t lwm2m_diag_get_reg_success(void);
	extern uint32_t lwm2m_diag_get_recover_count(void);
	extern uint32_t lwm2m_diag_get_restart_success(void);
	extern int32_t  lwm2m_diag_get_last_error_code(void);
	extern uint32_t lwm2m_diag_get_last_error_uptime(void);
	extern uint32_t lwm2m_diag_get_watchdog_count(void);
	extern uint32_t lwm2m_diag_get_storm_backoff(void);
	extern int32_t  lwm2m_diag_get_last_reset_reason(void);   /* v2.3 */
	extern uint32_t lwm2m_diag_get_total_resets(void);        /* v2.3 */
	/* v2.5 (v0.6.69 audit P3) — deadlock observability sources */
	extern uint32_t coap_keepalive_get_emit_count(void);
	extern uint32_t coap_keepalive_get_consec_fail(void);
	extern uint32_t lwm2m_watchdog_get_last_emit_uptime(void);
	extern uint32_t lwm2m_diag_get_noreg_boots(void);
	extern bool     lwm2m_diag_get_in_recovery(void);

	lwm2m_uptime_s         = (uint32_t)(k_uptime_get() / 1000);
	lwm2m_reg_attempts     = lwm2m_diag_get_reg_attempts();
	lwm2m_reg_success      = lwm2m_diag_get_reg_success();
	lwm2m_notify_emitted   = meter_get_notify_emitted_total();
	lwm2m_notify_throttled = meter_get_notify_throttled_total();
	lwm2m_recover_count_val = lwm2m_diag_get_recover_count();
	lwm2m_restart_success  = lwm2m_diag_get_restart_success();
	lwm2m_last_error_code  = lwm2m_diag_get_last_error_code();
	lwm2m_last_error_uptime = lwm2m_diag_get_last_error_uptime();
	lwm2m_watchdog_count   = lwm2m_diag_get_watchdog_count();
	lwm2m_storm_backoff    = lwm2m_diag_get_storm_backoff();
	boot_last_reset_reason = lwm2m_diag_get_last_reset_reason();
	boot_total_resets      = lwm2m_diag_get_total_resets();
	/* v2.5 deadlock observability snapshots */
	keepalive_emit_val        = coap_keepalive_get_emit_count();
	keepalive_consec_fail_val = coap_keepalive_get_consec_fail();
	last_emit_uptime_val      = lwm2m_watchdog_get_last_emit_uptime();
	noreg_boots_val           = lwm2m_diag_get_noreg_boots();
	in_recovery_val           = (uint8_t)lwm2m_diag_get_in_recovery();
	/* v2.6 (v0.6.71 audit P2.7) — field-robust at-risk indicators */
	extern uint32_t lwm2m_diag_get_boot_burst(void);
	extern uint32_t lwm2m_diag_get_detached_total_s(void);
	extern uint32_t pm_get_live_heap_min_free(void);
	boot_burst_val          = lwm2m_diag_get_boot_burst();
	detached_total_s_val    = lwm2m_diag_get_detached_total_s();
	heap_min_free_live_val  = pm_get_live_heap_min_free();
	extern uint32_t ami_reboot_get_last_code(void);
	last_reboot_code_val    = ami_reboot_get_last_code();
	/* v2.7 — exact LwM2M wire bytes since boot (engine-fed, RID 38) */
	lwm2m_tx_bytes_val      = (uint32_t)atomic_get(&ami_lwm2m_tx_bytes);

	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_UPTIME_S_RID);
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_LWM2M_REG_ATTEMPTS_RID);
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_LWM2M_REG_SUCCESS_RID);
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_LWM2M_NOTIFY_EMITTED_RID);
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_LWM2M_NOTIFY_THROTTLED_RID);
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_LWM2M_RECOVER_COUNT_RID);
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_LWM2M_RESTART_SUCCESS_RID);
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_LWM2M_LAST_ERROR_CODE_RID);
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_LWM2M_LAST_ERROR_UPTIME_RID);
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_LWM2M_WATCHDOG_COUNT_RID);
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_LWM2M_STORM_BACKOFF_RID);
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_BOOT_LAST_RESET_REASON_RID);
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_BOOT_TOTAL_RESETS_RID);
	/* v2.5 (v0.6.69 audit P3) — deadlock observability notifies */
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_KEEPALIVE_EMIT_RID);
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_KEEPALIVE_CONSEC_FAIL_RID);
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_LAST_EMIT_UPTIME_RID);
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_NOREG_BOOTS_RID);
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_IN_RECOVERY_RID);
	/* v2.6 (v0.6.71 audit P2.7) — field-robust at-risk indicators */
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_BOOT_BURST_RID);
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_DETACHED_TOTAL_S_RID);
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_HEAP_MIN_FREE_LIVE_RID);
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_LAST_REBOOT_CODE_RID);
	/* v2.7 — exact LwM2M wire bytes */
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_LWM2M_TX_BYTES_RID);

	LOG_INF("Obj4: RSSI=%ddBm LQI=%u%% IPs=%d router=%s",
		best_rssi, lqi_to_percent(best_lqi), ip_count, router_ip_str);
	LOG_INF("Obj33000: role=%s part=%u TX=%u RX=%u",
		role_str, partition_id_val, tx_total, rx_total);
	LOG_INF("Obj33000(diag): up=%us reg=%u/%u notify=%u/%u recover=%u/%u",
		lwm2m_uptime_s,
		lwm2m_reg_success, lwm2m_reg_attempts,
		lwm2m_notify_emitted, lwm2m_notify_emitted + lwm2m_notify_throttled,
		lwm2m_restart_success, lwm2m_recover_count_val);

	/* v0.6.11: log SoC die temperature once per minute alongside the
	 * other connectivity metrics so the operator can correlate thermal
	 * trends with operational events.
	 *
	 * v0.6.12: also push the value to IPSO Object 3303/0/5700 so TB
	 * Edge can read it remotely. The IPSO object auto-tracks min/max
	 * (RIDs 5601, 5602) — we only need to set the current value. */
	extern int32_t ami_read_die_temp_mc(void);
	int32_t die_mc = ami_read_die_temp_mc();
	if (die_mc != INT32_MIN) {
		LOG_INF("Obj33000(diag): die_temp=%d.%03d C",
			die_mc / 1000, die_mc < 0 ? -die_mc % 1000 : die_mc % 1000);
		/* Push to IPSO 3303/0/5700 (Sensor Value, Float).
		 * v0.6.65 A1 fix: explicit lwm2m_notify_observer paired with set —
		 * Zephyr's set_* does NOT auto-notify observers; without this the
		 * server-side OBSERVE never receives the temp updates and dashboards
		 * show stale values until the next REG_UPDATE cycle. */
		double temp_c = (double)die_mc / 1000.0;
		lwm2m_set_f64(&LWM2M_OBJ(3303, 0, 5700), temp_c);
		lwm2m_notify_observer(3303, 0, 5700);
	}
}
