/*
 * LwM2M custom Object 33001 — Thread Role Control (v0.6.38)
 *
 * Runtime control of the node's Thread mesh role so the server (TB Edge +
 * tools/topology_optimizer.py) can promote/demote individual nodes between
 * Child and Router without USB reflash. Coupled with the all-FTD compile-time
 * default in v0.6.38, this enables dynamic router-set management.
 *
 * Resources (Object 33001 / instance 0):
 *   0  become_router       (E)    otThreadBecomeRouter()
 *   1  become_child        (E)    otThreadBecomeChild() (force demote)
 *   2  router_upgrade_thr  (RW U8) 1..32, default 16
 *   3  router_downgrade_thr(RW U8) 1..32, default 23
 *   4  current_role        (R STRING) "Disabled"/"Detached"/"Child"/"Router"/"Leader"
 *   5  is_router_eligible  (R BOOL)   true iff compiled FTD
 *
 * Registration pattern follows lwm2m_obj_thread_net.c — define
 * lwm2m_engine_obj + obj_fields, create_cb returns the inst, lwm2m_register_obj
 * before lwm2m_create_obj_inst. v0.6.38 first cut tried lwm2m_create_object_inst
 * without registering the obj first and the engine emitted
 * "unable to find obj: 33001 / Failed to create /33001/0 (-2)" — fixed here.
 */
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/lwm2m.h>
#include <zephyr/net/openthread.h>
#include <openthread/instance.h>
#include <openthread/thread.h>
#include <openthread/thread_ftd.h>
#include <string.h>

#include "lwm2m_obj_thread_role.h"

/* Internal LwM2M engine headers — same path as other custom objects */
#include "lwm2m_object.h"
#include "lwm2m_engine.h"

LOG_MODULE_REGISTER(thread_role, LOG_LEVEL_INF);

#define OBJ_ID		33001
#define OBJ_VERSION_MAJOR 1
#define OBJ_VERSION_MINOR 0
#define MAX_INST	1
#define NUM_FIELDS	6

#define R_BECOME_ROUTER		0
#define R_BECOME_CHILD		1
#define R_UPGRADE_THR		2
#define R_DOWNGRADE_THR		3
#define R_CURRENT_ROLE		4
#define R_IS_ROUTER_ELIGIBLE	5

/* ───────── backing storage ───────── */
static char    role_buf[16] = "Detached";
/* Defaults match main.c boot tune (v0.6.65). LwM2M server may overwrite at
 * runtime via /33001/0/2 (upgrade) and /33001/0/3 (downgrade). */
static uint8_t upgrade_thr = 10;
static uint8_t downgrade_thr = 12;
#ifdef CONFIG_OPENTHREAD_FTD
static bool    router_eligible = true;
#else
static bool    router_eligible = false;
#endif

/* ───────── engine structs ───────── */
static struct lwm2m_engine_obj       obj;
static struct lwm2m_engine_obj_field fields[] = {
	OBJ_FIELD_EXECUTE_OPT(R_BECOME_ROUTER),
	OBJ_FIELD_EXECUTE_OPT(R_BECOME_CHILD),
	OBJ_FIELD_DATA(R_UPGRADE_THR,        RW, U8),
	OBJ_FIELD_DATA(R_DOWNGRADE_THR,      RW, U8),
	OBJ_FIELD_DATA(R_CURRENT_ROLE,       R,  STRING),
	OBJ_FIELD_DATA(R_IS_ROUTER_ELIGIBLE, R,  BOOL),
};
static struct lwm2m_engine_obj_inst  inst;
static struct lwm2m_engine_res       res[NUM_FIELDS];
static struct lwm2m_engine_res_inst  ri[NUM_FIELDS];

/* ───────── helpers ───────── */
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

static otInstance *ot_inst(void) { return openthread_get_default_instance(); }

/* ───────── callbacks ───────── */
static int become_router_cb(uint16_t obj_inst_id, uint8_t *args, uint16_t args_len)
{
#ifdef CONFIG_OPENTHREAD_FTD
	otInstance *ot = ot_inst();
	otThreadSetRouterEligible(ot, true);
	otError err = otThreadBecomeRouter(ot);
	LOG_INF("Execute become_router (eligible=true) -> err=%d", err);
	return (err == OT_ERROR_NONE) ? 0 : -EINVAL;
#else
	LOG_WRN("become_router ignored: MTD build");
	return -ENOTSUP;
#endif
}

