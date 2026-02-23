/*
 * LwM2M Object 10483 — Thread Network
 *
 * Standard OMA object for Thread network configuration and identity.
 * Provides network name, PAN ID, channel, RLOC16, EUI64, IPv6 addresses, etc.
 *
 * All readable data comes from OpenThread APIs.
 * Writable resources (Name, PAN, Channel, etc.) are exposed for
 * server-side configuration but Write operations are not yet implemented.
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

#include "lwm2m_obj_thread_net.h"

/* Internal headers for custom object creation */
#include "lwm2m_object.h"
#include "lwm2m_engine.h"

LOG_MODULE_REGISTER(thread_net, LOG_LEVEL_INF);

/* ================================================================
 * Static data buffers
 * ================================================================ */
static char     net_name[17];         /* Thread network name (max 16 chars + null) */
static char     pan_id_str[8];        /* "0xABCD" */
static char     xpan_id_str[24];      /* "12:34:56:78:90:ab:cd:ef" */
static char     passphrase[4] = "***"; /* Hidden for security */
static char     master_key[4] = "***"; /* Hidden for security */
static int32_t  channel_val;
static char     mesh_prefix_str[48];  /* "fdc6:63fd:328d:66df::/64" */
static int32_t  max_children_val;
static char     rloc16_str[8];        /* "0x8000" */
static char     eui64_str[24];        /* "AA:BB:CC:DD:EE:FF:00:11" */
static char     ext_mac_str[24];      /* "AA:BB:CC:DD:EE:FF:00:11" */

/* IPv6 addresses — multi-instance resource */
static char     ip_strs[TN_MAX_IPV6][48];

/* ================================================================
 * LwM2M Object structures
 * ================================================================ */
#define TN_MAX_INST    1
/* Resource instances: 10 single + 1 xpan + TN_MAX_IPV6 IPs */
#define TN_RI_COUNT    (10 + 1 + TN_MAX_IPV6)

static struct lwm2m_engine_obj        thread_net_obj;
static struct lwm2m_engine_obj_field  thread_net_fields[] = {
	OBJ_FIELD_DATA(TN_NET_NAME_RID, RW, STRING),
	OBJ_FIELD_DATA(TN_PAN_ID_RID, RW, STRING),
	OBJ_FIELD_DATA(TN_XPAN_ID_RID, RW, STRING),
	OBJ_FIELD_DATA(TN_PASSPHRASE_RID, RW, STRING),
	OBJ_FIELD_DATA(TN_MASTER_KEY_RID, RW, STRING),
	OBJ_FIELD_DATA(TN_CHANNEL_RID, RW, S32),
	OBJ_FIELD_DATA(TN_MESH_PREFIX_RID, RW, STRING),
	OBJ_FIELD_DATA(TN_MAX_CHILDREN_RID, RW, S32),
	OBJ_FIELD_DATA(TN_RLOC16_RID, R, STRING),
	OBJ_FIELD_DATA(TN_EUI64_RID, R, STRING),
	OBJ_FIELD_DATA(TN_EXT_MAC_RID, R, STRING),
	OBJ_FIELD_DATA(TN_IPV6_ADDRS_RID, R, STRING),
};

static struct lwm2m_engine_obj_inst     thread_net_inst;
static struct lwm2m_engine_res          thread_net_res[TN_NUM_FIELDS];
static struct lwm2m_engine_res_inst     thread_net_ri[TN_RI_COUNT];

/* ================================================================
 * Create callback
 * ================================================================ */
static struct lwm2m_engine_obj_inst *thread_net_create(uint16_t obj_inst_id)
{
	int i = 0, j = 0;

	init_res_instance(thread_net_ri, ARRAY_SIZE(thread_net_ri));

