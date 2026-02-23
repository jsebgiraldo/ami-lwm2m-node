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
 * Object 33000 (Thread Network Diagnostics) — Custom:
 *   Thread-mesh-specific data NOT available in Object 4:
 *   RID 0: Thread Role (string: "Disabled"/"Detached"/"Child"/"Router"/"Leader")
 *   RID 1: RLOC16
 *   RID 2: Partition ID
 *   RID 3: Thread Channel
 *   RID 4: Parent RSSI (dBm, average)
 *   RID 5: Parent RSSI Last (dBm)
 *   RID 6: Parent Link Quality In (0-3)
 *   RID 7: Parent RLOC16
 *   RID 8: TX Total (MAC counter)
 *   RID 9: RX Total (MAC counter)
 *   RID 10: TX Unicast (MAC counter)
 *   RID 11: RX Unicast (MAC counter)
 *   RID 12: TX Broadcast (MAC counter)
 *   RID 13: RX Broadcast (MAC counter)
 *   RID 14: TX Error Abort (MAC counter)
 *   RID 15: RX Error No Frame (MAC counter)
 */

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/lwm2m.h>
#include <zephyr/net/net_if.h>
#include <openthread.h>
#include <openthread/thread.h>
#include <openthread/link.h>
#include <openthread/instance.h>
#include <openthread/ip6.h>
#include <openthread/dataset.h>

#include "lwm2m_obj_thread_diag.h"

/* Internal headers for custom object creation */
#include "lwm2m_object.h"
#include "lwm2m_engine.h"

LOG_MODULE_REGISTER(thread_conn, LOG_LEVEL_INF);

/* ================================================================
 * Object 33000 — Thread Network Diagnostics (Custom)
 * ================================================================ */

#define THREAD_DIAG_MAX_ID    16  /* resource IDs 0..15 */
#define THREAD_DIAG_MAX_INST  1

/* Static data buffers */
static char     role_str[12];
static uint16_t rloc16_val;
static uint32_t partition_id_val;
static uint16_t channel_val;
static int16_t  parent_rssi_avg;
static int16_t  parent_rssi_last;
static uint8_t  parent_lqi;
static uint16_t parent_rloc16_val;
static uint32_t tx_total;
static uint32_t rx_total;
static uint32_t tx_unicast;
static uint32_t rx_unicast;
static uint32_t tx_broadcast;
static uint32_t rx_broadcast;
static uint32_t tx_err_abort;
static uint32_t rx_err_no_frame;

/* LwM2M object structures */
static struct lwm2m_engine_obj thread_diag_obj;
static struct lwm2m_engine_obj_field thread_diag_fields[] = {
	OBJ_FIELD_DATA(TD_ROLE_RID, R, STRING),
	OBJ_FIELD_DATA(TD_RLOC16_RID, R, U16),
	OBJ_FIELD_DATA(TD_PARTITION_ID_RID, R, U32),
	OBJ_FIELD_DATA(TD_CHANNEL_RID, R, U16),
	OBJ_FIELD_DATA(TD_PARENT_RSSI_AVG_RID, R, S16),
	OBJ_FIELD_DATA(TD_PARENT_RSSI_LAST_RID, R, S16),
	OBJ_FIELD_DATA(TD_PARENT_LQI_RID, R, U8),
	OBJ_FIELD_DATA(TD_PARENT_RLOC16_RID, R, U16),
	OBJ_FIELD_DATA(TD_TX_TOTAL_RID, R, U32),
	OBJ_FIELD_DATA(TD_RX_TOTAL_RID, R, U32),
	OBJ_FIELD_DATA(TD_TX_UNICAST_RID, R, U32),
	OBJ_FIELD_DATA(TD_RX_UNICAST_RID, R, U32),
	OBJ_FIELD_DATA(TD_TX_BROADCAST_RID, R, U32),
	OBJ_FIELD_DATA(TD_RX_BROADCAST_RID, R, U32),
	OBJ_FIELD_DATA(TD_TX_ERR_ABORT_RID, R, U32),
	OBJ_FIELD_DATA(TD_RX_ERR_NOFRAME_RID, R, U32),
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
	INIT_OBJ_RES_DATA(TD_RLOC16_RID, thread_diag_res, i, thread_diag_ri, j,
			  &rloc16_val, sizeof(rloc16_val));
	INIT_OBJ_RES_DATA(TD_PARTITION_ID_RID, thread_diag_res, i, thread_diag_ri, j,
			  &partition_id_val, sizeof(partition_id_val));
	INIT_OBJ_RES_DATA(TD_CHANNEL_RID, thread_diag_res, i, thread_diag_ri, j,
			  &channel_val, sizeof(channel_val));
	INIT_OBJ_RES_DATA(TD_PARENT_RSSI_AVG_RID, thread_diag_res, i, thread_diag_ri, j,
			  &parent_rssi_avg, sizeof(parent_rssi_avg));
	INIT_OBJ_RES_DATA(TD_PARENT_RSSI_LAST_RID, thread_diag_res, i, thread_diag_ri, j,
			  &parent_rssi_last, sizeof(parent_rssi_last));
	INIT_OBJ_RES_DATA(TD_PARENT_LQI_RID, thread_diag_res, i, thread_diag_ri, j,
			  &parent_lqi, sizeof(parent_lqi));
	INIT_OBJ_RES_DATA(TD_PARENT_RLOC16_RID, thread_diag_res, i, thread_diag_ri, j,
			  &parent_rloc16_val, sizeof(parent_rloc16_val));
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

