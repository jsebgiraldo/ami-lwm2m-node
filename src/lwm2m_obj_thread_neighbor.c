/*
 * LwM2M Object 10485 — Thread Neighbor Information
 *
 * Standard OMA object for Thread neighbor diagnostics.
 * Multiple instances — one per discovered neighbor.
 * Reports RSSI, LQI, role, age, MAC address, error rates.
 */

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/lwm2m.h>
#include <openthread.h>
#include <openthread/thread.h>
#include <openthread/link.h>
#include <openthread/instance.h>

#include "lwm2m_obj_thread_neighbor.h"

/* Internal headers for custom object creation */
#include "lwm2m_object.h"
#include "lwm2m_engine.h"

LOG_MODULE_REGISTER(thread_neighbor, LOG_LEVEL_INF);

/* ================================================================
 * Per-neighbor data buffers
 * ================================================================ */
struct neighbor_data {
	int32_t  role;            /* 0=Child, 1=Router */
	char     rloc16_str[8];   /* "0xNNNN" */
	int32_t  age;             /* seconds */
	int32_t  avg_rssi;        /* dBm */
	int32_t  last_rssi;       /* dBm */
	bool     rx_on_idle;
	bool     ftd;             /* Full Thread Device */
	bool     fnd;             /* Full Network Data */
	char     ext_mac_str[24]; /* "aa:bb:cc:dd:ee:ff:00:11" */
	int32_t  lqi_in;          /* 0-3 */
	int32_t  lqi_out;         /* 0-3 (not available in OT, set to 0) */
	double   frame_error;     /* percentage 0.0-100.0 */
	double   msg_error;       /* percentage 0.0-100.0 */
	int32_t  queued_msgs;     /* count */
};

static struct neighbor_data nd[NI_MAX_INSTANCES];

/* ================================================================
 * LwM2M Object structures — per instance
 * ================================================================ */
static struct lwm2m_engine_obj          thread_neighbor_obj;
static struct lwm2m_engine_obj_field    thread_neighbor_fields[] = {
	OBJ_FIELD_DATA(NI_ROLE_RID, R, S32),
	OBJ_FIELD_DATA(NI_RLOC16_RID, R, STRING),
	OBJ_FIELD_DATA(NI_AGE_RID, R, S32),
	OBJ_FIELD_DATA(NI_AVG_RSSI_RID, R, S32),
	OBJ_FIELD_DATA(NI_LAST_RSSI_RID, R, S32),
	OBJ_FIELD_DATA(NI_RX_ON_IDLE_RID, R, BOOL),
	OBJ_FIELD_DATA(NI_FTD_RID, R, BOOL),
	OBJ_FIELD_DATA(NI_FND_RID, R, BOOL),
	OBJ_FIELD_DATA(NI_EXT_MAC_RID, R, STRING),
	OBJ_FIELD_DATA(NI_LQI_IN_RID, R, S32),
	OBJ_FIELD_DATA(NI_LQI_OUT_RID, R, S32),
	OBJ_FIELD_DATA(NI_FRAME_ERR_RID, R, FLOAT),
	OBJ_FIELD_DATA(NI_MSG_ERR_RID, R, FLOAT),
	OBJ_FIELD_DATA(NI_QUEUED_MSGS_RID, R, S32),
};

static struct lwm2m_engine_obj_inst     neighbor_inst[NI_MAX_INSTANCES];
static struct lwm2m_engine_res          neighbor_res[NI_MAX_INSTANCES][NI_NUM_FIELDS];
static struct lwm2m_engine_res_inst     neighbor_ri[NI_MAX_INSTANCES][NI_NUM_FIELDS];
static bool                             neighbor_inst_created[NI_MAX_INSTANCES];

/* ================================================================
 * Create callback
 * ================================================================ */
static struct lwm2m_engine_obj_inst *neighbor_create_cb(uint16_t obj_inst_id)
{
	int slot = -1;

	/* Find a free slot or match instance ID to slot */
	if (obj_inst_id < NI_MAX_INSTANCES && !neighbor_inst_created[obj_inst_id]) {
		slot = obj_inst_id;
	} else {
		for (int s = 0; s < NI_MAX_INSTANCES; s++) {
			if (!neighbor_inst_created[s]) {
				slot = s;
				break;
			}
		}
	}

	if (slot < 0) {
		LOG_ERR("No free slot for neighbor instance %u", obj_inst_id);
		return NULL;
	}

	int i = 0, j = 0;
	init_res_instance(neighbor_ri[slot], ARRAY_SIZE(neighbor_ri[slot]));