static int become_child_cb(uint16_t obj_inst_id, uint8_t *args, uint16_t args_len)
{
#ifdef CONFIG_OPENTHREAD_FTD
	otInstance *ot = ot_inst();
	/* v0.6.47 Bug #7 fix: without disabling eligibility, the default
	 * router_upgrade_threshold (=16) makes the mesh auto-promote the node
	 * back to Router in ~60s. SetRouterEligible(false) permanently disables
	 * auto-promotion until become_router is called explicitly. */
	otThreadSetRouterEligible(ot, false);
	otError err = otThreadBecomeChild(ot);
	LOG_INF("Execute become_child (eligible=false) -> err=%d", err);
	return (err == OT_ERROR_NONE) ? 0 : -EINVAL;
#else
	LOG_INF("Execute become_child: MTD build already child");
	return 0;
#endif
}

static int upgrade_thr_post(uint16_t obj_inst_id, uint16_t res_id, uint16_t res_inst_id,
			    uint8_t *data, uint16_t data_len, bool last_block,
			    size_t total_size, size_t offset)
{
#ifdef CONFIG_OPENTHREAD_FTD
	if (upgrade_thr < 1)  upgrade_thr = 1;
	if (upgrade_thr > 32) upgrade_thr = 32;
	otThreadSetRouterUpgradeThreshold(ot_inst(), upgrade_thr);
	LOG_INF("router_upgrade_threshold -> %u", upgrade_thr);
#endif
	return 0;
}

static int downgrade_thr_post(uint16_t obj_inst_id, uint16_t res_id, uint16_t res_inst_id,
			      uint8_t *data, uint16_t data_len, bool last_block,
			      size_t total_size, size_t offset)
{
#ifdef CONFIG_OPENTHREAD_FTD
	if (downgrade_thr < 1)  downgrade_thr = 1;
	if (downgrade_thr > 32) downgrade_thr = 32;
	otThreadSetRouterDowngradeThreshold(ot_inst(), downgrade_thr);
	LOG_INF("router_downgrade_threshold -> %u", downgrade_thr);
#endif
	return 0;
}

/* ───────── create cb ───────── */
static struct lwm2m_engine_obj_inst *create_inst(uint16_t obj_inst_id)
{
	int i = 0, j = 0;
	init_res_instance(ri, ARRAY_SIZE(ri));

	INIT_OBJ_RES_EXECUTE(R_BECOME_ROUTER, res, i, become_router_cb);
	INIT_OBJ_RES_EXECUTE(R_BECOME_CHILD,  res, i, become_child_cb);
	INIT_OBJ_RES(R_UPGRADE_THR, res, i, ri, j, 1, false, true,
		     &upgrade_thr, sizeof(upgrade_thr),
		     NULL, NULL, NULL, upgrade_thr_post, NULL);
	INIT_OBJ_RES(R_DOWNGRADE_THR, res, i, ri, j, 1, false, true,
		     &downgrade_thr, sizeof(downgrade_thr),
		     NULL, NULL, NULL, downgrade_thr_post, NULL);
	INIT_OBJ_RES_DATA(R_CURRENT_ROLE,        res, i, ri, j, role_buf, sizeof(role_buf));
	INIT_OBJ_RES_DATA(R_IS_ROUTER_ELIGIBLE,  res, i, ri, j,
			  &router_eligible, sizeof(router_eligible));

	inst.resources = res;
	inst.resource_count = i;
	LOG_DBG("Created Thread Role Control (33001) instance %u", obj_inst_id);
	return &inst;
}

/* ───────── public ───────── */
void thread_role_refresh(void)
{
	otInstance *ot = ot_inst();
	if (!ot) return;
	const char *r = role_name(otThreadGetDeviceRole(ot));
	if (strncmp(role_buf, r, sizeof(role_buf)) != 0) {
		strncpy(role_buf, r, sizeof(role_buf) - 1);
		role_buf[sizeof(role_buf) - 1] = '\0';
	}
}

int thread_role_init(void)
{
	struct lwm2m_engine_obj_inst *obj_inst = NULL;

	obj.obj_id = OBJ_ID;
	obj.version_major = OBJ_VERSION_MAJOR;
	obj.version_minor = OBJ_VERSION_MINOR;
	obj.is_core = false;
	obj.fields = fields;
	obj.field_count = ARRAY_SIZE(fields);
	obj.max_instance_count = MAX_INST;
	obj.create_cb = create_inst;
	lwm2m_register_obj(&obj);

	int ret = lwm2m_create_obj_inst(OBJ_ID, 0, &obj_inst);
	if (ret < 0) {
		LOG_ERR("Failed to create /33001/0 (%d)", ret);
		return ret;
	}

	thread_role_refresh();
	LOG_INF("Object 33001 Thread Role Control initialised (role=%s, FTD=%d)",
		role_buf, (int)router_eligible);
	return 0;
}
