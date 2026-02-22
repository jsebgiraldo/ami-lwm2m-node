/*
 * OMA LwM2M Object 10242 — 3-Phase Power Meter
 *
 * Custom LwM2M object for AMI thesis project.
 * Implements key electrical measurement resources for a
 * 3-phase power meter per the OMA registry definition.
 *
 * All resources are Read-only (R). Data is bound directly
 * to static variables; main.c updates them and calls
 * lwm2m_notify_observer() to trigger Observe notifications.
 */

#define LOG_MODULE_NAME net_lwm2m_power_meter
#define LOG_LEVEL CONFIG_LWM2M_LOG_LEVEL

#include <zephyr/logging/log.h>
LOG_MODULE_REGISTER(LOG_MODULE_NAME);

#include <stdint.h>
#include <zephyr/init.h>

#include "lwm2m_object.h"
#include "lwm2m_engine.h"

#include "lwm2m_obj_power_meter.h"

/* ---------- Static storage (1 instance) ---------- */

/* Strings */
static char pm_manufacturer[PM_MAX_INSTANCES][PM_STRING_MAX];
static char pm_model[PM_MAX_INSTANCES][PM_STRING_MAX];
static char pm_serial[PM_MAX_INSTANCES][PM_STRING_MAX];
static char pm_description[PM_MAX_INSTANCES][PM_STRING_MAX];

/* Phase R */
static double pm_tension_r[PM_MAX_INSTANCES];
static double pm_current_r[PM_MAX_INSTANCES];
static double pm_active_power_r[PM_MAX_INSTANCES];
static double pm_reactive_power_r[PM_MAX_INSTANCES];
static double pm_apparent_power_r[PM_MAX_INSTANCES];
static double pm_pf_r[PM_MAX_INSTANCES];

/* Phase S */
static double pm_tension_s[PM_MAX_INSTANCES];
static double pm_current_s[PM_MAX_INSTANCES];
static double pm_active_power_s[PM_MAX_INSTANCES];
static double pm_reactive_power_s[PM_MAX_INSTANCES];
static double pm_apparent_power_s[PM_MAX_INSTANCES];
static double pm_pf_s[PM_MAX_INSTANCES];

/* Phase T */
static double pm_tension_t[PM_MAX_INSTANCES];
static double pm_current_t[PM_MAX_INSTANCES];
static double pm_active_power_t[PM_MAX_INSTANCES];
static double pm_reactive_power_t[PM_MAX_INSTANCES];
static double pm_apparent_power_t[PM_MAX_INSTANCES];
static double pm_pf_t[PM_MAX_INSTANCES];

/* Totals */
static double pm_3p_active_power[PM_MAX_INSTANCES];
static double pm_3p_reactive_power[PM_MAX_INSTANCES];
static double pm_3p_apparent_power[PM_MAX_INSTANCES];
static double pm_3p_pf[PM_MAX_INSTANCES];
static double pm_active_energy[PM_MAX_INSTANCES];
static double pm_reactive_energy[PM_MAX_INSTANCES];
static double pm_apparent_energy[PM_MAX_INSTANCES];
static double pm_frequency[PM_MAX_INSTANCES];
static double pm_neutral_current[PM_MAX_INSTANCES];

/* ---------- LwM2M engine structures ---------- */

static struct lwm2m_engine_obj power_meter_obj;

static struct lwm2m_engine_obj_field fields[] = {
	/* Strings */
	OBJ_FIELD_DATA(PM_MANUFACTURER_RID,  R_OPT, STRING),
	OBJ_FIELD_DATA(PM_MODEL_NUMBER_RID,  R_OPT, STRING),
	OBJ_FIELD_DATA(PM_SERIAL_NUMBER_RID, R_OPT, STRING),
	OBJ_FIELD_DATA(PM_DESCRIPTION_RID,   R_OPT, STRING),

