/*
 * AMI LwM2M Node — Thread + LwM2M on XIAO ESP32-C6
 *
 * LwM2M client that registers with ThingsBoard Edge via
 * Thread mesh network (OpenThread). Reports DLMS meter
 * data (voltage, current, power, energy, frequency).
 *
 * Flow:
 * 1. OpenThread joins the Thread network (credentials in prj.conf)
 * 2. Wait for L4 connectivity (IPv6 up via Thread)
 * 3. Register LwM2M client with ThingsBoard Edge (built-in LwM2M transport)
 * 4. Periodically poll DLMS meter and push via LwM2M objects
 *
 * Aligned with working Windows build — single file, no MCUboot,
 * no dataset injection, no FOTA.
 */

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/logging/log_ctrl.h>
#include <zephyr/sys/reboot.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/net/lwm2m.h>
#include <zephyr/net/net_if.h>
#include <zephyr/random/random.h>
#include <zephyr/shell/shell.h>
#include <zephyr/init.h>
#include <zephyr/sys/atomic.h>
#include <openthread.h>
#include <openthread/thread.h>
#include <openthread/instance.h>
#include <openthread/dataset.h>
#include <openthread/ip6.h>
#include <openthread/platform/radio.h>

#include "lwm2m_obj_power_meter.h"
#include "lwm2m_obj_thread_diag.h"
#include "lwm2m_obj_thread_net.h"
#include "lwm2m_obj_thread_neighbor.h"
#include "lwm2m_obj_thread_commission.h"
#include "lwm2m_obj_thread_cli.h"
#include "lwm2m_observation.h"
#include "dlms_meter.h"

/* Firmware update (Object 5) */
extern void init_firmware_update(void);

/* Thread connectivity monitoring (Objects 4 + 33000) */
extern void init_connmon_thread(void);
extern void init_thread_diag_object(void);
extern void update_connectivity_metrics(void);

LOG_MODULE_REGISTER(ami_lwm2m, LOG_LEVEL_INF);

/* Early SYS_INIT to confirm kernel is running before main() */
extern int esp_rom_printf(const char *fmt, ...);

/* PRE_KERNEL_1: fires before any driver init — absolute earliest */
static int boot_pre_kernel(void)
{
	esp_rom_printf("\r\n[AMI] PRE_KERNEL_1 OK\r\n");
	return 0;
}
SYS_INIT(boot_pre_kernel, PRE_KERNEL_1, 0);

/* POST_KERNEL: fires after drivers are up */
static int boot_post_kernel(void)
{
	esp_rom_printf("[AMI] POST_KERNEL OK\r\n");
	return 0;
}
SYS_INIT(boot_post_kernel, POST_KERNEL, 0);

/* APPLICATION: fires just before main() */
static int boot_application(void)
{
	esp_rom_printf("[AMI] APPLICATION init OK\r\n");
	return 0;
}
SYS_INIT(boot_application, APPLICATION, 0);

/* ---- Configuration ---- */
#define CLIENT_MANUFACTURER     "Tesis-AMI"
#define CLIENT_MODEL_NUMBER     "XIAO-ESP32-C6"
#define CLIENT_SERIAL_NUMBER    "AMI-001"
#define CLIENT_FIRMWARE_VER     "0.16.0"
#define CLIENT_HW_VER           "1.0"

/* Endpoint name built at runtime from MAC — e.g. "ami-esp32c6-2434" */
static char endpoint_name[32];

/* LwM2M server URI runtime selection (primary + secondary fallback). */
static char lwm2m_server_uri_primary[96];
static char lwm2m_server_uri_secondary[96];
static const char *lwm2m_server_uris[2];
static int lwm2m_server_index;
static uint8_t lwm2m_reg_failures;

/* Sensor update intervals */
#define DLMS_POLL_INTERVAL_DEFAULT  15   /* seconds — default DLMS meter poll */
#define CONN_UPDATE_INTERVAL_S     60   /* seconds — RSSI/LQI/Thread update (v0.18.0) */
#define LOOP_TICK              K_MSEC(500)     /* Main loop tick */

/* Runtime-configurable DLMS poll interval (seconds).
 * Changed via shell: "dlms_interval <s>" e.g. 10 for 10 seconds.
 * Default 15s (down from original 30s — DLMS poll dominates latency).
 */
static int dlms_poll_interval_s = DLMS_POLL_INTERVAL_DEFAULT;

/* v0.15.0: notify_interval removed — notifications are now
 * synchronized with the DLMS poll cycle via threshold-based
 * smart notification in meter_push_to_lwm2m().
 */

/* Track last DLMS poll time */
static int64_t last_dlms_poll_ms;
static const struct gpio_dt_spec led0 =
	GPIO_DT_SPEC_GET_OR(DT_ALIAS(led0), gpios, {0});

/* ---- DLMS Meter readings ---- */
static struct meter_readings last_readings;
static bool meter_initialized;
static int64_t demo_last_update_ms;
static double demo_energy_kwh = 61100.0;

/* Forward declarations */
static void update_sensors_fallback(void);
static void fill_demo_readings(struct meter_readings *r);
static const char *active_lwm2m_server_uri(void);
static void rd_client_event(struct lwm2m_ctx *client,
			enum lwm2m_rd_client_event client_event);
static void observe_cb(enum lwm2m_observe_event event,
		      struct lwm2m_obj_path *path, void *user_data);

/* ---- LwM2M context ---- */
static struct lwm2m_ctx client_ctx;
static bool lwm2m_connected;
static atomic_t lwm2m_recovering;