	INIT_OBJ_RES_DATA(NI_ROLE_RID, neighbor_res[slot], i,
			  neighbor_ri[slot], j,
			  &nd[slot].role, sizeof(nd[slot].role));
	INIT_OBJ_RES_DATA(NI_RLOC16_RID, neighbor_res[slot], i,
			  neighbor_ri[slot], j,
			  nd[slot].rloc16_str, sizeof(nd[slot].rloc16_str));
	INIT_OBJ_RES_DATA(NI_AGE_RID, neighbor_res[slot], i,
			  neighbor_ri[slot], j,
			  &nd[slot].age, sizeof(nd[slot].age));
	INIT_OBJ_RES_DATA(NI_AVG_RSSI_RID, neighbor_res[slot], i,
			  neighbor_ri[slot], j,
			  &nd[slot].avg_rssi, sizeof(nd[slot].avg_rssi));
	INIT_OBJ_RES_DATA(NI_LAST_RSSI_RID, neighbor_res[slot], i,
			  neighbor_ri[slot], j,
			  &nd[slot].last_rssi, sizeof(nd[slot].last_rssi));
	INIT_OBJ_RES_DATA(NI_RX_ON_IDLE_RID, neighbor_res[slot], i,
			  neighbor_ri[slot], j,
			  &nd[slot].rx_on_idle, sizeof(nd[slot].rx_on_idle));
	INIT_OBJ_RES_DATA(NI_FTD_RID, neighbor_res[slot], i,
			  neighbor_ri[slot], j,
			  &nd[slot].ftd, sizeof(nd[slot].ftd));
	INIT_OBJ_RES_DATA(NI_FND_RID, neighbor_res[slot], i,
			  neighbor_ri[slot], j,
			  &nd[slot].fnd, sizeof(nd[slot].fnd));
	INIT_OBJ_RES_DATA(NI_EXT_MAC_RID, neighbor_res[slot], i,
			  neighbor_ri[slot], j,
			  nd[slot].ext_mac_str, sizeof(nd[slot].ext_mac_str));
	INIT_OBJ_RES_DATA(NI_LQI_IN_RID, neighbor_res[slot], i,
			  neighbor_ri[slot], j,
			  &nd[slot].lqi_in, sizeof(nd[slot].lqi_in));
	INIT_OBJ_RES_DATA(NI_LQI_OUT_RID, neighbor_res[slot], i,
			  neighbor_ri[slot], j,
			  &nd[slot].lqi_out, sizeof(nd[slot].lqi_out));
	INIT_OBJ_RES_DATA(NI_FRAME_ERR_RID, neighbor_res[slot], i,
			  neighbor_ri[slot], j,
			  &nd[slot].frame_error, sizeof(nd[slot].frame_error));
	INIT_OBJ_RES_DATA(NI_MSG_ERR_RID, neighbor_res[slot], i,
			  neighbor_ri[slot], j,
			  &nd[slot].msg_error, sizeof(nd[slot].msg_error));
	INIT_OBJ_RES_DATA(NI_QUEUED_MSGS_RID, neighbor_res[slot], i,
			  neighbor_ri[slot], j,
			  &nd[slot].queued_msgs, sizeof(nd[slot].queued_msgs));

	neighbor_inst[slot].resources = neighbor_res[slot];
	neighbor_inst[slot].resource_count = i;
	neighbor_inst_created[slot] = true;

	LOG_DBG("Created Thread Neighbor instance %u (slot %d)", obj_inst_id, slot);
	return &neighbor_inst[slot];
}

/* ================================================================
 * Delete callback
 * ================================================================ */
static int neighbor_delete_cb(uint16_t obj_inst_id)
{
	if (obj_inst_id < NI_MAX_INSTANCES) {
		neighbor_inst_created[obj_inst_id] = false;
		memset(&nd[obj_inst_id], 0, sizeof(nd[obj_inst_id]));
	}
	return 0;
}

/* ================================================================
 * Initialization
 * ================================================================ */
void init_thread_neighbor_object(void)
{
	struct lwm2m_engine_obj_inst *obj_inst = NULL;

	thread_neighbor_obj.obj_id = THREAD_NEIGHBOR_OBJECT_ID;
	thread_neighbor_obj.version_major = 1;
	thread_neighbor_obj.version_minor = 0;
	thread_neighbor_obj.is_core = false;
	thread_neighbor_obj.fields = thread_neighbor_fields;
	thread_neighbor_obj.field_count = ARRAY_SIZE(thread_neighbor_fields);
	thread_neighbor_obj.max_instance_count = NI_MAX_INSTANCES;
	thread_neighbor_obj.create_cb = neighbor_create_cb;
	thread_neighbor_obj.delete_cb = neighbor_delete_cb;
	lwm2m_register_obj(&thread_neighbor_obj);

	/* Create instance 0 at init so Leshan always sees at least one */
	int ret = lwm2m_create_obj_inst(THREAD_NEIGHBOR_OBJECT_ID, 0, &obj_inst);
	if (ret < 0) {
		LOG_ERR("Failed to create initial neighbor instance: %d", ret);
		return;
	}

	/* Set default string values with proper data_len */
	lwm2m_set_string(&LWM2M_OBJ(THREAD_NEIGHBOR_OBJECT_ID, 0,
				     NI_RLOC16_RID), "N/A");
	lwm2m_set_string(&LWM2M_OBJ(THREAD_NEIGHBOR_OBJECT_ID, 0,
				     NI_EXT_MAC_RID), "N/A");

	LOG_INF("Object 10485 (Thread Neighbor) initialized (max %d)",
		NI_MAX_INSTANCES);
}

