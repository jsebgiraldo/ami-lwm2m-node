/*
 * LwM2M Object 10486 — Thread CLI Command
 *
 * Standard OMA object for remote CLI command execution on Thread devices.
 * Write a command string, Execute it, then Read the result.
 *
 * Uses direct OpenThread API calls for known commands instead of
 * hooking into the OT CLI infrastructure (avoids conflicts with
 * CONFIG_OPENTHREAD_SHELL).
 *
 * Supported commands:
 *   state, rloc16, channel, panid, leaderdata, counters mac,
 *   ipaddr, networkname, eui64, extaddr, version
 */

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/lwm2m.h>
#include <openthread.h>
#include <openthread/thread.h>
#include <openthread/link.h>
#include <openthread/instance.h>
#include <openthread/ip6.h>
#include <openthread/dataset.h>
#include <openthread/platform/radio.h>

#include "lwm2m_obj_thread_cli.h"

/* Internal headers for custom object creation */
#include "lwm2m_object.h"
#include "lwm2m_engine.h"

LOG_MODULE_REGISTER(thread_cli, LOG_LEVEL_INF);

/* ================================================================
 * Static data buffers
 * ================================================================ */
static char cli_version[32];
static char cli_command[128];
static char cli_result[128];

/* ================================================================
 * LwM2M Object structures
 * ================================================================ */
#define TCLI_MAX_INST    1
#define TCLI_RI_COUNT    3   /* version + command + result (execute has no ri) */

static struct lwm2m_engine_obj        thread_cli_obj;

/* Forward declaration */
static int cli_execute_cb(uint16_t obj_inst_id,
			  uint8_t *args, uint16_t args_len);

static struct lwm2m_engine_obj_field  thread_cli_fields[] = {
	OBJ_FIELD_DATA(TCLI_VERSION_RID, R_OPT, STRING),
	OBJ_FIELD_DATA(TCLI_COMMAND_RID, RW_OPT, STRING),
	OBJ_FIELD(TCLI_EXECUTE_RID, X_OPT, NONE),
	OBJ_FIELD_DATA(TCLI_RESULT_RID, R, STRING),
};

static struct lwm2m_engine_obj_inst     thread_cli_inst;
static struct lwm2m_engine_res          thread_cli_res[TCLI_NUM_FIELDS];
static struct lwm2m_engine_res_inst     thread_cli_ri[TCLI_RI_COUNT];

/* ================================================================
 * CLI command handler — maps commands to OT API calls
 * ================================================================ */
