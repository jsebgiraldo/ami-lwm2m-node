/*
 * AMI LwM2M Node — Thread + LwM2M on XIAO ESP32-C6
 *
 * LwM2M client that registers with a Leshan server via
 * Thread mesh network (OpenThread). Reports simulated
 * sensor data (voltage, current, temperature, energy).
 *
 * Flow:
 * 1. OpenThread joins the Thread network (credentials in prj.conf)
 * 2. Wait for L4 connectivity (IPv6 up via Thread)
 * 3. Register LwM2M client with Leshan server
 * 4. Periodically update IPSO sensor objects
 *
 * Aligned with working Windows build — single file, no MCUboot,
 * no dataset injection, no FOTA.
 */

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/net/lwm2m.h>
#include <zephyr/net/net_if.h>
#include <zephyr/random/random.h>
#include <openthread.h>
#include <openthread/thread.h>
#include <openthread/instance.h>

#include "lwm2m_obj_power_meter.h"
#include "lwm2m_observation.h"

/* Firmware update (Object 5) */
extern void init_firmware_update(void);

LOG_MODULE_REGISTER(ami_lwm2m, LOG_LEVEL_INF);

/* ---- Configuration ---- */
#define CLIENT_MANUFACTURER     "Tesis-AMI"
#define CLIENT_MODEL_NUMBER     "XIAO-ESP32-C6"
#define CLIENT_SERIAL_NUMBER    "AMI-001"
#define CLIENT_FIRMWARE_VER     "0.9.0"
#define CLIENT_HW_VER           "1.0"

/* Endpoint name built at runtime from MAC — e.g. "ami-esp32c6-2434" */
static char endpoint_name[32];

/* LwM2M Server URI — Leshan on OTBR mesh-local address */
#define LWM2M_SERVER_URI        "coap://[" CONFIG_NET_CONFIG_PEER_IPV6_ADDR "]:5683"

/* Sensor update interval */
#define SENSOR_UPDATE_INTERVAL  K_SECONDS(30)

/* LED */
static const struct gpio_dt_spec led0 =
	GPIO_DT_SPEC_GET_OR(DT_ALIAS(led0), gpios, {0});

/* ---- Simulated 3-Phase sensor accumulator ---- */
static double energy_kwh = 0.0;

/* ---- LwM2M context ---- */
static struct lwm2m_ctx client_ctx;
static bool lwm2m_connected;

/* ---- LwM2M callbacks ---- */
static int device_reboot_cb(uint16_t obj_inst_id,
			    uint8_t *args, uint16_t args_len)
{
	LOG_INF("DEVICE: Reboot requested");
	return 0;
}

static void rd_client_event(struct lwm2m_ctx *client,
			    enum lwm2m_rd_client_event client_event)
{
	switch (client_event) {
	case LWM2M_RD_CLIENT_EVENT_NONE:
		break;
	case LWM2M_RD_CLIENT_EVENT_REGISTRATION_COMPLETE:
		LOG_INF("LwM2M Registration complete!");
		lwm2m_connected = true;
		if (gpio_is_ready_dt(&led0)) {
			gpio_pin_set_dt(&led0, 1);
		}
		break;
	case LWM2M_RD_CLIENT_EVENT_REGISTRATION_FAILURE:
		LOG_ERR("LwM2M Registration FAILED");
		lwm2m_connected = false;
		break;
	case LWM2M_RD_CLIENT_EVENT_REG_TIMEOUT:
		LOG_WRN("LwM2M Registration timeout");
		lwm2m_connected = false;
		break;
	case LWM2M_RD_CLIENT_EVENT_REG_UPDATE_COMPLETE:
		LOG_DBG("LwM2M Registration update complete");
		break;
	case LWM2M_RD_CLIENT_EVENT_DISCONNECT:
		LOG_WRN("LwM2M Disconnected");
		lwm2m_connected = false;
		if (gpio_is_ready_dt(&led0)) {
			gpio_pin_set_dt(&led0, 0);
		}
		break;
	case LWM2M_RD_CLIENT_EVENT_NETWORK_ERROR:
		LOG_ERR("LwM2M network error — will retry");
		lwm2m_connected = false;
		break;
	default:
		LOG_DBG("LwM2M event: %d", client_event);
		break;
	}
}