/* ================================================================
 * Helper: format ext address
 * ================================================================ */
static void format_ext_addr(const uint8_t *addr, char *buf, size_t len)
{
	snprintf(buf, len, "%02x:%02x:%02x:%02x:%02x:%02x:%02x:%02x",
		 addr[0], addr[1], addr[2], addr[3],
		 addr[4], addr[5], addr[6], addr[7]);
}

/* ================================================================
 * Periodic update — called from main loop
 * ================================================================ */
void update_thread_neighbors(void)
{
	struct otInstance *ot = openthread_get_default_instance();
	if (!ot) {
		return;
	}

	/* Instance 0 always exists (created in init), so prev starts at 1 */
	static int prev_count = 1;

	openthread_mutex_lock();

	/* Iterate OT neighbor table */
	otNeighborInfoIterator iter = OT_NEIGHBOR_INFO_ITERATOR_INIT;
	otNeighborInfo ninfo;
	int count = 0;

	while (otThreadGetNextNeighborInfo(ot, &iter, &ninfo) == OT_ERROR_NONE &&
	       count < NI_MAX_INSTANCES) {

		/* Update data buffer for this neighbor */
		nd[count].role = ninfo.mIsChild ? 0 : 1;
		snprintf(nd[count].rloc16_str, sizeof(nd[count].rloc16_str),
			 "0x%04X", ninfo.mRloc16);
		nd[count].age = (int32_t)ninfo.mAge;
		nd[count].avg_rssi = (int32_t)ninfo.mAverageRssi;
		nd[count].last_rssi = (int32_t)ninfo.mLastRssi;
		nd[count].rx_on_idle = ninfo.mRxOnWhenIdle;
		nd[count].ftd = ninfo.mFullThreadDevice;
		nd[count].fnd = ninfo.mFullNetworkData;
		format_ext_addr(ninfo.mExtAddress.m8, nd[count].ext_mac_str,
				sizeof(nd[count].ext_mac_str));
		nd[count].lqi_in = (int32_t)ninfo.mLinkQualityIn;
		nd[count].lqi_out = 0; /* Not directly available in OT */
		/* Frame/Message error rates: OT uses 0xFFFF scale -> convert to % */
		nd[count].frame_error =
			(double)ninfo.mFrameErrorRate * 100.0 / 0xFFFF;
		nd[count].msg_error =
			(double)ninfo.mMessageErrorRate * 100.0 / 0xFFFF;
		nd[count].queued_msgs = 0; /* Not directly available */

		count++;
	}

	openthread_mutex_unlock();

	/* Keep at least instance 0 alive (created in init) */
	int effective = count > 0 ? count : 1;

	/* Create new instances if effective > prev_count (instance 0 already exists) */
	for (int i = prev_count; i < effective; i++) {
		struct lwm2m_engine_obj_inst *inst = NULL;
		int ret = lwm2m_create_obj_inst(THREAD_NEIGHBOR_OBJECT_ID, i, &inst);
		if (ret < 0) {
			LOG_ERR("Failed to create neighbor inst %d: %d", i, ret);
		}
	}

	/* Delete excess instances if effective < prev_count (never delete inst 0) */
	for (int i = effective; i < prev_count; i++) {
		lwm2m_delete_object_inst(&LWM2M_OBJ(THREAD_NEIGHBOR_OBJECT_ID, i));
		neighbor_inst_created[i] = false;
	}

	prev_count = effective;

	/* Update string resources via lwm2m_set_string for proper data_len */
	for (int i = 0; i < count; i++) {
		lwm2m_set_string(&LWM2M_OBJ(THREAD_NEIGHBOR_OBJECT_ID, i,
					     NI_RLOC16_RID),
				 nd[i].rloc16_str);
		lwm2m_set_string(&LWM2M_OBJ(THREAD_NEIGHBOR_OBJECT_ID, i,
					     NI_EXT_MAC_RID),
				 nd[i].ext_mac_str);
	}

	/* If no neighbors, clear instance 0 with defaults */
	if (count == 0) {
		memset(&nd[0], 0, sizeof(nd[0]));
		lwm2m_set_string(&LWM2M_OBJ(THREAD_NEIGHBOR_OBJECT_ID, 0,
					     NI_RLOC16_RID), "N/A");
		lwm2m_set_string(&LWM2M_OBJ(THREAD_NEIGHBOR_OBJECT_ID, 0,
					     NI_EXT_MAC_RID), "N/A");
	}

	/* Notify observers for all active instances */
	for (int i = 0; i < effective; i++) {
		lwm2m_notify_observer(THREAD_NEIGHBOR_OBJECT_ID, i, NI_AVG_RSSI_RID);
		lwm2m_notify_observer(THREAD_NEIGHBOR_OBJECT_ID, i, NI_AGE_RID);
	}

	LOG_INF("Obj10485: %d neighbor(s) updated", count);
}