static void init_lwm2m_server_uris(void)
{
	snprintk(lwm2m_server_uri_primary, sizeof(lwm2m_server_uri_primary),
		 "coap://[%s]:5683", CONFIG_AMI_LWM2M_SERVER_IPV6_PRIMARY);
	snprintk(lwm2m_server_uri_secondary, sizeof(lwm2m_server_uri_secondary),
		 "coap://[%s]:5683", CONFIG_AMI_LWM2M_SERVER_IPV6_SECONDARY);

	lwm2m_server_uris[0] = lwm2m_server_uri_primary;
	if (strlen(CONFIG_AMI_LWM2M_SERVER_IPV6_SECONDARY) > 0) {
		lwm2m_server_uris[1] = lwm2m_server_uri_secondary;
	} else {
		lwm2m_server_uris[1] = lwm2m_server_uri_primary;
	}
	lwm2m_server_index = 0;
}

static const char *active_lwm2m_server_uri(void)
{
	return lwm2m_server_uris[lwm2m_server_index];
}

static void maybe_switch_lwm2m_server(void)
{
	if (lwm2m_server_uris[0] == lwm2m_server_uris[1]) {
		return;
	}

	if (lwm2m_reg_failures < 2) {
		return;
	}

	lwm2m_server_index ^= 1;
	lwm2m_reg_failures = 0;
	lwm2m_set_string(&LWM2M_OBJ(0, 0, 0), active_lwm2m_server_uri());
	LOG_WRN("LwM2M server failover -> %s", active_lwm2m_server_uri());
}

static void lwm2m_recover_work_fn(struct k_work *w)
{
	ARG_UNUSED(w);

	atomic_set(&lwm2m_recovering, 1);
	LOG_WRN("LwM2M recovery: restarting RD client (server=%s)",
		active_lwm2m_server_uri());

	/* Force-close current session, then start clean registration. */
	(void)lwm2m_rd_client_stop(&client_ctx, rd_client_event, false);
	memset(&client_ctx, 0, sizeof(client_ctx));

	int ret = lwm2m_rd_client_start(&client_ctx, endpoint_name, 0,
				      rd_client_event, observe_cb);
	if (ret == 0 || ret == -EINPROGRESS) {
		LOG_INF("LwM2M recovery: RD client restart requested");
	} else {
		LOG_ERR("LwM2M recovery: RD start failed (%d)", ret);
	}

	atomic_set(&lwm2m_recovering, 0);
}

static K_WORK_DELAYABLE_DEFINE(lwm2m_recover_work, lwm2m_recover_work_fn);

/* ---- LwM2M callbacks ---- */
static int device_reboot_cb(uint16_t obj_inst_id,
			    uint8_t *args, uint16_t args_len)
{
	LOG_INF("DEVICE: Reboot requested");
	k_sleep(K_MSEC(100));
	sys_reboot(SYS_REBOOT_COLD);
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
		lwm2m_reg_failures = 0;
		k_work_cancel_delayable(&lwm2m_recover_work);
		if (gpio_is_ready_dt(&led0)) {
			gpio_pin_set_dt(&led0, 1);
		}
		break;
	case LWM2M_RD_CLIENT_EVENT_REGISTRATION_FAILURE:
		LOG_ERR("LwM2M Registration FAILED");
		lwm2m_connected = false;
		lwm2m_reg_failures++;
		maybe_switch_lwm2m_server();
		if (!atomic_get(&lwm2m_recovering)) {
			k_work_reschedule(&lwm2m_recover_work, K_SECONDS(10));
		}
		break;
	case LWM2M_RD_CLIENT_EVENT_REG_TIMEOUT:
		LOG_WRN("LwM2M Registration timeout");
		lwm2m_connected = false;
		lwm2m_reg_failures++;
		maybe_switch_lwm2m_server();
		if (!atomic_get(&lwm2m_recovering)) {
			k_work_reschedule(&lwm2m_recover_work, K_SECONDS(10));
		}
		break;
	case LWM2M_RD_CLIENT_EVENT_REG_UPDATE_COMPLETE:
		LOG_DBG("LwM2M Registration update complete");
		break;
	case LWM2M_RD_CLIENT_EVENT_DISCONNECT:
		LOG_WRN("LwM2M Disconnected");
		lwm2m_connected = false;
		if (!atomic_get(&lwm2m_recovering)) {
			k_work_reschedule(&lwm2m_recover_work, K_SECONDS(10));
		}
		if (gpio_is_ready_dt(&led0)) {
			gpio_pin_set_dt(&led0, 0);
		}
		break;
	case LWM2M_RD_CLIENT_EVENT_NETWORK_ERROR:
		LOG_ERR("LwM2M network error — will retry");
		lwm2m_connected = false;
		lwm2m_reg_failures++;
		maybe_switch_lwm2m_server();
		if (!atomic_get(&lwm2m_recovering)) {
			k_work_reschedule(&lwm2m_recover_work, K_SECONDS(10));
		}
		break;
	default:
		LOG_DBG("LwM2M event: %d", client_event);
		break;
	}
}

/* Observe summary: one LOG_INF after 500 ms of quiet instead of one line per resource */
static int observe_total;

static void observe_summary_work_fn(struct k_work *w)
{
	LOG_INF("Observe: %d resource(s) active", observe_total);
}

static K_WORK_DELAYABLE_DEFINE(observe_summary_work, observe_summary_work_fn);

