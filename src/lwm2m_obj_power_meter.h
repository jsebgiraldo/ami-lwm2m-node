/*
 * OMA LwM2M Object 10242 — 3-Phase Power Meter
 * https://devtoolkit.openmobilealliance.org/OEditor/LWMOView
 *
 * Custom implementation for AMI thesis.
 * Supports key resources: voltages, currents, active power,
 * power factor, frequency, and energy.
 */

#ifndef LWM2M_OBJ_POWER_METER_H_
#define LWM2M_OBJ_POWER_METER_H_

#define POWER_METER_OBJECT_ID   10242

/* Resource IDs — from OMA 10242.xml */
#define PM_MANUFACTURER_RID      0
#define PM_MODEL_NUMBER_RID      1
#define PM_SERIAL_NUMBER_RID     2
#define PM_DESCRIPTION_RID       3

/* Phase R (1) */
#define PM_TENSION_R_RID         4   /* Mandatory - V */
#define PM_CURRENT_R_RID         5   /* Mandatory - A */
#define PM_ACTIVE_POWER_R_RID    6   /* kW */
#define PM_REACTIVE_POWER_R_RID  7   /* kvar */
#define PM_APPARENT_POWER_R_RID  10  /* kVA */
#define PM_POWER_FACTOR_R_RID    11  /* -1..1 */

/* Phase S (2) */
#define PM_TENSION_S_RID         14  /* Mandatory - V */
#define PM_CURRENT_S_RID         15  /* Mandatory - A */
#define PM_ACTIVE_POWER_S_RID    16  /* kW */
#define PM_REACTIVE_POWER_S_RID  17  /* kvar */
#define PM_APPARENT_POWER_S_RID  20  /* kVA */
#define PM_POWER_FACTOR_S_RID    21  /* -1..1 */

/* Phase T (3) */
#define PM_TENSION_T_RID         24  /* Mandatory - V */
#define PM_CURRENT_T_RID         25  /* Mandatory - A */
#define PM_ACTIVE_POWER_T_RID    26  /* kW */
#define PM_REACTIVE_POWER_T_RID  27  /* kvar */
#define PM_APPARENT_POWER_T_RID  30  /* kVA */
#define PM_POWER_FACTOR_T_RID    31  /* -1..1 */

/* Totals */
#define PM_3P_ACTIVE_POWER_RID   34  /* kW */
#define PM_3P_REACTIVE_POWER_RID 35  /* kvar */
#define PM_3P_APPARENT_POWER_RID 38  /* kVA */
#define PM_3P_POWER_FACTOR_RID   39  /* -1..1 */
#define PM_ACTIVE_ENERGY_RID     41  /* kWh */
#define PM_REACTIVE_ENERGY_RID   42  /* kvarh */
#define PM_APPARENT_ENERGY_RID   45  /* kVAh */
#define PM_FREQUENCY_RID         49  /* Hz */
#define PM_NEUTRAL_CURRENT_RID   50  /* A */

/* Number of resources we implement */
#define PM_NUM_FIELDS            31
/* Resource instances = fields minus exec resources (0 exec) */
#define PM_RES_INST_COUNT        31
#define PM_MAX_INSTANCES         1

/* String buffer sizes */
#define PM_STRING_MAX            32

#endif /* LWM2M_OBJ_POWER_METER_H_ */
