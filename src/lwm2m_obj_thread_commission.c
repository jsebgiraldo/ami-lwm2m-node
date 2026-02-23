/*
 * LwM2M Object 10484 — Thread Commissioning (joiner add)
 *
 * Standard OMA object for commissioning Thread devices.
 * Write Joiner EUI64 and PSK, then Execute "Start" to begin.
 *
 * Requires CONFIG_OPENTHREAD_COMMISSIONER=y in prj.conf.
 * The device must be an FTD to act as commissioner.
 */

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/lwm2m.h>
#include <openthread.h>
#include <openthread/thread.h>
#include <openthread/instance.h>

#ifdef CONFIG_OPENTHREAD_COMMISSIONER
#include <openthread/commissioner.h>
#endif

#include "lwm2m_obj_thread_commission.h"

/* Internal headers for custom object creation */
#include "lwm2m_object.h"
#include "lwm2m_engine.h"

LOG_MODULE_REGISTER(thread_commission, LOG_LEVEL_INF);

/* ================================================================
 * Static data buffers
 * ================================================================ */
static char     joiner_eui64[65];     /* EUI64 or "*" for any */
static char     joiner_psk[33];       /* PSKd (6-32 chars) */
static int32_t  commission_state;     /* 0=Disabled, 1=Active */

/* Pending joiner IDs — multi-instance resource (written via lwm2m_set_string) */

/* ================================================================
 * LwM2M Object structures
 * ================================================================ */
#define TC_MAX_INST    1
#define TC_RI_COUNT    6   /* 4 single + 2 pending IDs */

static struct lwm2m_engine_obj        thread_commission_obj;

/* Forward declarations for execute callbacks */
static int commission_start_cb(uint16_t obj_inst_id,
			       uint8_t *args, uint16_t args_len);
static int commission_cancel_cb(uint16_t obj_inst_id,
				uint8_t *args, uint16_t args_len);

static struct lwm2m_engine_obj_field  thread_commission_fields[] = {
	OBJ_FIELD_DATA(TC_JOINER_EUI64_RID, RW, STRING),
	OBJ_FIELD_DATA(TC_JOINER_PSK_RID, RW, STRING),
	OBJ_FIELD(TC_START_RID, X_OPT, NONE),
	OBJ_FIELD(TC_CANCEL_RID, X_OPT, NONE),
	OBJ_FIELD_DATA(TC_STATE_RID, R_OPT, S32),
	OBJ_FIELD_DATA(TC_PENDING_IDS_RID, R_OPT, STRING),
};

static struct lwm2m_engine_obj_inst     thread_commission_inst;
static struct lwm2m_engine_res          thread_commission_res[TC_NUM_FIELDS];
static struct lwm2m_engine_res_inst     thread_commission_ri[TC_RI_COUNT];

/* ================================================================
 * Commissioner callbacks (if enabled)
 * ================================================================ */
#ifdef CONFIG_OPENTHREAD_COMMISSIONER

static void commissioner_state_cb(otCommissionerState aState, void *aContext)
{
	ARG_UNUSED(aContext);

	switch (aState) {
	case OT_COMMISSIONER_STATE_DISABLED:
		commission_state = 0;
		LOG_INF("Commissioner: Disabled");
		break;
	case OT_COMMISSIONER_STATE_ACTIVE:
		commission_state = 1;
		LOG_INF("Commissioner: Active");
		break;
	default:
		LOG_INF("Commissioner state: %d", (int)aState);
		break;
	}
}

static void commissioner_joiner_cb(otCommissionerJoinerEvent aEvent,
				    const otJoinerInfo *aJoinerInfo,
				    const otExtAddress *aJoinerId,
				    void *aContext)
{
	ARG_UNUSED(aContext);
	ARG_UNUSED(aJoinerInfo);

	if (aJoinerId) {
		LOG_INF("Commissioner joiner event %d: %02x%02x%02x%02x%02x%02x%02x%02x",
			(int)aEvent,
			aJoinerId->m8[0], aJoinerId->m8[1],
			aJoinerId->m8[2], aJoinerId->m8[3],
			aJoinerId->m8[4], aJoinerId->m8[5],
			aJoinerId->m8[6], aJoinerId->m8[7]);
	}
}

#endif /* CONFIG_OPENTHREAD_COMMISSIONER */

/* ================================================================
 * Execute: Start commissioning
 * ================================================================ */