static void observe_cb(enum lwm2m_observe_event event,
		       struct lwm2m_obj_path *path, void *user_data)
{
	switch (event) {
	case LWM2M_OBSERVE_EVENT_OBSERVER_ADDED:
		observe_total++;
		LOG_DBG("Observe started: /%u/%u/%u (%d active)",
			path->obj_id, path->obj_inst_id, path->res_id,
			observe_total);
		k_work_reschedule(&observe_summary_work, K_MSEC(500));
		break;
	case LWM2M_OBSERVE_EVENT_OBSERVER_REMOVED:
		if (observe_total > 0) {
			observe_total--;
		}
		LOG_DBG("Observe stopped: /%u/%u/%u (%d active)",
			path->obj_id, path->obj_inst_id, path->res_id,
			observe_total);
		k_work_reschedule(&observe_summary_work, K_MSEC(500));
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
	lwm2m_set_string(&LWM2M_OBJ(0, 0, 0), active_lwm2m_server_uri());
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

	/* Initialize Thread connectivity monitoring */
	init_thread_diag_object();
	init_connmon_thread();

	/* Initialize standard Thread objects (OMA 10483-10486) */
	init_thread_net_object();
	init_thread_neighbor_object();
	init_thread_commission_object();
	init_thread_cli_object();

	LOG_INF("LwM2M objects configured");
	LOG_INF("  Server(primary):   %s", lwm2m_server_uris[0]);
	LOG_INF("  Server(secondary): %s", lwm2m_server_uris[1]);
	LOG_INF("  Server(active):    %s", active_lwm2m_server_uri());
	return 0;
}

/* v0.15.0: notify_interval shell command removed.
 * Notifications are now threshold-based, synchronized with DLMS poll.
 */

/* ---- Shell command: set DLMS poll interval ---- */
static int cmd_dlms_interval(const struct shell *sh, size_t argc, char **argv)
{
	int s = atoi(argv[1]);

	if (s < 5) {
		shell_error(sh, "interval must be >= 5 seconds");
		return -EINVAL;
	}
	if (s > 300) {
		shell_error(sh, "interval must be <= 300 seconds");
		return -EINVAL;
	}
	dlms_poll_interval_s = s;
	shell_print(sh, "dlms_interval set to %d seconds", s);
	LOG_INF("dlms_interval changed to %d s", s);
	return 0;
}
SHELL_CMD_ARG_REGISTER(dlms_interval, NULL,
		       "Set DLMS meter poll interval in seconds (5-300, default 15)",
		       cmd_dlms_interval, 2, 0);

/*
 * v0.17.0: Consecutive meter failure tracking.
 * After MAX_CONSEC_FAILURES, suppress data to avoid pushing stale values.
 */
#define MAX_CONSEC_FAILURES  5
static int consecutive_meter_failures;

/* ---- Dedicated DLMS poll thread — semaphore and running flag ---- */
static K_SEM_DEFINE(dlms_poll_sem, 0, 1);
static volatile bool dlms_thread_running;

/* ================================================================
 * ami test commands
 * Usage:
 *   ami status          — overall node status
 *   ami test thread     — Thread network connectivity
 *   ami test lwm2m      — LwM2M registration
 *   ami test dlms       — trigger DLMS poll and report readings
 *   ami test all        — run all tests
 * ================================================================ */

static int cmd_ami_status(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	int64_t uptime_ms = k_uptime_get();
	int uptime_s = (int)(uptime_ms / 1000);

	shell_print(sh, "=== AMI Node Status (uptime %ds) ===", uptime_s);

	/* Thread role */
	openthread_mutex_lock();
	struct otInstance *ot = openthread_get_default_instance();
	otDeviceRole role = ot ? otThreadGetDeviceRole(ot) : OT_DEVICE_ROLE_DISABLED;
	int8_t rssi = ot ? otPlatRadioGetRssi(ot) : -127;
	openthread_mutex_unlock();

	static const char * const role_str[] = {
		"DISABLED", "DETACHED", "CHILD", "ROUTER", "LEADER"
	};
	bool thread_ok = (role >= OT_DEVICE_ROLE_CHILD);

	shell_print(sh, "  Thread : %s  role=%s  RSSI=%ddBm",
		    thread_ok ? "OK" : "FAIL",
		    role < ARRAY_SIZE(role_str) ? role_str[role] : "?",
		    rssi);

	/* LwM2M */
	shell_print(sh, "  LwM2M  : %s", lwm2m_connected ? "OK  (registered)" : "FAIL (not registered)");
	shell_print(sh, "    server=%s", active_lwm2m_server_uri());

	/* DLMS */
	shell_print(sh, "  DLMS   : %s  failures=%d  poll_interval=%ds",
		    meter_initialized ? "OK" : "INIT_PENDING",
		    consecutive_meter_failures,
		    dlms_poll_interval_s);

	if (meter_initialized && consecutive_meter_failures == 0) {
		shell_print(sh, "    Vr=%.2fV  Ir=%.3fA  Ptot=%.3fkW  E=%.3fkWh  f=%.2fHz",
			    last_readings.voltage_r,
			    last_readings.current_r,
			    last_readings.total_active_power,
			    last_readings.active_energy,
			    last_readings.frequency);
	}

	shell_print(sh, "  Result : %s",
		    (thread_ok && lwm2m_connected) ? "ALL OK" :
		    (!thread_ok && !lwm2m_connected) ? "THREAD+LWM2M DOWN" :
		    !thread_ok ? "THREAD DOWN" : "LWM2M DOWN");
	return 0;
}

static int cmd_ami_test_thread(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	shell_print(sh, "[TEST] Thread network...");

	openthread_mutex_lock();
	struct otInstance *ot = openthread_get_default_instance();

	if (!ot) {
		openthread_mutex_unlock();
		shell_error(sh, "FAIL: OpenThread instance not available");
		return -EIO;
	}

	otDeviceRole role = otThreadGetDeviceRole(ot);
	int8_t rssi = otPlatRadioGetRssi(ot);
	uint16_t rloc = otThreadGetRloc16(ot);
	uint8_t chan = otLinkGetChannel(ot);

	/* Count neighbors */
	int neighbor_count = 0;
	otNeighborInfoIterator iter = OT_NEIGHBOR_INFO_ITERATOR_INIT;
	otNeighborInfo info;

	while (otThreadGetNextNeighborInfo(ot, &iter, &info) == OT_ERROR_NONE) {
		neighbor_count++;
	}

	openthread_mutex_unlock();

	static const char * const role_str[] = {
		"DISABLED", "DETACHED", "CHILD", "ROUTER", "LEADER"
	};

	shell_print(sh, "  role      : %s",
		    role < ARRAY_SIZE(role_str) ? role_str[role] : "?");
	shell_print(sh, "  RLOC16    : 0x%04x", rloc);
	shell_print(sh, "  channel   : %u", chan);
	shell_print(sh, "  RSSI      : %d dBm", rssi);
	shell_print(sh, "  neighbors : %d", neighbor_count);

	if (role >= OT_DEVICE_ROLE_CHILD) {
		shell_print(sh, "  [PASS] Thread attached");
		return 0;
	} else {
		shell_error(sh, "  [FAIL] Thread not attached (role=%d)", (int)role);
		return -ENETDOWN;
	}
}

static int cmd_ami_test_lwm2m(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	shell_print(sh, "[TEST] LwM2M registration...");

	if (lwm2m_connected) {
		shell_print(sh, "  endpoint  : %s", endpoint_name);
		shell_print(sh, "  server    : %s", active_lwm2m_server_uri());
		shell_print(sh, "  [PASS] LwM2M registered");
		return 0;
	} else {
		shell_error(sh, "  [FAIL] LwM2M not registered");
		return -ENOTCONN;
	}
}

static int cmd_ami_test_dlms(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc);
	ARG_UNUSED(argv);