static void observe_cb(enum lwm2m_observe_event event,
		       struct lwm2m_obj_path *path, void *user_data)
{
	switch (event) {
	case LWM2M_OBSERVE_EVENT_OBSERVER_ADDED:
		LOG_INF("Observe started: /%u/%u/%u",
			path->obj_id, path->obj_inst_id, path->res_id);
		break;
	case LWM2M_OBSERVE_EVENT_OBSERVER_REMOVED:
		LOG_INF("Observe stopped: /%u/%u/%u",
			path->obj_id, path->obj_inst_id, path->res_id);
		break;
	case LWM2M_OBSERVE_EVENT_NOTIFY_ACK:
		LOG_DBG("Notify ACK: /%u/%u/%u",
			path->obj_id, path->obj_inst_id, path->res_id);
		break;
	default:
		break;
	}
}

/* ---- LwM2M object setup ---- */
static int lwm2m_setup(void)
{
	int ret;

	/* Security Object (0) */
	lwm2m_set_string(&LWM2M_OBJ(0, 0, 0), LWM2M_SERVER_URI);
	lwm2m_set_u8(&LWM2M_OBJ(0, 0, 2), 3); /* NoSec mode */
	lwm2m_set_u16(&LWM2M_OBJ(0, 0, 10), 101); /* Short Server ID */

	/* Server Object (1) */
	lwm2m_set_u16(&LWM2M_OBJ(1, 0, 0), 101); /* Short Server ID */
	lwm2m_set_u32(&LWM2M_OBJ(1, 0, 1), 300); /* Lifetime = 300s */

	/* Device Object (3) */
	lwm2m_set_res_buf(&LWM2M_OBJ(3, 0, 0),
			  CLIENT_MANUFACTURER, sizeof(CLIENT_MANUFACTURER),
			  sizeof(CLIENT_MANUFACTURER), LWM2M_RES_DATA_FLAG_RO);
	lwm2m_set_res_buf(&LWM2M_OBJ(3, 0, 1),
			  CLIENT_MODEL_NUMBER, sizeof(CLIENT_MODEL_NUMBER),
			  sizeof(CLIENT_MODEL_NUMBER), LWM2M_RES_DATA_FLAG_RO);
	lwm2m_set_res_buf(&LWM2M_OBJ(3, 0, 2),
			  CLIENT_SERIAL_NUMBER, sizeof(CLIENT_SERIAL_NUMBER),
			  sizeof(CLIENT_SERIAL_NUMBER), LWM2M_RES_DATA_FLAG_RO);
	lwm2m_set_res_buf(&LWM2M_OBJ(3, 0, 3),
			  CLIENT_FIRMWARE_VER, sizeof(CLIENT_FIRMWARE_VER),
			  sizeof(CLIENT_FIRMWARE_VER), LWM2M_RES_DATA_FLAG_RO);
	lwm2m_register_exec_callback(&LWM2M_OBJ(3, 0, 4), device_reboot_cb);
	lwm2m_set_res_buf(&LWM2M_OBJ(3, 0, 17),
			  CONFIG_BOARD, sizeof(CONFIG_BOARD),
			  sizeof(CONFIG_BOARD), LWM2M_RES_DATA_FLAG_RO);
	lwm2m_set_res_buf(&LWM2M_OBJ(3, 0, 18),
			  CLIENT_HW_VER, sizeof(CLIENT_HW_VER),
			  sizeof(CLIENT_HW_VER), LWM2M_RES_DATA_FLAG_RO);

	/* Create 3-Phase Power Meter instance (10242/0) */
	ret = lwm2m_create_object_inst(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0));
	if (ret < 0) {
		LOG_ERR("Failed to create Power Meter inst: %d", ret);
	}

	/* Initialize firmware update callbacks (Object 5) */
	init_firmware_update();

	LOG_INF("LwM2M objects configured");
	LOG_INF("  Server: %s", LWM2M_SERVER_URI);
	LOG_INF("  Endpoint: %s", endpoint_name);
	return 0;
}