	/* Phase R — mandatory voltages/currents */
	OBJ_FIELD_DATA(PM_TENSION_R_RID,        R, FLOAT),
	OBJ_FIELD_DATA(PM_CURRENT_R_RID,        R, FLOAT),
	OBJ_FIELD_DATA(PM_ACTIVE_POWER_R_RID,   R_OPT, FLOAT),
	OBJ_FIELD_DATA(PM_REACTIVE_POWER_R_RID, R_OPT, FLOAT),
	OBJ_FIELD_DATA(PM_APPARENT_POWER_R_RID, R_OPT, FLOAT),
	OBJ_FIELD_DATA(PM_POWER_FACTOR_R_RID,   R_OPT, FLOAT),

	/* Phase S — mandatory voltages/currents */
	OBJ_FIELD_DATA(PM_TENSION_S_RID,        R, FLOAT),
	OBJ_FIELD_DATA(PM_CURRENT_S_RID,        R, FLOAT),
	OBJ_FIELD_DATA(PM_ACTIVE_POWER_S_RID,   R_OPT, FLOAT),
	OBJ_FIELD_DATA(PM_REACTIVE_POWER_S_RID, R_OPT, FLOAT),
	OBJ_FIELD_DATA(PM_APPARENT_POWER_S_RID, R_OPT, FLOAT),
	OBJ_FIELD_DATA(PM_POWER_FACTOR_S_RID,   R_OPT, FLOAT),

	/* Phase T — mandatory voltages/currents */
	OBJ_FIELD_DATA(PM_TENSION_T_RID,        R, FLOAT),
	OBJ_FIELD_DATA(PM_CURRENT_T_RID,        R, FLOAT),
	OBJ_FIELD_DATA(PM_ACTIVE_POWER_T_RID,   R_OPT, FLOAT),
	OBJ_FIELD_DATA(PM_REACTIVE_POWER_T_RID, R_OPT, FLOAT),
	OBJ_FIELD_DATA(PM_APPARENT_POWER_T_RID, R_OPT, FLOAT),
	OBJ_FIELD_DATA(PM_POWER_FACTOR_T_RID,   R_OPT, FLOAT),

	/* Totals */
	OBJ_FIELD_DATA(PM_3P_ACTIVE_POWER_RID,   R_OPT, FLOAT),
	OBJ_FIELD_DATA(PM_3P_REACTIVE_POWER_RID, R_OPT, FLOAT),
	OBJ_FIELD_DATA(PM_3P_APPARENT_POWER_RID, R_OPT, FLOAT),
	OBJ_FIELD_DATA(PM_3P_POWER_FACTOR_RID,   R_OPT, FLOAT),
	OBJ_FIELD_DATA(PM_ACTIVE_ENERGY_RID,     R_OPT, FLOAT),
	OBJ_FIELD_DATA(PM_REACTIVE_ENERGY_RID,   R_OPT, FLOAT),
	OBJ_FIELD_DATA(PM_APPARENT_ENERGY_RID,   R_OPT, FLOAT),
	OBJ_FIELD_DATA(PM_FREQUENCY_RID,         R_OPT, FLOAT),
	OBJ_FIELD_DATA(PM_NEUTRAL_CURRENT_RID,   R_OPT, FLOAT),
};

/* Verify field count matches header */
BUILD_ASSERT(ARRAY_SIZE(fields) == PM_NUM_FIELDS,
	     "fields[] size mismatch with PM_NUM_FIELDS");

static struct lwm2m_engine_obj_inst inst[PM_MAX_INSTANCES];
static struct lwm2m_engine_res res[PM_MAX_INSTANCES][PM_NUM_FIELDS];
static struct lwm2m_engine_res_inst res_inst[PM_MAX_INSTANCES][PM_RES_INST_COUNT];

/* ---------- Create callback ---------- */