	shell_print(sh, "[TEST] DLMS meter poll (max 15s)...");

	/* Trigger a poll if the thread is idle */
	if (dlms_thread_running) {
		shell_print(sh, "  DLMS poll already in progress, waiting...");
	} else {
		k_sem_give(&dlms_poll_sem);
	}

	/* Wait up to 15s for the poll to complete */
	int waited = 0;

	while (dlms_thread_running && waited < 150) {
		k_sleep(K_MSEC(100));
		waited++;
	}

	if (waited >= 150) {
		shell_error(sh, "  [FAIL] DLMS poll timed out after 15s");
		return -ETIMEDOUT;
	}

	if (consecutive_meter_failures > 0) {
		shell_error(sh, "  [FAIL] meter poll failed (%d consecutive failures)",
			    consecutive_meter_failures);
		return -EIO;
	}

	shell_print(sh, "  voltage_r     : %.2f V",   last_readings.voltage_r);
	shell_print(sh, "  current_r     : %.3f A",   last_readings.current_r);
	shell_print(sh, "  total_power   : %.3f kW",  last_readings.total_active_power);
	shell_print(sh, "  active_energy : %.3f kWh", last_readings.active_energy);
	shell_print(sh, "  frequency     : %.2f Hz",  last_readings.frequency);
	shell_print(sh, "  [PASS] DLMS meter reachable");
	return 0;
}

static int cmd_ami_test_all(const struct shell *sh, size_t argc, char **argv)
{
	int ret, overall = 0;

	shell_print(sh, "=== AMI full test ===");

	ret = cmd_ami_test_thread(sh, argc, argv);
	if (ret) {
		overall = ret;
	}

	ret = cmd_ami_test_lwm2m(sh, argc, argv);
	if (ret) {
		overall = ret;
	}

	ret = cmd_ami_test_dlms(sh, argc, argv);
	if (ret) {
		overall = ret;
	}

	shell_print(sh, "=== %s ===", overall == 0 ? "ALL PASS" : "SOME FAILURES");
	return overall;
}

SHELL_STATIC_SUBCMD_SET_CREATE(ami_test_cmds,
	SHELL_CMD(thread, NULL, "Test Thread network attachment", cmd_ami_test_thread),
	SHELL_CMD(lwm2m,  NULL, "Test LwM2M server registration", cmd_ami_test_lwm2m),
	SHELL_CMD(dlms,   NULL, "Trigger DLMS poll and report readings", cmd_ami_test_dlms),
	SHELL_CMD(all,    NULL, "Run all tests", cmd_ami_test_all),
	SHELL_SUBCMD_SET_END
);

/* ================================================================
 * ami log commands — runtime DLMS/RS485 log verbosity control
 *   ami log quiet    — suppress all DLMS modules to WRN (no INF)
 *   ami log verbose  — enable DBG for all DLMS modules
 *   ami log meter    — enable dlms_meter DBG only
 *   ami log cosem    — enable dlms_cosem DBG only
 *   ami log hdlc     — enable dlms_hdlc DBG only
 *   ami log rs485    — enable rs485 DBG only
 * ================================================================
 */
static const char * const dlms_dbg_modules[] = {
	"rs485", "dlms_hdlc", "dlms_cosem", "dlms_meter"
};

static const char * const lwm2m_dbg_modules[] = {
	"net_lwm2m_rd_client", "net_lwm2m_engine", "net_lwm2m_registry"
};

static void set_dlms_log_level(uint32_t level)
{
	for (int m = 0; m < ARRAY_SIZE(dlms_dbg_modules); m++) {
		int32_t src_id = log_source_id_get(dlms_dbg_modules[m]);

		if (src_id < 0) {
			continue;
		}
		for (uint32_t b = 0; b < log_backend_count_get(); b++) {
			log_filter_set(log_backend_get(b), 0,
				       (uint32_t)src_id, level);
		}
	}
}

/*
 * Suppress net_lwm2m_registry ERR noise at startup.
 *
 * When the device re-registers, Leshan restores observations stored from the
 * previous session.  If object/resource instance counts changed (e.g. fewer
 * Thread neighbours), the LwM2M engine logs harmless "res instance X not found"
 * ERRs while the server re-syncs.  We suppress the module for 25 s — well past
 * the observe-storm window (~12 s) — then restore it to INF so real errors
 * remain visible during normal operation.
 */