/* ---- Simulated 3-Phase sensor update ---- */
static void update_sensors(void)
{
	/* Simulate realistic 3-phase measurements */
	double v_r = 118.0 + (sys_rand32_get() % 60) / 10.0;  /* 118-124V */
	double v_s = 118.0 + (sys_rand32_get() % 60) / 10.0;
	double v_t = 118.0 + (sys_rand32_get() % 60) / 10.0;

	double i_r = 4.0 + (sys_rand32_get() % 30) / 10.0;    /* 4.0-7.0A */
	double i_s = 3.5 + (sys_rand32_get() % 30) / 10.0;
	double i_t = 3.0 + (sys_rand32_get() % 30) / 10.0;

	double pf = 0.85 + (sys_rand32_get() % 10) / 100.0;   /* 0.85-0.95 */
	double freq = 59.9 + (sys_rand32_get() % 20) / 100.0; /* 59.9-60.1 Hz */

	/* Active power per phase: P = V * I * PF (kW) */
	double p_r = v_r * i_r * pf / 1000.0;
	double p_s = v_s * i_s * pf / 1000.0;
	double p_t = v_t * i_t * pf / 1000.0;

	/* Reactive power: Q = V * I * sin(acos(PF)) (kvar) */
	double sin_phi = 0.527;  /* approx sin(acos(0.85)) */
	double q_r = v_r * i_r * sin_phi / 1000.0;
	double q_s = v_s * i_s * sin_phi / 1000.0;
	double q_t = v_t * i_t * sin_phi / 1000.0;

	/* Apparent power: S = V * I (kVA) */
	double s_r = v_r * i_r / 1000.0;
	double s_s = v_s * i_s / 1000.0;
	double s_t = v_t * i_t / 1000.0;

	/* Totals */
	double p_total = p_r + p_s + p_t;
	double q_total = q_r + q_s + q_t;
	double s_total = s_r + s_s + s_t;

	/* Energy accumulator */
	energy_kwh += p_total * (30.0 / 3600.0);  /* 30s interval */

	/* Neutral current (vector sum, simplified) */
	double i_n = (i_r - i_s) * 0.3;  /* Simplified unbalance */
	if (i_n < 0) { i_n = -i_n; }

	/* ---- Set all Object 10242 resources ---- */

	/* Phase R */
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_TENSION_R_RID), v_r);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_CURRENT_R_RID), i_r);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_ACTIVE_POWER_R_RID), p_r);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_REACTIVE_POWER_R_RID), q_r);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_APPARENT_POWER_R_RID), s_r);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_POWER_FACTOR_R_RID), pf);

	/* Phase S */
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_TENSION_S_RID), v_s);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_CURRENT_S_RID), i_s);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_ACTIVE_POWER_S_RID), p_s);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_REACTIVE_POWER_S_RID), q_s);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_APPARENT_POWER_S_RID), s_s);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_POWER_FACTOR_S_RID), pf);

	/* Phase T */
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_TENSION_T_RID), v_t);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_CURRENT_T_RID), i_t);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_ACTIVE_POWER_T_RID), p_t);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_REACTIVE_POWER_T_RID), q_t);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_APPARENT_POWER_T_RID), s_t);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_POWER_FACTOR_T_RID), pf);

	/* Totals */
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_3P_ACTIVE_POWER_RID), p_total);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_3P_REACTIVE_POWER_RID), q_total);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_3P_APPARENT_POWER_RID), s_total);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_3P_POWER_FACTOR_RID), pf);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_ACTIVE_ENERGY_RID), energy_kwh);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_REACTIVE_ENERGY_RID), q_total * (30.0 / 3600.0));
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_APPARENT_ENERGY_RID), s_total * (30.0 / 3600.0));
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_FREQUENCY_RID), freq);
	lwm2m_set_f64(&LWM2M_OBJ(POWER_METER_OBJECT_ID, 0, PM_NEUTRAL_CURRENT_RID), i_n);

	/* Notify observers for key resources */
	lwm2m_notify_observer(POWER_METER_OBJECT_ID, 0, PM_TENSION_R_RID);
	lwm2m_notify_observer(POWER_METER_OBJECT_ID, 0, PM_CURRENT_R_RID);
	lwm2m_notify_observer(POWER_METER_OBJECT_ID, 0, PM_TENSION_S_RID);
	lwm2m_notify_observer(POWER_METER_OBJECT_ID, 0, PM_CURRENT_S_RID);
	lwm2m_notify_observer(POWER_METER_OBJECT_ID, 0, PM_TENSION_T_RID);
	lwm2m_notify_observer(POWER_METER_OBJECT_ID, 0, PM_CURRENT_T_RID);
	lwm2m_notify_observer(POWER_METER_OBJECT_ID, 0, PM_3P_ACTIVE_POWER_RID);
	lwm2m_notify_observer(POWER_METER_OBJECT_ID, 0, PM_ACTIVE_ENERGY_RID);
	lwm2m_notify_observer(POWER_METER_OBJECT_ID, 0, PM_FREQUENCY_RID);

	LOG_INF("3P: R=%.1fV/%.1fA  S=%.1fV/%.1fA  T=%.1fV/%.1fA  "
		"P=%.2fkW  E=%.3fkWh  f=%.1fHz",
		v_r, i_r, v_s, i_s, v_t, i_t,
		p_total, energy_kwh, freq);
}