static struct lwm2m_engine_obj_inst *
power_meter_create(uint16_t obj_inst_id)
{
	int index, i = 0, j = 0;

	/* Check for duplicate */
	for (index = 0; index < PM_MAX_INSTANCES; index++) {
		if (inst[index].obj && inst[index].obj_inst_id == obj_inst_id) {
			LOG_ERR("PowerMeter: instance %u already exists", obj_inst_id);
			return NULL;
		}
	}

	/* Find free slot */
	for (index = 0; index < PM_MAX_INSTANCES; index++) {
		if (!inst[index].obj) {
			break;
		}
	}
	if (index >= PM_MAX_INSTANCES) {
		LOG_ERR("PowerMeter: no free instance slot");
		return NULL;
	}

	/* Clear arrays */
	(void)memset(res[index], 0, sizeof(res[index]));
	init_res_instance(res_inst[index], ARRAY_SIZE(res_inst[index]));

	/* Set default string values */
	snprintf(pm_manufacturer[index], PM_STRING_MAX, "Tesis-AMI");
	snprintf(pm_model[index], PM_STRING_MAX, "XIAO-ESP32-C6");
	snprintf(pm_serial[index], PM_STRING_MAX, "AMI-001");
	snprintf(pm_description[index], PM_STRING_MAX, "3-Phase Power Meter");

	/* Default numeric values */
	pm_tension_r[index] = 120.0;
	pm_current_r[index] = 5.0;
	pm_tension_s[index] = 120.0;
	pm_current_s[index] = 5.0;
	pm_tension_t[index] = 120.0;
	pm_current_t[index] = 5.0;
	pm_frequency[index] = 60.0;

	/* ---------- Wire resources ---------- */

	/* Strings */
	INIT_OBJ_RES_DATA_LEN(PM_MANUFACTURER_RID, res[index], i,
		res_inst[index], j,
		pm_manufacturer[index], PM_STRING_MAX,
		strlen(pm_manufacturer[index]) + 1);

	INIT_OBJ_RES_DATA_LEN(PM_MODEL_NUMBER_RID, res[index], i,
		res_inst[index], j,
		pm_model[index], PM_STRING_MAX,
		strlen(pm_model[index]) + 1);

	INIT_OBJ_RES_DATA_LEN(PM_SERIAL_NUMBER_RID, res[index], i,
		res_inst[index], j,
		pm_serial[index], PM_STRING_MAX,
		strlen(pm_serial[index]) + 1);

	INIT_OBJ_RES_DATA_LEN(PM_DESCRIPTION_RID, res[index], i,
		res_inst[index], j,
		pm_description[index], PM_STRING_MAX,
		strlen(pm_description[index]) + 1);

	/* Phase R */
	INIT_OBJ_RES_DATA(PM_TENSION_R_RID, res[index], i,
		res_inst[index], j,
		&pm_tension_r[index], sizeof(double));

	INIT_OBJ_RES_DATA(PM_CURRENT_R_RID, res[index], i,
		res_inst[index], j,
		&pm_current_r[index], sizeof(double));

	INIT_OBJ_RES_DATA(PM_ACTIVE_POWER_R_RID, res[index], i,
		res_inst[index], j,
		&pm_active_power_r[index], sizeof(double));

	INIT_OBJ_RES_DATA(PM_REACTIVE_POWER_R_RID, res[index], i,
		res_inst[index], j,
		&pm_reactive_power_r[index], sizeof(double));

	INIT_OBJ_RES_DATA(PM_APPARENT_POWER_R_RID, res[index], i,
		res_inst[index], j,
		&pm_apparent_power_r[index], sizeof(double));

	INIT_OBJ_RES_DATA(PM_POWER_FACTOR_R_RID, res[index], i,
		res_inst[index], j,
		&pm_pf_r[index], sizeof(double));

	/* Phase S */
	INIT_OBJ_RES_DATA(PM_TENSION_S_RID, res[index], i,
		res_inst[index], j,
		&pm_tension_s[index], sizeof(double));

	INIT_OBJ_RES_DATA(PM_CURRENT_S_RID, res[index], i,
		res_inst[index], j,
		&pm_current_s[index], sizeof(double));

	INIT_OBJ_RES_DATA(PM_ACTIVE_POWER_S_RID, res[index], i,
		res_inst[index], j,
		&pm_active_power_s[index], sizeof(double));

	INIT_OBJ_RES_DATA(PM_REACTIVE_POWER_S_RID, res[index], i,
		res_inst[index], j,
		&pm_reactive_power_s[index], sizeof(double));

	INIT_OBJ_RES_DATA(PM_APPARENT_POWER_S_RID, res[index], i,
		res_inst[index], j,
		&pm_apparent_power_s[index], sizeof(double));

	INIT_OBJ_RES_DATA(PM_POWER_FACTOR_S_RID, res[index], i,
		res_inst[index], j,
		&pm_pf_s[index], sizeof(double));

	/* Phase T */
	INIT_OBJ_RES_DATA(PM_TENSION_T_RID, res[index], i,
		res_inst[index], j,
		&pm_tension_t[index], sizeof(double));

	INIT_OBJ_RES_DATA(PM_CURRENT_T_RID, res[index], i,
		res_inst[index], j,
		&pm_current_t[index], sizeof(double));

	INIT_OBJ_RES_DATA(PM_ACTIVE_POWER_T_RID, res[index], i,
		res_inst[index], j,
		&pm_active_power_t[index], sizeof(double));

	INIT_OBJ_RES_DATA(PM_REACTIVE_POWER_T_RID, res[index], i,
		res_inst[index], j,
		&pm_reactive_power_t[index], sizeof(double));

	INIT_OBJ_RES_DATA(PM_APPARENT_POWER_T_RID, res[index], i,
		res_inst[index], j,
		&pm_apparent_power_t[index], sizeof(double));

	INIT_OBJ_RES_DATA(PM_POWER_FACTOR_T_RID, res[index], i,
		res_inst[index], j,
		&pm_pf_t[index], sizeof(double));

	/* Totals */
	INIT_OBJ_RES_DATA(PM_3P_ACTIVE_POWER_RID, res[index], i,
		res_inst[index], j,
		&pm_3p_active_power[index], sizeof(double));

	INIT_OBJ_RES_DATA(PM_3P_REACTIVE_POWER_RID, res[index], i,
		res_inst[index], j,
		&pm_3p_reactive_power[index], sizeof(double));

	INIT_OBJ_RES_DATA(PM_3P_APPARENT_POWER_RID, res[index], i,
		res_inst[index], j,
		&pm_3p_apparent_power[index], sizeof(double));

	INIT_OBJ_RES_DATA(PM_3P_POWER_FACTOR_RID, res[index], i,
		res_inst[index], j,
		&pm_3p_pf[index], sizeof(double));

	INIT_OBJ_RES_DATA(PM_ACTIVE_ENERGY_RID, res[index], i,
		res_inst[index], j,
		&pm_active_energy[index], sizeof(double));

	INIT_OBJ_RES_DATA(PM_REACTIVE_ENERGY_RID, res[index], i,
		res_inst[index], j,
		&pm_reactive_energy[index], sizeof(double));

	INIT_OBJ_RES_DATA(PM_APPARENT_ENERGY_RID, res[index], i,
		res_inst[index], j,
		&pm_apparent_energy[index], sizeof(double));

	INIT_OBJ_RES_DATA(PM_FREQUENCY_RID, res[index], i,
		res_inst[index], j,
		&pm_frequency[index], sizeof(double));

	INIT_OBJ_RES_DATA(PM_NEUTRAL_CURRENT_RID, res[index], i,
		res_inst[index], j,
		&pm_neutral_current[index], sizeof(double));

	inst[index].resources = res[index];
	inst[index].resource_count = i;

	LOG_INF("Created 3-Phase Power Meter instance: %d (%d resources)",
		obj_inst_id, i);
	return &inst[index];
}

/* ---------- Object init (runs automatically) ---------- */

static int power_meter_init(void)
{
	power_meter_obj.obj_id = POWER_METER_OBJECT_ID;
	power_meter_obj.version_major = 1;
	power_meter_obj.version_minor = 0;
	power_meter_obj.is_core = false;
	power_meter_obj.fields = fields;
	power_meter_obj.field_count = ARRAY_SIZE(fields);
	power_meter_obj.max_instance_count = PM_MAX_INSTANCES;
	power_meter_obj.create_cb = power_meter_create;
	lwm2m_register_obj(&power_meter_obj);

	LOG_INF("Registered OMA Object 10242 (3-Phase Power Meter)");
	return 0;
}

LWM2M_OBJ_INIT(power_meter_init);