static void restore_lwm2m_registry_fn(struct k_work *w)
{
	int32_t src_id = log_source_id_get("net_lwm2m_registry");

	if (src_id < 0) {
		return;
	}
	for (uint32_t b = 0; b < log_backend_count_get(); b++) {
		log_filter_set(log_backend_get(b), 0,
			       (uint32_t)src_id, LOG_LEVEL_INF);
	}
	LOG_DBG("net_lwm2m_registry log restored to INF");
}

static K_WORK_DELAYABLE_DEFINE(restore_lwm2m_registry_work,
			       restore_lwm2m_registry_fn);

static void suppress_lwm2m_registry_startup_noise(void)
{
	int32_t src_id = log_source_id_get("net_lwm2m_registry");

	if (src_id < 0) {
		return; /* module not found — nothing to suppress */
	}
	for (uint32_t b = 0; b < log_backend_count_get(); b++) {
		log_filter_set(log_backend_get(b), 0,
			       (uint32_t)src_id, LOG_LEVEL_NONE);
	}
	k_work_schedule(&restore_lwm2m_registry_work, K_SECONDS(25));
}

static int cmd_ami_log_quiet(const struct shell *sh, size_t argc, char **argv)
{
	set_dlms_log_level(LOG_LEVEL_WRN);
	shell_print(sh, "DLMS/RS485 log: WRN (quiet) — all INF suppressed. Use 'ami log verbose' or 'ami log <module>' to re-enable");
	return 0;
}

static int cmd_ami_log_verbose(const struct shell *sh, size_t argc, char **argv)
{
	set_dlms_log_level(LOG_LEVEL_DBG);
	shell_print(sh, "DLMS/RS485 log: DBG (verbose) — use 'ami log quiet' to suppress all");
	return 0;
}

static int set_single_module_dbg(const struct shell *sh, const char *module)
{
	int32_t src_id = log_source_id_get(module);

	if (src_id < 0) {
		shell_error(sh, "Module '%s' not found", module);
		return -ENOENT;
	}
	for (uint32_t b = 0; b < log_backend_count_get(); b++) {
		log_filter_set(log_backend_get(b), 0, (uint32_t)src_id, LOG_LEVEL_DBG);
	}
	shell_print(sh, "%s: DBG enabled — use 'ami log quiet' to suppress all", module);
	return 0;
}

static int cmd_ami_log_meter(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc); ARG_UNUSED(argv);
	return set_single_module_dbg(sh, "dlms_meter");
}

static int cmd_ami_log_cosem(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc); ARG_UNUSED(argv);
	return set_single_module_dbg(sh, "dlms_cosem");
}

static int cmd_ami_log_hdlc(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc); ARG_UNUSED(argv);
	return set_single_module_dbg(sh, "dlms_hdlc");
}

static int cmd_ami_log_rs485(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc); ARG_UNUSED(argv);
	return set_single_module_dbg(sh, "rs485");
}

static int cmd_ami_log_lwm2m(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc); ARG_UNUSED(argv);

	for (int m = 0; m < ARRAY_SIZE(lwm2m_dbg_modules); m++) {
		int32_t src_id = log_source_id_get(lwm2m_dbg_modules[m]);

		if (src_id < 0) {
			continue;
		}
		for (uint32_t b = 0; b < log_backend_count_get(); b++) {
			log_filter_set(log_backend_get(b), 0,
				       (uint32_t)src_id, LOG_LEVEL_DBG);
		}
	}

	shell_print(sh, "LwM2M log: DBG enabled (rd_client/engine/registry)");
	return 0;
}

SHELL_STATIC_SUBCMD_SET_CREATE(ami_log_cmds,
	SHELL_CMD(quiet,   NULL, "Suppress all DLMS/RS485 logging (WRN, no INF)", cmd_ami_log_quiet),
	SHELL_CMD(verbose, NULL, "Enable all DLMS/RS485 debug logging (DBG)",     cmd_ami_log_verbose),
	SHELL_CMD(meter,   NULL, "Enable dlms_meter DBG only",                    cmd_ami_log_meter),
	SHELL_CMD(cosem,   NULL, "Enable dlms_cosem DBG only",                    cmd_ami_log_cosem),
	SHELL_CMD(hdlc,    NULL, "Enable dlms_hdlc DBG only",                     cmd_ami_log_hdlc),
	SHELL_CMD(rs485,   NULL, "Enable rs485 DBG only",                         cmd_ami_log_rs485),
	SHELL_CMD(lwm2m,   NULL, "Enable LwM2M debug logs (rd_client/engine/registry)", cmd_ami_log_lwm2m),
	SHELL_SUBCMD_SET_END
);

static int cmd_ami_reset(const struct shell *sh, size_t argc, char **argv)
{
	shell_print(sh, "Rebooting...");
	k_sleep(K_MSEC(100));
	sys_reboot(SYS_REBOOT_COLD);
	return 0;
}

static int cmd_ami_diag(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc); ARG_UNUSED(argv);

	shell_print(sh, "=== DLMS OBIS Diagnostics (polls=%u  T_avg=%lldms) ===",
		    meter_get_poll_count(), meter_get_avg_poll_duration_ms());

	size_t n = meter_get_obis_table_size();

	for (int i = 0; i < (int)n; i++) {
		uint32_t s, f, r, sk;

		meter_get_obis_diag(i, &s, &f, &r, &sk);
		const char *name = meter_get_obis_name(i);
		const char *st;

		if (meter_get_obis_user_skip(i)) {
			st = "USER";
		} else if (sk > 0 && s == 0) {
			st = "AUTO";
		} else if (f > 0) {
			st = "ERR ";
		} else {
			st = "OK  ";
		}

		shell_print(sh, "  [%2d] %-22s %s ok=%-4u fail=%-3u retry=%-3u skip=%u",
			    i, name, st, s, f, r, sk);
	}
	return 0;
}