	thread_diag_inst.resources = thread_diag_res;
	thread_diag_inst.resource_count = i;

	LOG_DBG("Created Thread Diagnostics instance %u", obj_inst_id);
	return &thread_diag_inst;
}

void init_thread_diag_object(void)
{
	struct lwm2m_engine_obj_inst *obj_inst = NULL;

	thread_diag_obj.obj_id = THREAD_DIAG_OBJECT_ID;
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
	strncpy(role_str, "Detached", sizeof(role_str));
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
	strncpy(role_str, role_to_str(role), sizeof(role_str) - 1);
	role_str[sizeof(role_str) - 1] = '\0';

	/* ---- RLOC16 ---- */
	rloc16_val = otThreadGetRloc16(ot);

	/* ---- Partition ID ---- */
	partition_id_val = otThreadGetPartitionId(ot);

	/* ---- Channel ---- */
	channel_val = otLinkGetChannel(ot);

	/* ---- Parent info (for Object 33000) ---- */
	int8_t rssi_avg = 0, rssi_last = 0;

	if (otThreadGetParentAverageRssi(ot, &rssi_avg) == OT_ERROR_NONE) {
		parent_rssi_avg = (int16_t)rssi_avg;
	}
	if (otThreadGetParentLastRssi(ot, &rssi_last) == OT_ERROR_NONE) {
		parent_rssi_last = (int16_t)rssi_last;
	}

	/* Get parent info for LQI and RLOC16 */
	otRouterInfo parent_info;
	if (otThreadGetParentInfo(ot, &parent_info) == OT_ERROR_NONE) {
		parent_lqi = parent_info.mLinkQualityIn;
		parent_rloc16_val = parent_info.mRloc16;
	}

	/* ---- REAL RSSI for Object 4 ---- */
	/* Scan ALL neighbors to find the best RSSI (works for Router AND Child) */
	uint8_t best_lqi = 0;
	int16_t best_rssi = compute_best_neighbor_rssi(ot, &best_lqi);

	/* Fallback to parent RSSI if neighbor table is empty (e.g. just attached) */
	if (best_rssi <= -128 && parent_rssi_avg != 0) {
		best_rssi = parent_rssi_avg;
		best_lqi = parent_lqi;
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
	lwm2m_set_s16(&LWM2M_OBJ(4, 0, 2), best_rssi);                 /* RSSI dBm (real) */
	lwm2m_set_s16(&LWM2M_OBJ(4, 0, 3), lqi_to_percent(best_lqi));  /* Link Quality % */
	lwm2m_set_u32(&LWM2M_OBJ(4, 0, 8), partition_id_val);           /* Cell ID */

	/* ---- Update Object 4 IP addresses (RID 4) from OpenThread ---- */
	/* OPTDATA resources have no pre-allocated buffer, must use set_res_buf */
	static int prev_ip_count = 0;
	for (int i = 0; i < ip_count; i++) {
		if (i >= prev_ip_count) {
			lwm2m_create_res_inst(&LWM2M_OBJ(4, 0, 4, i));
		}
		lwm2m_set_res_buf(&LWM2M_OBJ(4, 0, 4, i),
				  ip_strs[i], sizeof(ip_strs[i]),
				  strlen(ip_strs[i]) + 1, 0);
	}
	prev_ip_count = ip_count;

	/* ---- Update Object 4 Router IP Addresses (RID 5) ---- */
	static bool router_ip_created = false;
	if (router_ip_str[0] != '\0') {
		if (!router_ip_created) {
			lwm2m_create_res_inst(&LWM2M_OBJ(4, 0, 5, 0));
			router_ip_created = true;
		}
		lwm2m_set_res_buf(&LWM2M_OBJ(4, 0, 5, 0),
				  router_ip_str, sizeof(router_ip_str),
				  strlen(router_ip_str) + 1, 0);
	}

	/* ---- Notify observers ---- */
	lwm2m_notify_observer(4, 0, 2);   /* RSSI */
	lwm2m_notify_observer(4, 0, 3);   /* Link Quality */
	lwm2m_notify_observer(4, 0, 4);   /* IP Addresses */
	lwm2m_notify_observer(4, 0, 5);   /* Router IP */
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_PARENT_RSSI_AVG_RID);
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_TX_TOTAL_RID);
	lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0, TD_RX_TOTAL_RID);

	LOG_INF("Obj4: RSSI=%ddBm LQI=%u%% IPs=%d router=%s",
		best_rssi, lqi_to_percent(best_lqi), ip_count, router_ip_str);
	LOG_INF("Obj33000: role=%s RLOC=0x%04X part=%u ch=%u "
		"parentRSSI=%d/%d TX=%u RX=%u",
		role_str, rloc16_val, partition_id_val, channel_val,
		parent_rssi_avg, parent_rssi_last,
		tx_total, rx_total);
}