static int commission_start_cb(uint16_t obj_inst_id,
			       uint8_t *args, uint16_t args_len)
{
	ARG_UNUSED(obj_inst_id);

#ifdef CONFIG_OPENTHREAD_COMMISSIONER
	struct otInstance *ot = openthread_get_default_instance();
	if (!ot) {
		return -ENODEV;
	}

	uint32_t timeout = 120; /* default 120 seconds */

	/* Parse optional timeout from args: "0='60'" */
	if (args && args_len > 2 && args[0] == '0' && args[1] == '=') {
		timeout = (uint32_t)atoi((const char *)&args[2]);
		if (timeout == 0) {
			timeout = 120;
		}
	}

	openthread_mutex_lock();

	/* Start commissioner */
	otError err = otCommissionerStart(ot,
					  commissioner_state_cb,
					  commissioner_joiner_cb,
					  NULL);
	if (err != OT_ERROR_NONE) {
		openthread_mutex_unlock();
		LOG_ERR("Commissioner start failed: %d", (int)err);
		return -EIO;
	}

	/* Parse EUI64 — use NULL for wildcard "*" */
	otExtAddress *eui = NULL;
	otExtAddress parsed_eui;

	if (joiner_eui64[0] != '*' && joiner_eui64[0] != '\0') {
		/* Parse hex EUI64 string to bytes */
		const char *p = joiner_eui64;
		for (int i = 0; i < 8 && *p; i++) {
			char hex[3] = {0};
			hex[0] = *p++;
			if (*p && *p != ':') {
				hex[1] = *p++;
			}
			parsed_eui.m8[i] = (uint8_t)strtol(hex, NULL, 16);
			if (*p == ':') {
				p++;
			}
		}
		eui = &parsed_eui;
	}

	/* Add joiner */
	err = otCommissionerAddJoiner(ot, eui, joiner_psk, timeout);

	openthread_mutex_unlock();

	if (err != OT_ERROR_NONE) {
		LOG_ERR("Commissioner AddJoiner failed: %d", (int)err);
		return -EIO;
	}

	LOG_INF("Commissioner started: eui=%s psk=%s timeout=%u",
		joiner_eui64, joiner_psk, timeout);
	return 0;

#else
	LOG_WRN("Commissioner not enabled (CONFIG_OPENTHREAD_COMMISSIONER)");
	return -ENOTSUP;
#endif
}

/* ================================================================
 * Execute: Cancel commissioning
 * ================================================================ */
static int commission_cancel_cb(uint16_t obj_inst_id,
				uint8_t *args, uint16_t args_len)
{
	ARG_UNUSED(obj_inst_id);
	ARG_UNUSED(args);
	ARG_UNUSED(args_len);

#ifdef CONFIG_OPENTHREAD_COMMISSIONER
	struct otInstance *ot = openthread_get_default_instance();
	if (!ot) {
		return -ENODEV;
	}

	openthread_mutex_lock();
	otError err = otCommissionerStop(ot);
	openthread_mutex_unlock();

	if (err != OT_ERROR_NONE) {
		LOG_ERR("Commissioner stop failed: %d", (int)err);
		return -EIO;
	}

	commission_state = 0;
	LOG_INF("Commissioner stopped");
	return 0;
#else
	return -ENOTSUP;
#endif
}

/* ================================================================
 * Create callback
 * ================================================================ */
static struct lwm2m_engine_obj_inst *thread_commission_create(uint16_t obj_inst_id)
{
	int i = 0, j = 0;

	init_res_instance(thread_commission_ri, ARRAY_SIZE(thread_commission_ri));

	INIT_OBJ_RES_DATA(TC_JOINER_EUI64_RID, thread_commission_res, i,
			  thread_commission_ri, j,
			  joiner_eui64, sizeof(joiner_eui64));
	INIT_OBJ_RES_DATA(TC_JOINER_PSK_RID, thread_commission_res, i,
			  thread_commission_ri, j,
			  joiner_psk, sizeof(joiner_psk));
	INIT_OBJ_RES_EXECUTE(TC_START_RID, thread_commission_res, i,
			      commission_start_cb);
	INIT_OBJ_RES_EXECUTE(TC_CANCEL_RID, thread_commission_res, i,
			      commission_cancel_cb);
	INIT_OBJ_RES_DATA(TC_STATE_RID, thread_commission_res, i,
			  thread_commission_ri, j,
			  &commission_state, sizeof(commission_state));
	INIT_OBJ_RES_MULTI_OPTDATA(TC_PENDING_IDS_RID, thread_commission_res, i,
				   thread_commission_ri, j, 2, 0);

	thread_commission_inst.resources = thread_commission_res;
	thread_commission_inst.resource_count = i;

	LOG_DBG("Created Thread Commissioning instance %u", obj_inst_id);
	return &thread_commission_inst;
}

/* ================================================================
 * Initialization
 * ================================================================ */
void init_thread_commission_object(void)
{
	struct lwm2m_engine_obj_inst *obj_inst = NULL;

	thread_commission_obj.obj_id = THREAD_COMMISSION_OBJECT_ID;
	thread_commission_obj.version_major = 1;
	thread_commission_obj.version_minor = 0;
	thread_commission_obj.is_core = false;
	thread_commission_obj.fields = thread_commission_fields;
	thread_commission_obj.field_count = ARRAY_SIZE(thread_commission_fields);
	thread_commission_obj.max_instance_count = TC_MAX_INST;
	thread_commission_obj.create_cb = thread_commission_create;
	lwm2m_register_obj(&thread_commission_obj);

	int ret = lwm2m_create_obj_inst(THREAD_COMMISSION_OBJECT_ID, 0, &obj_inst);
	if (ret < 0) {
		LOG_ERR("Failed to create Thread Commission instance: %d", ret);
		return;
	}

	/* Set default values */
	strncpy(joiner_eui64, "*", sizeof(joiner_eui64));
	strncpy(joiner_psk, "", sizeof(joiner_psk));
	commission_state = 0;

	LOG_INF("Object 10484 (Thread Commissioning) initialized");
}