/* ---- ami obis subcommands ---- */
static int cmd_ami_obis_list(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argc); ARG_UNUSED(argv);

	shell_print(sh, "  Idx  Name                     State");
	shell_print(sh, "  ---  -----------------------  ---------");
	size_t n = meter_get_obis_table_size();

	for (int i = 0; i < (int)n; i++) {
		const char *st;

		if (meter_get_obis_user_skip(i)) {
			st = "USER-SKIP";
		} else if (meter_get_obis_skip(i)) {
			st = "AUTO-SKIP";
		} else {
			st = "OK";
		}
		shell_print(sh, "  [%2d] %-23s  %s", i, meter_get_obis_name(i), st);
	}
	return 0;
}

static int cmd_ami_obis_skip(const struct shell *sh, size_t argc, char **argv)
{
	if (argc < 2) {
		shell_error(sh, "Usage: ami obis skip <index>");
		return -EINVAL;
	}

	char *end;
	long idx = strtol(argv[1], &end, 10);

	if (*end != '\0' || idx < 0 || (size_t)idx >= meter_get_obis_table_size()) {
		shell_error(sh, "Invalid index '%s' (0..%zu)", argv[1],
			    meter_get_obis_table_size() - 1);
		return -EINVAL;
	}

	int ret = meter_set_obis_user_skip((int)idx, true);

	if (ret == 0) {
		shell_print(sh, "OBIS [%ld] '%s' -> USER-SKIP", idx,
			    meter_get_obis_name((int)idx));
	}
	return ret;
}

static int cmd_ami_obis_enable(const struct shell *sh, size_t argc, char **argv)
{
	if (argc < 2) {
		shell_error(sh, "Usage: ami obis enable <index>");
		return -EINVAL;
	}

	char *end;
	long idx = strtol(argv[1], &end, 10);

	if (*end != '\0' || idx < 0 || (size_t)idx >= meter_get_obis_table_size()) {
		shell_error(sh, "Invalid index '%s' (0..%zu)", argv[1],
			    meter_get_obis_table_size() - 1);
		return -EINVAL;
	}

	int ret = meter_set_obis_user_skip((int)idx, false);

	if (ret == 0) {
		shell_print(sh, "OBIS [%ld] '%s' -> OK (auto-skip also cleared)", idx,
			    meter_get_obis_name((int)idx));
	}
	return ret;
}

SHELL_STATIC_SUBCMD_SET_CREATE(ami_obis_cmds,
	SHELL_CMD(list,   NULL, "List OBIS codes and their polling state", cmd_ami_obis_list),
	SHELL_CMD_ARG(skip,   NULL, "Force-skip an OBIS code: skip <index>",   cmd_ami_obis_skip,   2, 0),
	SHELL_CMD_ARG(enable, NULL, "Re-enable an OBIS code:  enable <index>", cmd_ami_obis_enable, 2, 0),
	SHELL_SUBCMD_SET_END
);

SHELL_STATIC_SUBCMD_SET_CREATE(ami_cmds,
	SHELL_CMD(status, NULL, "Show overall node status",             cmd_ami_status),
	SHELL_CMD(test,   &ami_test_cmds, "Run subsystem tests",        NULL),
	SHELL_CMD(log,    &ami_log_cmds,  "Control DLMS/RS485 log verbosity", NULL),
	SHELL_CMD(diag,   NULL,           "Show per-OBIS read diagnostics",   cmd_ami_diag),
	SHELL_CMD(obis,   &ami_obis_cmds, "OBIS polling control (list/skip/enable)", NULL),
	SHELL_CMD(reset,  NULL,           "Reboot the node",                  cmd_ami_reset),
	SHELL_SUBCMD_SET_END
);

SHELL_CMD_REGISTER(ami, &ami_cmds, "AMI node test commands", NULL);

/* v0.15.0: force_notify_f64() and notify_all_observers() removed.
 * Threshold-based smart notification in meter_push_to_lwm2m() handles
 * all observer notifications directly after each DLMS poll cycle.
 */

/* ---- Read real meter data via RS485/DLMS ---- */
static void update_sensors(void)
{
	int ret;

#ifdef CONFIG_AMI_DEMO_MODE
	fill_demo_readings(&last_readings);
	meter_initialized = true;
	consecutive_meter_failures = 0;
	meter_push_to_lwm2m(&last_readings);
	return;
#endif

	if (!meter_initialized) {
		ret = meter_init();
		if (ret < 0) {
			LOG_ERR("Meter init failed: %d — using fallback", ret);
			update_sensors_fallback();
			consecutive_meter_failures++;
			return;
		}
		meter_initialized = true;
	}

	/* Full poll cycle: connect → read → disconnect */
	ret = meter_poll(&last_readings);
	if (ret < 0) {
		consecutive_meter_failures++;
		if (consecutive_meter_failures >= MAX_CONSEC_FAILURES) {
			LOG_ERR("Meter poll failed %d consecutive times — "
				"NO data sent to server (all stale)",
				consecutive_meter_failures);
		} else {
			LOG_WRN("Meter poll failed (%d) — keeping last values "
				"(%d/%d failures)", ret,
				consecutive_meter_failures, MAX_CONSEC_FAILURES);
		}
		update_sensors_fallback();
		return;
	}

	/* Reset failure counter on success */
	if (consecutive_meter_failures > 0) {
		LOG_INF("Meter recovered after %d failures",
			consecutive_meter_failures);
	}
	consecutive_meter_failures = 0;

	/* Push ONLY real meter readings to LwM2M (field_mask gates each field) */
	meter_push_to_lwm2m(&last_readings);
}

