/*
 * LwM2M custom Object 33001 — Thread Role Control (v0.6.38)
 *
 * Exposes runtime control of the node's role in the Thread mesh, so the
 * server (TB Edge + an optimizer running in tools/) can promote/demote
 * individual nodes between Child and Router without USB reflash. Coupled
 * with the all-FTD default of v0.6.38 (every node compiled router-eligible),
 * this gives full dynamic router-set management.
 *
 * Resources (Object 33001 / instance 0):
 *   0  become_router       (E)  — otThreadBecomeRouter()
 *   1  become_child        (E)  — otThreadBecomeChild() (force demote)
 *   2  router_upgrade_thr  (RW int 1..32, default 16)  — controls how
 *      aggressively OT auto-promotes Children to Routers when the network
 *      has fewer routers than threshold
 *   3  router_downgrade_thr (RW int 1..32, default 23) — auto-demote when
 *      router count exceeds threshold
 *   4  current_role        (R string) — "Disabled"/"Detached"/"Child"/
 *                                       "Router"/"Leader"
 *   5  is_router_eligible  (R bool)   — true iff compiled FTD
 *
 * MTD compile-time builds will not link in otThreadBecomeRouter; this object
 * is only present in FTD builds. We still register it conditionally so an MTD
 * binary doesn't break the build — see #ifdef below.
 */
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/lwm2m.h>
#include <zephyr/net/openthread.h>
#include <openthread/instance.h>
#include <openthread/thread.h>
#include <openthread/thread_ftd.h>

#include "lwm2m_obj_thread_role.h"

LOG_MODULE_REGISTER(thread_role, LOG_LEVEL_INF);

#define OBJ_ID	33001

#define R_BECOME_ROUTER		0
#define R_BECOME_CHILD		1
#define R_UPGRADE_THRESHOLD	2
#define R_DOWNGRADE_THRESHOLD	3
#define R_CURRENT_ROLE		4
#define R_IS_ROUTER_ELIGIBLE	5

static char role_buf[16] = "Detached";

static const char *role_name(otDeviceRole r)
{
	switch (r) {
	case OT_DEVICE_ROLE_DISABLED: return "Disabled";
	case OT_DEVICE_ROLE_DETACHED: return "Detached";
	case OT_DEVICE_ROLE_CHILD:    return "Child";
	case OT_DEVICE_ROLE_ROUTER:   return "Router";
	case OT_DEVICE_ROLE_LEADER:   return "Leader";
	default:                      return "?";
	}
}

static otInstance *ot_inst(void)
{
	return openthread_get_default_instance();
}

static int become_router_cb(uint16_t obj_inst_id, uint8_t *args, uint16_t args_len)
{
#ifdef CONFIG_OPENTHREAD_FTD
	otInstance *ot = ot_inst();
	otError err = otThreadBecomeRouter(ot);
	LOG_INF("Execute become_router -> %d (%s)", err,
		err == OT_ERROR_NONE ? "OK" : "rejected");
	return (err == OT_ERROR_NONE) ? 0 : -EINVAL;
#else
	LOG_WRN("become_router ignored: this build is MTD (compile-time)");
	return -ENOTSUP;
#endif
}

static int become_child_cb(uint16_t obj_inst_id, uint8_t *args, uint16_t args_len)
{
	otInstance *ot = ot_inst();
	otError err = otThreadBecomeChild(ot);
	LOG_INF("Execute become_child -> %d", err);
	return (err == OT_ERROR_NONE) ? 0 : -EINVAL;
}

static int upgrade_thr_cb(uint16_t obj_inst_id, uint16_t res_id, uint16_t res_inst_id,
			  uint8_t *data, uint16_t data_len, bool last_block,
			  size_t total_size, size_t offset)
{
#ifdef CONFIG_OPENTHREAD_FTD
	int32_t v = (data_len >= sizeof(int32_t)) ? *(int32_t *)data
		   : (data_len > 0)                 ? *(int8_t *)data : 16;
	if (v < 1)  v = 1;
	if (v > 32) v = 32;
	otThreadSetRouterUpgradeThreshold(ot_inst(), (uint8_t)v);
	LOG_INF("router_upgrade_threshold -> %d", v);
#endif
	return 0;
}

static int downgrade_thr_cb(uint16_t obj_inst_id, uint16_t res_id, uint16_t res_inst_id,
			    uint8_t *data, uint16_t data_len, bool last_block,
			    size_t total_size, size_t offset)
{
#ifdef CONFIG_OPENTHREAD_FTD
	int32_t v = (data_len >= sizeof(int32_t)) ? *(int32_t *)data
		   : (data_len > 0)                 ? *(int8_t *)data : 23;
	if (v < 1)  v = 1;
	if (v > 32) v = 32;
	otThreadSetRouterDowngradeThreshold(ot_inst(), (uint8_t)v);
	LOG_INF("router_downgrade_threshold -> %d", v);
#endif
	return 0;
}

void thread_role_refresh(void)
{
	otInstance *ot = ot_inst();
	if (!ot) return;
	const char *r = role_name(otThreadGetDeviceRole(ot));
	if (strcmp(role_buf, r) != 0) {
		strncpy(role_buf, r, sizeof(role_buf) - 1);
		role_buf[sizeof(role_buf) - 1] = '\0';
		lwm2m_set_string(&LWM2M_OBJ(OBJ_ID, 0, R_CURRENT_ROLE), role_buf);
	}
}

int thread_role_init(void)
{
	int ret = lwm2m_create_object_inst(&LWM2M_OBJ(OBJ_ID, 0));
	if (ret < 0) {
		LOG_ERR("Failed to create /33001/0 (%d)", ret);
		return ret;
	}
	/* Seed read-only values so observers see something at registration. */
	lwm2m_set_string(&LWM2M_OBJ(OBJ_ID, 0, R_CURRENT_ROLE), role_buf);
#ifdef CONFIG_OPENTHREAD_FTD
	lwm2m_set_bool(&LWM2M_OBJ(OBJ_ID, 0, R_IS_ROUTER_ELIGIBLE), true);
#else
	lwm2m_set_bool(&LWM2M_OBJ(OBJ_ID, 0, R_IS_ROUTER_ELIGIBLE), false);
#endif

	lwm2m_register_exec_callback(&LWM2M_OBJ(OBJ_ID, 0, R_BECOME_ROUTER), become_router_cb);
	lwm2m_register_exec_callback(&LWM2M_OBJ(OBJ_ID, 0, R_BECOME_CHILD), become_child_cb);
	lwm2m_register_post_write_callback(&LWM2M_OBJ(OBJ_ID, 0, R_UPGRADE_THRESHOLD), upgrade_thr_cb);
	lwm2m_register_post_write_callback(&LWM2M_OBJ(OBJ_ID, 0, R_DOWNGRADE_THRESHOLD), downgrade_thr_cb);

	thread_role_refresh();
	LOG_INF("Object 33001 Thread Role Control initialised (role=%s, FTD=%d)",
		role_buf,
#ifdef CONFIG_OPENTHREAD_FTD
		1
#else
		0
#endif
	);
	return 0;
}