/* ---- Main ---- */
static void build_endpoint_name(void)
{
	struct net_if *iface = net_if_get_default();
	struct net_linkaddr *link = net_if_get_link_addr(iface);

	if (link && link->len >= 2) {
		snprintf(endpoint_name, sizeof(endpoint_name),
			 "ami-esp32c6-%02x%02x",
			 link->addr[link->len - 2],
			 link->addr[link->len - 1]);
	} else {
		snprintf(endpoint_name, sizeof(endpoint_name),
			 "ami-esp32c6-%04x", (uint16_t)sys_rand32_get());
	}
}

int main(void)
{
	int ret;

	LOG_INF("=== AMI LwM2M Node v%s ===", CLIENT_FIRMWARE_VER);
	LOG_INF("Board: %s", CONFIG_BOARD);
	LOG_INF("Network: Thread Ch%d PAN 0x%04X",
		CONFIG_OPENTHREAD_CHANNEL, CONFIG_OPENTHREAD_PANID);

	/* LED init */
	if (gpio_is_ready_dt(&led0)) {
		gpio_pin_configure_dt(&led0, GPIO_OUTPUT_INACTIVE);
	}

	/* Poll OpenThread role until attached (Child/Router/Leader) */
	LOG_INF("Waiting for Thread network...");
	for (int i = 0; i < 120; i++) {
		openthread_mutex_lock();
		struct otInstance *instance = openthread_get_default_instance();
		otDeviceRole role = OT_DEVICE_ROLE_DISABLED;
		if (instance) {
			role = otThreadGetDeviceRole(instance);
		}
		openthread_mutex_unlock();

		if (role >= OT_DEVICE_ROLE_CHILD) {
			LOG_INF("Thread attached! Role=%d after %ds",
				(int)role, i * 2);
			break;
		}
		if (gpio_is_ready_dt(&led0)) {
			gpio_pin_toggle_dt(&led0);
		}
		k_sleep(K_SECONDS(2));
	}

	/* Extra wait for IPv6 address propagation */
	LOG_INF("Extra 5s wait for IPv6 addresses...");
	k_sleep(K_SECONDS(5));

	/* Build unique endpoint name from MAC */
	build_endpoint_name();
	LOG_INF("Endpoint: %s", endpoint_name);

	/* Setup LwM2M objects */
	ret = lwm2m_setup();
	if (ret < 0) {
		LOG_ERR("LwM2M setup failed: %d", ret);
		return ret;
	}

	/* Start LwM2M RD client */
	memset(&client_ctx, 0, sizeof(client_ctx));
	lwm2m_rd_client_start(&client_ctx, endpoint_name, 0,
			      rd_client_event, observe_cb);

	/* Main loop — update sensors periodically */
	LOG_INF("Entering sensor loop (every 30 seconds)");

	while (1) {
		k_sleep(SENSOR_UPDATE_INTERVAL);
		update_sensors();
	}

	return 0;
}