/* ---- Dedicated DLMS poll thread ---- */
static void dlms_thread_entry(void *p1, void *p2, void *p3)
{
	ARG_UNUSED(p1);
	ARG_UNUSED(p2);
	ARG_UNUSED(p3);

	while (1) {
		/* Wait until main loop triggers a poll */
		k_sem_take(&dlms_poll_sem, K_FOREVER);
		dlms_thread_running = true;

		update_sensors();

		dlms_thread_running = false;
	}
}

K_THREAD_DEFINE(dlms_tid, 4096, dlms_thread_entry,
		NULL, NULL, NULL, 5, 0, 0);

/*
 * Fallback: meter init or poll failed.
 * Do NOT push zeros — that would corrupt the LwM2M cache with fake data.
 * The previous valid readings (or LwM2M defaults) remain in place.
 */
static void update_sensors_fallback(void)
{
	LOG_WRN("Meter unavailable — keeping last known values (no zeros sent)");
}

/*
 * Demo mode: generate deterministic synthetic readings so the node can be
 * validated end-to-end (Thread + LwM2M + ThingsBoard) without a physical meter.
 */
static void fill_demo_readings(struct meter_readings *r)
{
	int64_t now_ms = k_uptime_get();
	uint32_t t = (uint32_t)(now_ms / 1000);
	double dt_h = 0.0;
	uint32_t mask = 0;

	if (demo_last_update_ms > 0 && now_ms > demo_last_update_ms) {
		dt_h = (double)(now_ms - demo_last_update_ms) / 3600000.0;
	}
	demo_last_update_ms = now_ms;

	memset(r, 0, sizeof(*r));

	/* Smooth bounded waveforms (no random jumps) */
	double v_r = 121.6 + ((double)((int)(t % 20) - 10) * 0.08);
	double i_r = 0.22 + ((double)((t / 3) % 8) * 0.012);
	double pf_r = 0.93 + ((double)((t / 7) % 5) * 0.004);
	double f_hz = 59.95 + ((double)((int)(t % 6) - 3) * 0.01);

	double p_r = (v_r * i_r * pf_r) / 1000.0;
	double q_r = p_r * 0.38;
	double s_r = p_r / pf_r;

	r->voltage_r = v_r;
	r->current_r = i_r;
	r->active_power_r = p_r;
	r->reactive_power_r = q_r;
	r->apparent_power_r = s_r;
	r->power_factor_r = pf_r;

	mask |= (1u << 0) | (1u << 1) | (1u << 2) | (1u << 3) | (1u << 4) | (1u << 5);

#ifndef CONFIG_AMI_SINGLE_PHASE
	/* Add synthetic phase offsets for 3-phase demo */
	double v_s = v_r - 0.6;
	double v_t = v_r + 0.7;
	double i_s = i_r * 0.97;
	double i_t = i_r * 1.03;
	double pf_s = pf_r - 0.01;
	double pf_t = pf_r + 0.005;

	double p_s = (v_s * i_s * pf_s) / 1000.0;
	double p_t = (v_t * i_t * pf_t) / 1000.0;
	double q_s = p_s * 0.36;
	double q_t = p_t * 0.40;
	double s_s = p_s / pf_s;
	double s_t = p_t / pf_t;

	r->voltage_s = v_s;
	r->current_s = i_s;
	r->active_power_s = p_s;
	r->reactive_power_s = q_s;
	r->apparent_power_s = s_s;
	r->power_factor_s = pf_s;

	r->voltage_t = v_t;
	r->current_t = i_t;
	r->active_power_t = p_t;
	r->reactive_power_t = q_t;
	r->apparent_power_t = s_t;
	r->power_factor_t = pf_t;

	r->total_active_power = p_r + p_s + p_t;
	r->total_reactive_power = q_r + q_s + q_t;
	r->total_apparent_power = s_r + s_s + s_t;
	r->total_power_factor = r->total_active_power / r->total_apparent_power;

	for (int i = 6; i <= 17; i++) {
		mask |= (1u << i);
	}
#else
	r->total_active_power = p_r;
	r->total_reactive_power = q_r;
	r->total_apparent_power = s_r;
	r->total_power_factor = pf_r;
#endif

	demo_energy_kwh += (r->total_active_power * dt_h);

	r->active_energy = demo_energy_kwh;
	r->reactive_energy = demo_energy_kwh * 0.40;
	r->apparent_energy = demo_energy_kwh * 1.08;
	r->frequency = f_hz;

	mask |= (1u << 18) | (1u << 19) | (1u << 20) | (1u << 21);
	mask |= (1u << 22) | (1u << 23) | (1u << 24) | (1u << 25);

	r->valid = true;
	r->field_mask = mask;
	r->read_target = __builtin_popcount(mask);
	r->read_count = r->read_target;
	r->error_count = 0;
	r->timestamp_ms = now_ms;
}

/*
 * Apply the exact OTBR active dataset and start Thread.
 * CONFIG_OPENTHREAD_MANUAL_START=y prevents auto-start, so we set
 * the dataset first and start Thread exactly once — avoiding the
 * radio stop/restart that triggers an ESP-IDF interrupt re-alloc bug.
 *
 * TLV blob exported from OTBR via: ot-ctl dataset active -x
 */