	/* Single-instance resources with static buffers */
	INIT_OBJ_RES_DATA(TN_NET_NAME_RID, thread_net_res, i,
			  thread_net_ri, j, net_name, sizeof(net_name));
	INIT_OBJ_RES_DATA(TN_PAN_ID_RID, thread_net_res, i,
			  thread_net_ri, j, pan_id_str, sizeof(pan_id_str));
	/* Extended PAN ID — multi but we support 1 instance, use static buf */
	INIT_OBJ_RES_MULTI_OPTDATA(TN_XPAN_ID_RID, thread_net_res, i,
				   thread_net_ri, j, 1, 0);
	INIT_OBJ_RES_DATA(TN_PASSPHRASE_RID, thread_net_res, i,
			  thread_net_ri, j, passphrase, sizeof(passphrase));
	INIT_OBJ_RES_DATA(TN_MASTER_KEY_RID, thread_net_res, i,
			  thread_net_ri, j, master_key, sizeof(master_key));
	INIT_OBJ_RES_DATA(TN_CHANNEL_RID, thread_net_res, i,
			  thread_net_ri, j, &channel_val, sizeof(channel_val));
	INIT_OBJ_RES_DATA(TN_MESH_PREFIX_RID, thread_net_res, i,
			  thread_net_ri, j, mesh_prefix_str, sizeof(mesh_prefix_str));
	INIT_OBJ_RES_DATA(TN_MAX_CHILDREN_RID, thread_net_res, i,
			  thread_net_ri, j, &max_children_val, sizeof(max_children_val));
	INIT_OBJ_RES_DATA(TN_RLOC16_RID, thread_net_res, i,
			  thread_net_ri, j, rloc16_str, sizeof(rloc16_str));
	INIT_OBJ_RES_DATA(TN_EUI64_RID, thread_net_res, i,
			  thread_net_ri, j, eui64_str, sizeof(eui64_str));
	INIT_OBJ_RES_DATA(TN_EXT_MAC_RID, thread_net_res, i,
			  thread_net_ri, j, ext_mac_str, sizeof(ext_mac_str));
	/* IPv6 Addresses — multi-instance, up to TN_MAX_IPV6 */
	INIT_OBJ_RES_MULTI_OPTDATA(TN_IPV6_ADDRS_RID, thread_net_res, i,
				   thread_net_ri, j, TN_MAX_IPV6, 0);

	thread_net_inst.resources = thread_net_res;
	thread_net_inst.resource_count = i;

	LOG_DBG("Created Thread Network (10483) instance %u", obj_inst_id);
	return &thread_net_inst;
}

/* ================================================================
 * Helper: format EUI64/ExtAddress as hex string
 * ================================================================ */
static void format_ext_addr(const uint8_t *addr, char *buf, size_t len)
{
	snprintf(buf, len, "%02x:%02x:%02x:%02x:%02x:%02x:%02x:%02x",
		 addr[0], addr[1], addr[2], addr[3],
		 addr[4], addr[5], addr[6], addr[7]);
}

/* ================================================================
 * Initialization
 * ================================================================ */
void init_thread_net_object(void)
{
	struct lwm2m_engine_obj_inst *obj_inst = NULL;

	thread_net_obj.obj_id = THREAD_NET_OBJECT_ID;
	thread_net_obj.version_major = 1;
	thread_net_obj.version_minor = 0;
	thread_net_obj.is_core = false;
	thread_net_obj.fields = thread_net_fields;
	thread_net_obj.field_count = ARRAY_SIZE(thread_net_fields);
	thread_net_obj.max_instance_count = TN_MAX_INST;
	thread_net_obj.create_cb = thread_net_create;
	lwm2m_register_obj(&thread_net_obj);

	int ret = lwm2m_create_obj_inst(THREAD_NET_OBJECT_ID, 0, &obj_inst);
	if (ret < 0) {
		LOG_ERR("Failed to create Thread Network instance: %d", ret);
		return;
	}

	/* Set initial defaults */
	strncpy(net_name, "unknown", sizeof(net_name));
	strncpy(pan_id_str, "0x0000", sizeof(pan_id_str));
	max_children_val = 0;

	LOG_INF("Object 10483 (Thread Network) initialized");
}

/* ================================================================
 * Periodic update — called from main loop
 * ================================================================ */