static void handle_command(const char *cmd, char *result, size_t result_len)
{
	struct otInstance *ot = openthread_get_default_instance();
	if (!ot) {
		snprintf(result, result_len, "Error: No OT instance");
		return;
	}

	openthread_mutex_lock();

	if (strcmp(cmd, "state") == 0) {
		otDeviceRole role = otThreadGetDeviceRole(ot);
		const char *role_str;
		switch (role) {
		case OT_DEVICE_ROLE_DISABLED: role_str = "disabled"; break;
		case OT_DEVICE_ROLE_DETACHED: role_str = "detached"; break;
		case OT_DEVICE_ROLE_CHILD:    role_str = "child"; break;
		case OT_DEVICE_ROLE_ROUTER:   role_str = "router"; break;
		case OT_DEVICE_ROLE_LEADER:   role_str = "leader"; break;
		default: role_str = "unknown"; break;
		}
		snprintf(result, result_len, "%s\nDone", role_str);

	} else if (strcmp(cmd, "rloc16") == 0) {
		snprintf(result, result_len, "0x%04x\nDone",
			 otThreadGetRloc16(ot));

	} else if (strcmp(cmd, "channel") == 0) {
		snprintf(result, result_len, "%d\nDone",
			 (int)otLinkGetChannel(ot));

	} else if (strcmp(cmd, "panid") == 0) {
		snprintf(result, result_len, "0x%04x\nDone",
			 otLinkGetPanId(ot));

	} else if (strcmp(cmd, "leaderdata") == 0) {
		otLeaderData ld;
		if (otThreadGetLeaderData(ot, &ld) == OT_ERROR_NONE) {
			snprintf(result, result_len,
				 "Partition ID: %u\n"
				 "Weighting: %u\n"
				 "Data Version: %u\n"
				 "Stable Data Version: %u\n"
				 "Leader Router ID: %u\nDone",
				 ld.mPartitionId, ld.mWeighting,
				 ld.mDataVersion, ld.mStableDataVersion,
				 ld.mLeaderRouterId);
		} else {
			snprintf(result, result_len, "Error: no leader data");
		}

	} else if (strcmp(cmd, "counters mac") == 0) {
		const otMacCounters *mac = otLinkGetCounters(ot);
		if (mac) {
			snprintf(result, result_len,
				 "TxTotal: %u\n"
				 "TxUnicast: %u\n"
				 "TxBroadcast: %u\n"
				 "TxErrAbort: %u\n"
				 "RxTotal: %u\n"
				 "RxUnicast: %u\n"
				 "RxBroadcast: %u\n"
				 "RxErrNoFrame: %u\nDone",
				 mac->mTxTotal, mac->mTxUnicast,
				 mac->mTxBroadcast, mac->mTxErrAbort,
				 mac->mRxTotal, mac->mRxUnicast,
				 mac->mRxBroadcast, mac->mRxErrNoFrame);
		} else {
			snprintf(result, result_len, "Error: no MAC counters");
		}

	} else if (strcmp(cmd, "ipaddr") == 0) {
		char *p = result;
		size_t rem = result_len;
		const otNetifAddress *addr = otIp6GetUnicastAddresses(ot);
		for (; addr != NULL; addr = addr->mNext) {
			char ip_str[48];
			otIp6AddressToString(&addr->mAddress, ip_str, sizeof(ip_str));
			int n = snprintf(p, rem, "%s\n", ip_str);
			if (n > 0 && (size_t)n < rem) {
				p += n;
				rem -= n;
			}
		}
		snprintf(p, rem, "Done");

	} else if (strcmp(cmd, "networkname") == 0) {
		const char *name = otThreadGetNetworkName(ot);
		snprintf(result, result_len, "%s\nDone", name ? name : "");

	} else if (strcmp(cmd, "eui64") == 0) {
		otExtAddress eui;
		otLinkGetFactoryAssignedIeeeEui64(ot, &eui);
		snprintf(result, result_len,
			 "%02x%02x%02x%02x%02x%02x%02x%02x\nDone",
			 eui.m8[0], eui.m8[1], eui.m8[2], eui.m8[3],
			 eui.m8[4], eui.m8[5], eui.m8[6], eui.m8[7]);

	} else if (strcmp(cmd, "extaddr") == 0) {
		const otExtAddress *ext = otLinkGetExtendedAddress(ot);
		if (ext) {
			snprintf(result, result_len,
				 "%02x%02x%02x%02x%02x%02x%02x%02x\nDone",
				 ext->m8[0], ext->m8[1], ext->m8[2], ext->m8[3],
				 ext->m8[4], ext->m8[5], ext->m8[6], ext->m8[7]);
		}

	} else if (strcmp(cmd, "version") == 0) {
		snprintf(result, result_len, "%s\nDone",
			 otGetVersionString());

	} else if (strcmp(cmd, "dataset active") == 0) {
		otOperationalDataset ds;
		if (otDatasetGetActive(ot, &ds) == OT_ERROR_NONE) {
			snprintf(result, result_len,
				 "Network Name: %s\n"
				 "PAN ID: 0x%04x\n"
				 "Channel: %d\nDone",
				 ds.mNetworkName.m8,
				 ds.mPanId,
				 (int)ds.mChannel);
		} else {
			snprintf(result, result_len, "Error: no active dataset");
		}

	} else if (strcmp(cmd, "help") == 0) {
		snprintf(result, result_len,
			 "Supported: state, rloc16, channel, panid, "
			 "leaderdata, counters mac, ipaddr, networkname, "
			 "eui64, extaddr, version, dataset active, help\nDone");

	} else {
		snprintf(result, result_len,
			 "Error: Unknown command '%s'\n"
			 "Type 'help' for available commands", cmd);
	}

	openthread_mutex_unlock();
}