static void apply_otbr_dataset(void)
{
	static const uint8_t otbr_tlvs[] = {
		/* OTBR active dataset — exported via: ot-ctl dataset active -x
		 * Network: UNAL-Thread, Ch25, PAN 0x23ED
		 * Mesh-local: fdf5:bffd:0bd6:ef74::/64
		 */
		0x0e, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00,
		0x4a, 0x03, 0x00, 0x00, 0x0f, 0x35, 0x06, 0x00, 0x04, 0x00,
		0x1f, 0xff, 0xe0, 0x02, 0x08, 0x1a, 0x25, 0x78, 0xdd, 0x6e,
		0xe3, 0x57, 0x3b, 0x07, 0x08, 0xfd, 0xf5, 0xbf, 0xfd, 0x0b,
		0xd6, 0xef, 0x74, 0x05, 0x10, 0x5e, 0xde, 0xbe, 0xad, 0x64,
		0x40, 0x5b, 0x3e, 0x17, 0x19, 0x36, 0x46, 0xc2, 0x94, 0x22,
		0x85, 0x01, 0x02, 0x23, 0xed, 0x04, 0x10, 0xbb, 0x7e, 0x7a,
		0xee, 0x56, 0x23, 0x6e, 0xa9, 0x6a, 0xc8, 0xdc, 0x65, 0xbb,
		0xa1, 0x83, 0x51, 0x0c, 0x04, 0x02, 0xa0, 0xf7, 0xf8, 0x03,
		0x0b, 0x55, 0x4e, 0x41, 0x4c, 0x2d, 0x54, 0x68, 0x72, 0x65,
		0x61, 0x64, 0x00, 0x03, 0x00, 0x00, 0x19,
	};

	openthread_mutex_lock();
	struct otInstance *ot = openthread_get_default_instance();

	if (!ot) {
		LOG_ERR("OpenThread instance not available");
		openthread_mutex_unlock();
		return;
	}

	/* Erase any stale persistent Thread state from previous boots */
	otInstanceErasePersistentInfo(ot);
	LOG_INF("Persistent info erased");

	/* Set the full OTBR dataset (Thread not yet started due to MANUAL_START) */
	otOperationalDatasetTlvs dataset;

	memcpy(dataset.mTlvs, otbr_tlvs, sizeof(otbr_tlvs));
	dataset.mLength = sizeof(otbr_tlvs);

	otError err = otDatasetSetActiveTlvs(ot, &dataset);

	if (err != OT_ERROR_NONE) {
		LOG_ERR("otDatasetSetActiveTlvs failed: %d", (int)err);
		openthread_mutex_unlock();
		return;
	}

	LOG_INF("OTBR dataset applied (mesh-local fdf5:bffd:0bd6:ef74::/64)");
	LOG_INF("Dataset commissioned: %s",
		otDatasetIsCommissioned(ot) ? "yes" : "no");

	/* Set TX power to maximum (20 dBm) before enabling radio */
	otPlatRadioSetTransmitPower(ot, 20);
	LOG_INF("TX power set to 20 dBm");

	/* Enable IPv6 (this triggers otPlatRadioEnable → radio SLEEP) */
	otIp6SetEnabled(ot, true);
	LOG_INF("IPv6 enabled, radio state: %d",
		(int)otPlatRadioGetState(ot));

	/* Start Thread (this triggers otPlatRadioReceive → radio RX) */
	otThreadSetEnabled(ot, true);
	LOG_INF("Thread started, radio state: %d",
		(int)otPlatRadioGetState(ot));

	openthread_mutex_unlock();
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

	esp_rom_printf("\r\n[AMI] main() ENTERED\r\n");

	/* Diagnostic: verify main() is executing and console works */
	k_sleep(K_MSEC(500));
	esp_rom_printf("[AMI] After 500ms sleep, printk next\r\n");
	printk("\n\n*** AMI ALIVE v%s ***\n", CLIENT_FIRMWARE_VER);

	LOG_INF("=== AMI LwM2M Node v%s ===", CLIENT_FIRMWARE_VER);
	LOG_INF("Board: %s", CONFIG_BOARD);
	LOG_INF("Network: Thread Ch%d PAN 0x%04X",
		CONFIG_OPENTHREAD_CHANNEL, CONFIG_OPENTHREAD_PANID);

	/* Suppress DLMS/RS485 noise at startup — INF+DBG filtered out. Re-enable via 'ami log verbose' */
	set_dlms_log_level(LOG_LEVEL_WRN);

	/* Suppress cosmetic LwM2M registry re-sync errors at startup (restored after 25 s) */
	suppress_lwm2m_registry_startup_noise();
	init_lwm2m_server_uris();

	/* LED init */
	if (gpio_is_ready_dt(&led0)) {
		gpio_pin_configure_dt(&led0, GPIO_OUTPUT_INACTIVE);
	}

	/* Apply OTBR dataset (mesh-local prefix + PSKc) before Thread attaches */
	apply_otbr_dataset();

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

	/* Main loop — DLMS poll at configurable interval with smart threshold notify */
	LOG_INF("Entering sensor loop (DLMS=%ds, conn=%ds, threshold-notify)",
		dlms_poll_interval_s, CONN_UPDATE_INTERVAL_S);

	/* Initial update so resources have real values before first sleep */
	update_connectivity_metrics();
	update_thread_network();
	update_thread_neighbors();
	k_sem_give(&dlms_poll_sem);  /* Trigger initial DLMS poll in background */
	last_dlms_poll_ms = k_uptime_get();
	int64_t last_conn_update_ms = last_dlms_poll_ms;

	while (1) {
		k_sleep(LOOP_TICK);

		int64_t now = k_uptime_get();

		/* Full DLMS poll at configurable interval (non-blocking) */
		if ((now - last_dlms_poll_ms) >= (dlms_poll_interval_s * 1000)) {
			if (!dlms_thread_running) {
				k_sem_give(&dlms_poll_sem);  /* Wake DLMS thread */
			}
			last_dlms_poll_ms = now;
		}

		/* Connectivity metrics at slower rate (v0.18.0: decoupled from DLMS) */
		if ((now - last_conn_update_ms) >= (CONN_UPDATE_INTERVAL_S * 1000)) {
			update_connectivity_metrics();
			update_thread_network();
			update_thread_neighbors();
			last_conn_update_ms = now;
		}
	}

	return 0;
}