void update_thread_network(void)
{
	struct otInstance *ot = openthread_get_default_instance();
	if (!ot) {
		return;
	}

	openthread_mutex_lock();

	/* ---- Active Dataset info ---- */
	otOperationalDataset dataset;
	if (otDatasetGetActive(ot, &dataset) == OT_ERROR_NONE) {
		if (dataset.mComponents.mIsNetworkNamePresent) {
			strncpy(net_name, dataset.mNetworkName.m8, sizeof(net_name) - 1);
			net_name[sizeof(net_name) - 1] = '\0';
		}
		if (dataset.mComponents.mIsPanIdPresent) {
			snprintf(pan_id_str, sizeof(pan_id_str), "0x%04X",
				 dataset.mPanId);
		}
		if (dataset.mComponents.mIsExtendedPanIdPresent) {
			snprintf(xpan_id_str, sizeof(xpan_id_str),
				 "%02x:%02x:%02x:%02x:%02x:%02x:%02x:%02x",
				 dataset.mExtendedPanId.m8[0],
				 dataset.mExtendedPanId.m8[1],
				 dataset.mExtendedPanId.m8[2],
				 dataset.mExtendedPanId.m8[3],
				 dataset.mExtendedPanId.m8[4],
				 dataset.mExtendedPanId.m8[5],
				 dataset.mExtendedPanId.m8[6],
				 dataset.mExtendedPanId.m8[7]);
		}
	}

	/* ---- Channel ---- */
	channel_val = (int32_t)otLinkGetChannel(ot);

	/* ---- Mesh Local Prefix ---- */
	const otMeshLocalPrefix *mlp = otThreadGetMeshLocalPrefix(ot);
	if (mlp) {
		snprintf(mesh_prefix_str, sizeof(mesh_prefix_str),
			 "%02x%02x:%02x%02x:%02x%02x:%02x%02x::/64",
			 mlp->m8[0], mlp->m8[1], mlp->m8[2], mlp->m8[3],
			 mlp->m8[4], mlp->m8[5], mlp->m8[6], mlp->m8[7]);
	}

	/* ---- Max Children (from config, if FTD) ---- */
#if defined(CONFIG_OPENTHREAD_FTD)
#if defined(CONFIG_OPENTHREAD_MAX_CHILDREN)
	max_children_val = CONFIG_OPENTHREAD_MAX_CHILDREN;
#else
	max_children_val = 10; /* OT default */
#endif
#else
	max_children_val = 0;
#endif

	/* ---- RLOC16 ---- */
	snprintf(rloc16_str, sizeof(rloc16_str), "0x%04X",
		 otThreadGetRloc16(ot));

	/* ---- EUI64 ---- */
	otExtAddress eui64;
	otLinkGetFactoryAssignedIeeeEui64(ot, &eui64);
	format_ext_addr(eui64.m8, eui64_str, sizeof(eui64_str));

	/* ---- Extended MAC Address ---- */
	const otExtAddress *ext_mac = otLinkGetExtendedAddress(ot);
	if (ext_mac) {
		format_ext_addr(ext_mac->m8, ext_mac_str, sizeof(ext_mac_str));
	}

	/* ---- IPv6 Addresses ---- */
	int ip_count = 0;
	const otNetifAddress *addr = otIp6GetUnicastAddresses(ot);
	for (; addr != NULL && ip_count < TN_MAX_IPV6; addr = addr->mNext) {
		if (!addr->mValid) {
			continue;
		}
		otIp6AddressToString(&addr->mAddress, ip_strs[ip_count],
				     sizeof(ip_strs[ip_count]));
		ip_count++;
	}

	/* ---- Extended PAN ID resource instance ---- */
	static bool xpan_created = false;
	if (!xpan_created && xpan_id_str[0] != '\0') {
		lwm2m_create_res_inst(&LWM2M_OBJ(THREAD_NET_OBJECT_ID, 0,
						  TN_XPAN_ID_RID, 0));
		xpan_created = true;
	}
	if (xpan_created) {
		lwm2m_set_res_buf(&LWM2M_OBJ(THREAD_NET_OBJECT_ID, 0,
					      TN_XPAN_ID_RID, 0),
				  xpan_id_str, sizeof(xpan_id_str),
				  strlen(xpan_id_str) + 1, 0);
	}

	openthread_mutex_unlock();

	/* ---- Update IPv6 address resource instances ---- */
	static int prev_ip_count = 0;
	for (int i = 0; i < ip_count; i++) {
		if (i >= prev_ip_count) {
			lwm2m_create_res_inst(&LWM2M_OBJ(THREAD_NET_OBJECT_ID, 0,
							  TN_IPV6_ADDRS_RID, i));
		}
		lwm2m_set_res_buf(&LWM2M_OBJ(THREAD_NET_OBJECT_ID, 0,
					      TN_IPV6_ADDRS_RID, i),
				  ip_strs[i], sizeof(ip_strs[i]),
				  strlen(ip_strs[i]) + 1, 0);
	}
	prev_ip_count = ip_count;

	/* ---- Notify observers ---- */
	lwm2m_notify_observer(THREAD_NET_OBJECT_ID, 0, TN_RLOC16_RID);
	lwm2m_notify_observer(THREAD_NET_OBJECT_ID, 0, TN_CHANNEL_RID);
	lwm2m_notify_observer(THREAD_NET_OBJECT_ID, 0, TN_IPV6_ADDRS_RID);

	LOG_INF("Obj10483: net=%s PAN=%s ch=%d RLOC=%s IPs=%d",
		net_name, pan_id_str, channel_val, rloc16_str, ip_count);
}