/* ================================================================
 * Execute callback
 * ================================================================ */
static int cli_execute_cb(uint16_t obj_inst_id,
			  uint8_t *args, uint16_t args_len)
{
	ARG_UNUSED(obj_inst_id);
	ARG_UNUSED(args);
	ARG_UNUSED(args_len);

	if (cli_command[0] == '\0') {
		lwm2m_set_string(&LWM2M_OBJ(THREAD_CLI_OBJECT_ID, 0,
					     TCLI_RESULT_RID),
				 "Error: No command set");
		return -EINVAL;
	}

	LOG_INF("CLI Execute: '%s'", cli_command);
	handle_command(cli_command, cli_result, sizeof(cli_result));

	/* Update via LwM2M API to set correct data_len */
	lwm2m_set_string(&LWM2M_OBJ(THREAD_CLI_OBJECT_ID, 0, TCLI_RESULT_RID),
			 cli_result);

	LOG_INF("CLI Result: %.80s%s", cli_result,
		strlen(cli_result) > 80 ? "..." : "");

	/* Notify observer of result change */
	lwm2m_notify_observer(THREAD_CLI_OBJECT_ID, 0, TCLI_RESULT_RID);

	return 0;
}

/* ================================================================
 * Create callback
 * ================================================================ */
static struct lwm2m_engine_obj_inst *thread_cli_create(uint16_t obj_inst_id)
{
	int i = 0, j = 0;

	init_res_instance(thread_cli_ri, ARRAY_SIZE(thread_cli_ri));

	INIT_OBJ_RES_DATA(TCLI_VERSION_RID, thread_cli_res, i,
			  thread_cli_ri, j,
			  cli_version, sizeof(cli_version));
	INIT_OBJ_RES_DATA(TCLI_COMMAND_RID, thread_cli_res, i,
			  thread_cli_ri, j,
			  cli_command, sizeof(cli_command));
	INIT_OBJ_RES_EXECUTE(TCLI_EXECUTE_RID, thread_cli_res, i,
			      cli_execute_cb);
	INIT_OBJ_RES_DATA(TCLI_RESULT_RID, thread_cli_res, i,
			  thread_cli_ri, j,
			  cli_result, sizeof(cli_result));

	thread_cli_inst.resources = thread_cli_res;
	thread_cli_inst.resource_count = i;

	LOG_DBG("Created Thread CLI instance %u", obj_inst_id);
	return &thread_cli_inst;
}

/* ================================================================
 * Initialization
 * ================================================================ */
void init_thread_cli_object(void)
{
	struct lwm2m_engine_obj_inst *obj_inst = NULL;

	thread_cli_obj.obj_id = THREAD_CLI_OBJECT_ID;
	thread_cli_obj.version_major = 1;
	thread_cli_obj.version_minor = 0;
	thread_cli_obj.is_core = false;
	thread_cli_obj.fields = thread_cli_fields;
	thread_cli_obj.field_count = ARRAY_SIZE(thread_cli_fields);
	thread_cli_obj.max_instance_count = TCLI_MAX_INST;
	thread_cli_obj.create_cb = thread_cli_create;
	lwm2m_register_obj(&thread_cli_obj);

	int ret = lwm2m_create_obj_inst(THREAD_CLI_OBJECT_ID, 0, &obj_inst);
	if (ret < 0) {
		LOG_ERR("Failed to create Thread CLI instance: %d", ret);
		return;
	}

	/* Set CLI version from OT version string */
	const char *ot_ver = otGetVersionString();
	lwm2m_set_string(&LWM2M_OBJ(THREAD_CLI_OBJECT_ID, 0, TCLI_VERSION_RID),
			 ot_ver ? ot_ver : "unknown");

	/* Initial result */
	lwm2m_set_string(&LWM2M_OBJ(THREAD_CLI_OBJECT_ID, 0, TCLI_RESULT_RID),
			 "Ready");

	LOG_INF("Object 10486 (Thread CLI) initialized — OT %s", cli_version);
}
