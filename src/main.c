/*
 * AMI LwM2M Node — Thread + LwM2M on ESP32-C6 Super Mini
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
 */

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/logging/log_ctrl.h>
#include <zephyr/sys/reboot.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/hwinfo.h>           /* PRIO 7: reset cause */
#include <zephyr/net/lwm2m.h>
#include <zephyr/net/net_if.h>
#include <zephyr/random/random.h>
#include <zephyr/shell/shell.h>
#include <zephyr/init.h>
#include <zephyr/settings/settings.h>        /* PRIO 7: persist total_resets */
#include <zephyr/sys/atomic.h>
#include <string.h>
#include <openthread.h>
#include <openthread/thread.h>
#include <openthread/instance.h>
#include <openthread/dataset.h>
#include <openthread/ip6.h>
#include <openthread/platform/radio.h>

#include "lwm2m_discover.h"
#include "lwm2m_watchdog.h"
#include "lwm2m_obj_power_meter.h"
#include "lwm2m_obj_thread_diag.h"
#include "lwm2m_obj_thread_net.h"
#include "lwm2m_obj_thread_neighbor.h"
#include "lwm2m_obj_thread_commission.h"
#include "lwm2m_obj_thread_cli.h"
#include "lwm2m_observation.h"
#include "dlms_meter.h"
#include "rgb_led.h"

/* Firmware update (Object 5) */
extern void init_firmware_update(void);

/* Thread connectivity monitoring (Objects 4 + 33000) */
extern void init_connmon_thread(void);
extern void init_thread_diag_object(void);
extern void update_connectivity_metrics(void);

LOG_MODULE_REGISTER(ami_lwm2m, LOG_LEVEL_INF);

/* Early SYS_INIT to confirm kernel is running before main() */
extern int esp_rom_printf(const char *fmt, ...);

/* PRE_KERNEL_1: earliest boot marker — confirms ROM → Zephyr handoff */
static int boot_pre_kernel(void)
{
	esp_rom_printf("\r\n[AMI] PRE_KERNEL_1 OK\r\n");
	return 0;
}
SYS_INIT(boot_pre_kernel, PRE_KERNEL_1, 0);

/* ---- Configuration ---- */
#define CLIENT_MANUFACTURER     "Tesis-AMI"
#define CLIENT_MODEL_NUMBER     "ESP32-C6-Super-Mini"
#define CLIENT_SERIAL_NUMBER    "AMI-001"
#define CLIENT_FIRMWARE_VER     "0.6.1"
#define CLIENT_HW_VER           "1.0"

/* Endpoint name built at runtime from MAC — e.g. "ami-esp32c6-2434" */
static char endpoint_name[32];

/* LwM2M server URI runtime selection (primary + secondary fallback). */
/* DNS-SD resolved server URI. Set at boot from
 * lwm2m_discover_resolve(_lwm2m._udp.default.service.arpa.) and refreshed
 * on every recovery cycle. The Kconfig fallback symbols
 * AMI_LWM2M_SERVER_IPV6_PRIMARY/_SECONDARY are deprecated and ignored
 * (v0.6.0+). */
static char lwm2m_server_uri[96];
static uint8_t lwm2m_reg_failures;

/* === v0.20.0 production observability counters (Object 33000 RIDs 11-20) ===
 * Monotonic from boot. Snapshot read by thread_conn_monitor.c via the
 * getters below. atomic_t for safety with the work queue contexts.
 */
static atomic_t lwm2m_diag_reg_attempts     = ATOMIC_INIT(0);
static atomic_t lwm2m_diag_reg_success      = ATOMIC_INIT(0);
static atomic_t lwm2m_diag_recover_count    = ATOMIC_INIT(0);
static atomic_t lwm2m_diag_restart_success  = ATOMIC_INIT(0);
/* v2.2 — populated from PRIO 1.5 (diagnostics), PRIO 5 (watchdog), PRIO 6 (jitter) */
static atomic_t lwm2m_diag_last_error_code  = ATOMIC_INIT(0);   /* signed; stored as int */
static atomic_t lwm2m_diag_last_error_uptime = ATOMIC_INIT(0);
static atomic_t lwm2m_diag_watchdog_count   = ATOMIC_INIT(0);
static atomic_t lwm2m_diag_storm_backoff    = ATOMIC_INIT(0);
/* Set true while lwm2m_recover_work_fn is in flight; the next
 * REGISTRATION_COMPLETE counts as a successful restart. */
static atomic_t lwm2m_diag_in_recovery = ATOMIC_INIT(0);

uint32_t lwm2m_diag_get_reg_attempts(void)
{ return (uint32_t)atomic_get(&lwm2m_diag_reg_attempts); }
uint32_t lwm2m_diag_get_reg_success(void)
{ return (uint32_t)atomic_get(&lwm2m_diag_reg_success); }
uint32_t lwm2m_diag_get_recover_count(void)
{ return (uint32_t)atomic_get(&lwm2m_diag_recover_count); }
uint32_t lwm2m_diag_get_restart_success(void)
{ return (uint32_t)atomic_get(&lwm2m_diag_restart_success); }
int32_t  lwm2m_diag_get_last_error_code(void)
{ return (int32_t)atomic_get(&lwm2m_diag_last_error_code); }
uint32_t lwm2m_diag_get_last_error_uptime(void)
{ return (uint32_t)atomic_get(&lwm2m_diag_last_error_uptime); }
uint32_t lwm2m_diag_get_watchdog_count(void)
{ return (uint32_t)atomic_get(&lwm2m_diag_watchdog_count); }
uint32_t lwm2m_diag_get_storm_backoff(void)
{ return (uint32_t)atomic_get(&lwm2m_diag_storm_backoff); }

/* Helper: record an error with timestamp. Public for watchdog/event paths. */
void lwm2m_diag_record_error(int err)
{
	atomic_set(&lwm2m_diag_last_error_code, err);
	atomic_set(&lwm2m_diag_last_error_uptime, (uint32_t)(k_uptime_get() / 1000));
}

/* Helper: invoked by lwm2m_watchdog when it forces a recover. */
void lwm2m_diag_inc_watchdog_count(void)
{
	atomic_inc(&lwm2m_diag_watchdog_count);
}

/* Helper: invoked by recover/event paths when a registration storm is
 * detected (5.03/5.00 / NETWORK_ERROR proxy). Doubles backoff externally. */
void lwm2m_diag_inc_storm_backoff(void)
{
	atomic_inc(&lwm2m_diag_storm_backoff);
}

/* Helper: invoked from any event handler that schedules lwm2m_recover_work
 * in response to an engine-side error. Bug B fix: previously recover_count
 * only incremented inside lwm2m_recover_work_fn, so events that triggered
 * a fresh REGISTER without going through the work_fn (e.g. NETWORK_ERROR
 * → engine internal restart) left recover_count=0 and zombie events
 * invisible to the operator. */
void lwm2m_diag_inc_recover_count(void)
{
	atomic_inc(&lwm2m_diag_recover_count);
}

/* Helper: increment reg_attempts at every call site that invokes
 * lwm2m_rd_client_start(). Bug C fix: previously reg_attempts only
 * incremented from boot path + recover_work_fn, so engine-initiated
 * REGISTERs (e.g. after lifetime expiry via REG_UPDATE failure path) left
 * reg_success > reg_attempts which broke the "success rate = success/attempts"
 * intuition. Always pair with the actual rd_client_start call. */
void lwm2m_diag_inc_reg_attempts(void)
{
	atomic_inc(&lwm2m_diag_reg_attempts);
}

/* === v2.3 boot reliability counters (Object 33000 RIDs 21-22) ===
 *
 * last_reset_reason: hwinfo_get_reset_cause() bitmap captured at boot.
 *                    Bit values (zephyr/drivers/hwinfo.h): RESET_PIN=BIT(0),
 *                    RESET_SOFTWARE=BIT(1), RESET_BROWNOUT=BIT(2),
 *                    RESET_POR=BIT(3), RESET_WATCHDOG=BIT(4),
 *                    RESET_LOW_POWER_WAKE=BIT(7), RESET_CPU_LOCKUP=BIT(8).
 *                    -1 if hwinfo failed to read.
 *
 * total_resets:      Monotonic counter persisted in NVS via the settings
 *                    subsystem under "ami/total_resets". Survives reboots.
 *                    Survives crashes too because it's incremented EARLY
 *                    in main() before any LwM2M / Thread code runs.
 */
static atomic_t lwm2m_diag_last_reset_reason = ATOMIC_INIT(-1);
static atomic_t lwm2m_diag_total_resets      = ATOMIC_INIT(0);

int32_t  lwm2m_diag_get_last_reset_reason(void)
{ return (int32_t)atomic_get(&lwm2m_diag_last_reset_reason); }
uint32_t lwm2m_diag_get_total_resets(void)
{ return (uint32_t)atomic_get(&lwm2m_diag_total_resets); }

/* === Boot watchdog atomic (PRIO 8) ===
 * Set to 1 on the FIRST LWM2M_RD_CLIENT_EVENT_REGISTRATION_COMPLETE.
 * The boot watchdog work fires CONFIG_AMI_BOOT_REGISTER_DEADLINE_S
 * seconds after main() reaches the watchdog init point; if the atomic
 * is still 0, the boot path has failed to register and we sys_reboot.
 */
static atomic_t lwm2m_first_register_complete = ATOMIC_INIT(0);

/* === Drain-and-reboot helper (USB-friendly) ===
 *
 * The ESP32-C6 native USB Serial/JTAG driver leaves the host-side device
 * in a "enumerated but cannot open" state if sys_reboot() races with an
 * active console. Sleeping before the reboot lets the host detect link
 * drop, clean its USB stack, and re-enumerate cleanly post-reset.
 *
 * Use this helper for EVERY sys_reboot() in the firmware.
 */
static void ami_reboot_drain(int reboot_type, const char *reason)
{
	LOG_WRN("REBOOT (%s): drain USB %dms then sys_reboot(%s)",
		reason, CONFIG_AMI_REBOOT_USB_DRAIN_MS,
		reboot_type == SYS_REBOOT_WARM ? "WARM" : "COLD");
	/* Flush log buffer + give USB host time to detect link drop. */
	if (CONFIG_AMI_REBOOT_USB_DRAIN_MS > 0) {
		k_sleep(K_MSEC(CONFIG_AMI_REBOOT_USB_DRAIN_MS));
	}
	sys_reboot(reboot_type);
}

/* === Settings subsystem hooks for total_resets persistence ===
 *
 * Storage key: "ami/total_resets" (uint32_t little-endian).
 * Loaded once at boot; saved once after capture_reset_reason() bumps it.
 */
#define AMI_RESETS_KEY "ami/total_resets"

static int ami_settings_load_cb(const char *name, size_t len,
				settings_read_cb read_cb, void *cb_arg)
{
	if (strcmp(name, "total_resets") == 0 && len == sizeof(uint32_t)) {
		uint32_t v = 0;
		ssize_t got = read_cb(cb_arg, &v, sizeof(v));
		if (got == sizeof(v)) {
			atomic_set(&lwm2m_diag_total_resets, (atomic_val_t)v);
		}
	}
	return 0;
}

SETTINGS_STATIC_HANDLER_DEFINE(ami_settings, "ami", NULL,
			       ami_settings_load_cb, NULL, NULL);

/* Capture reset cause + bump total_resets (PRIO 7).
 * Call once, very early, from main(). Failures are non-fatal.
 */
static void capture_reset_reason(void)
{
	uint32_t cause = 0;
	int rc = hwinfo_get_reset_cause(&cause);
	if (rc == 0) {
		atomic_set(&lwm2m_diag_last_reset_reason, (atomic_val_t)cause);
		LOG_INF("Reset cause: 0x%08x (POR=%d PIN=%d SW=%d WDT=%d "
			"BROWNOUT=%d CPU_LOCKUP=%d)",
			cause,
			!!(cause & RESET_POR),
			!!(cause & RESET_PIN),
			!!(cause & RESET_SOFTWARE),
			!!(cause & RESET_WATCHDOG),
			!!(cause & RESET_BROWNOUT),
			!!(cause & RESET_CPU_LOCKUP));
		(void)hwinfo_clear_reset_cause();
	} else {
		LOG_WRN("hwinfo_get_reset_cause failed: %d", rc);
	}

	/* Increment + persist total_resets BEFORE any networking code runs,
	 * so even a crash mid-boot still bumps the counter on the next try. */
	uint32_t prev = (uint32_t)atomic_get(&lwm2m_diag_total_resets);
	uint32_t now  = prev + 1U;
	atomic_set(&lwm2m_diag_total_resets, (atomic_val_t)now);
	int sr = settings_save_one(AMI_RESETS_KEY, &now, sizeof(now));
	LOG_INF("total_resets: %u -> %u (settings_save=%d)", prev, now, sr);
}

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
/* WS2812 RGB brightness (0-255). Keep low to avoid blinding. */
#define AMI_RGB_BRIGHTNESS 40

enum ami_rgb_color {
	AMI_RGB_OFF,
	AMI_RGB_RED,
	AMI_RGB_GREEN,
	AMI_RGB_BLUE,
	AMI_RGB_YELLOW,
	AMI_RGB_CYAN,
	AMI_RGB_MAGENTA,
	AMI_RGB_WHITE,
};

static const char *rgb_color_name(enum ami_rgb_color c)
{
	static const char *names[] = {
		"OFF","RED","GREEN","BLUE","YELLOW","CYAN","MAGENTA","WHITE"
	};
	return (c <= AMI_RGB_WHITE) ? names[c] : "?";
}

static void ami_set_rgb(enum ami_rgb_color color)
{
	const uint8_t br = AMI_RGB_BRIGHTNESS;

	LOG_INF("LED -> %s", rgb_color_name(color));

	if (rgb_led_is_ready()) {
		switch (color) {
		case AMI_RGB_RED:     rgb_led_set(br, 0, 0);   break;
		case AMI_RGB_GREEN:   rgb_led_set(0, br, 0);   break;
		case AMI_RGB_BLUE:    rgb_led_set(0, 0, br);   break;
		case AMI_RGB_YELLOW:  rgb_led_set(br, br, 0);  break;
		case AMI_RGB_CYAN:    rgb_led_set(0, br, br);  break;
		case AMI_RGB_MAGENTA: rgb_led_set(br, 0, br);  break;
		case AMI_RGB_WHITE:   rgb_led_set(br, br, br); break;
		case AMI_RGB_OFF:
		default:              rgb_led_off();           break;
		}
		return;
	}

	/* Fallback for boards that only expose led0. */
	if (gpio_is_ready_dt(&led0)) {
		bool on = (color != AMI_RGB_OFF);
		gpio_pin_set_dt(&led0, on ? 1 : 0);
	}
}

static void ami_led_init(void)
{
	/* led0 fallback */
	if (gpio_is_ready_dt(&led0)) {
		gpio_pin_configure_dt(&led0, GPIO_OUTPUT_INACTIVE);
	}

	/* WS2812 on GPIO8 (ESP32-C6 Super Mini) */
	int ret = rgb_led_init();
	if (ret == 0) {
		LOG_INF("WS2812 RGB LED initialized on GPIO8");
	} else {
		LOG_WRN("WS2812 init failed (%d) - using led0 fallback", ret);
	}
}

/* ---- DLMS Meter readings ---- */
static struct meter_readings last_readings;
static bool meter_initialized;
static int64_t demo_last_update_ms;
static double demo_energy_kwh = 61100.0;

/* Forward declarations */
static void update_sensors_fallback(void);
static void fill_demo_readings(struct meter_readings *r);
static void rd_client_event(struct lwm2m_ctx *client,
			enum lwm2m_rd_client_event client_event);
static void observe_cb(enum lwm2m_observe_event event,
		      struct lwm2m_obj_path *path, void *user_data);

/* ---- LwM2M context ---- */
static struct lwm2m_ctx client_ctx;
static bool lwm2m_connected;
static atomic_t lwm2m_recovering;

/* DNS-SD resolution with retry+backoff.
 *
 * Boot path: invoke this until success or until CONFIG_AMI_LWM2M_DNS_SD_RETRY_MAX
 * attempts have failed; the caller is responsible for the escalation
 * (sys_reboot warm) on terminal failure.
 *
 * Recovery path: invoke once before lwm2m_rd_client_start(); on failure,
 * abort the cycle and let recover_work_fn reschedule with backoff.
 *
 * Returns 0 on success (lwm2m_server_uri populated), negative errno on failure.
 */
static int lwm2m_discover_with_retry(int max_attempts, int per_attempt_ms)
{
	char discovered[sizeof(lwm2m_server_uri)];
	int backoff_s = 5;
	const int backoff_cap_s = 60;

	for (int i = 1; i <= max_attempts; i++) {
		LOG_INF("DNS-SD lookup attempt %d/%d (timeout=%dms)...",
			i, max_attempts, per_attempt_ms);
		int ret = lwm2m_discover_resolve(discovered, sizeof(discovered),
						 per_attempt_ms);
		if (ret == 0) {
			strncpy(lwm2m_server_uri, discovered,
				sizeof(lwm2m_server_uri) - 1);
			lwm2m_server_uri[sizeof(lwm2m_server_uri) - 1] = '\0';
			LOG_INF("DNS-SD resolved: %s", lwm2m_server_uri);
			return 0;
		}

		LOG_WRN("DNS-SD lookup failed (err=%d); attempt %d/%d, "
			"sleeping %ds", ret, i, max_attempts, backoff_s);
		if (i < max_attempts) {
			k_sleep(K_SECONDS(backoff_s));
			backoff_s = MIN(backoff_s * 2, backoff_cap_s);
		}
	}
	return -EHOSTUNREACH;
}

/* Forward declarations so lwm2m_recover_work_fn can reschedule itself
 * (recovery on start() failure path).
 */
static void lwm2m_recover_work_fn(struct k_work *w);
static void lwm2m_recover_probe_fn(struct k_work *w);
static void boot_watchdog_fn(struct k_work *w);
/* Non-static: lwm2m_watchdog.c references this symbol via extern. */
K_WORK_DELAYABLE_DEFINE(lwm2m_recover_work, lwm2m_recover_work_fn);

/* Boot watchdog (PRIO 8): fires CONFIG_AMI_BOOT_REGISTER_DEADLINE_S after
 * being scheduled. If lwm2m_first_register_complete is still 0, the boot
 * path failed to produce a REGISTRATION_COMPLETE — sys_reboot warm to retry
 * the entire boot sequence. The work is canceled in the
 * REGISTRATION_COMPLETE handler. */
static K_WORK_DELAYABLE_DEFINE(boot_watchdog_work, boot_watchdog_fn);

static void boot_watchdog_fn(struct k_work *w)
{
	ARG_UNUSED(w);
	if (atomic_get(&lwm2m_first_register_complete) != 0) {
		/* Should not happen: REGISTRATION_COMPLETE handler cancels us. */
		LOG_DBG("boot_watchdog fired but already registered — no-op");
		return;
	}
	LOG_ERR("BOOT WATCHDOG: no REGISTRATION_COMPLETE in %ds — "
		"sys_reboot WARM to retry boot",
		CONFIG_AMI_BOOT_REGISTER_DEADLINE_S);
	lwm2m_diag_record_error(-ETIMEDOUT);
	ami_reboot_drain(SYS_REBOOT_WARM, "boot-watchdog");
	/* unreachable */
}
/* Post-restart health probe — fires N seconds after start() returns OK to
 * verify the engine actually produced a REGISTRATION_COMPLETE. If not,
 * treats the restart as silently failed and reschedules recover_work.
 */
static K_WORK_DELAYABLE_DEFINE(lwm2m_recover_probe, lwm2m_recover_probe_fn);
/* Snapshot of reg_success at the moment we scheduled the probe; the probe
 * compares this to the current value to decide if a fresh REGISTRATION
 * happened during the probe window. */
static atomic_t lwm2m_probe_baseline_success = ATOMIC_INIT(0);
#define LWM2M_RECOVER_PROBE_S  30   /* health probe window after start() */

/* ---- Recovery state machine (production hardening) ----
 *
 * Original design called lwm2m_rd_client_stop+start once per failure event
 * and gave up if start() returned an error. Field testing with 30 nodes
 * revealed ~80% zombie rate after server hiccups (GC pause / restart /
 * network blip): a single failed start() left the engine wedged.
 *
 * v0.20.0 redesign:
 *   - Exponential backoff (60s → 120s → 240s → 300s cap)
 *   - Retry on start() failure (not just on next external event)
 *   - After MAX_ATTEMPTS, sys_reboot(WARM) preserves Thread session
 *   - Counter resets ONLY on REGISTRATION_COMPLETE (most conservative —
 *     repeated REGISTER→fail cycles still escalate).
 *
 * Tunables in Kconfig: AMI_LWM2M_RECOVER_BACKOFF_{MIN,MAX}_S,
 *                       AMI_LWM2M_RECOVER_MAX_ATTEMPTS.
 *
 * Reproduction of the original bug for regression testing:
 *   1. Bring 5+ nodes registered against the Edge.
 *   2. systemctl stop tb-edge   (or docker stop tb-edge-v2)
 *   3. Wait ≥ lifetime + grace.
 *   4. systemctl start tb-edge.
 *   5. Without this fix: ~80% nodes silent, only power-cycle recovers.
 *      With this fix: 100% nodes back inside ~5 min via recovery work.
 */
static uint8_t lwm2m_recover_attempt;

/*
 * PRIO 6 — exponential backoff with ±25% decorrelated jitter.
 *
 * Original (PRIO 1) was deterministic: 60s, 120s, 240s, 300s. With 30 nodes
 * in a mesh-cascade event, all 30 retry at the same wall-clock second →
 * server avalanche → many fail again. Standard fix (AWS guidance):
 * randomize ±25% so retries spread over a 50%-wide window.
 *
 * Range per attempt: [0.75 × base, 1.25 × base]
 * Examples (base=60): attempt 1 → 45..75 s, attempt 2 → 90..150 s, etc.
 */
static uint32_t lwm2m_recover_backoff_s(uint8_t attempt)
{
	uint32_t base = CONFIG_AMI_LWM2M_RECOVER_BACKOFF_MIN_S;
	for (uint8_t i = 1; i < attempt; i++) {
		base *= 2;
		if (base >= (uint32_t)CONFIG_AMI_LWM2M_RECOVER_BACKOFF_MAX_S) {
			base = CONFIG_AMI_LWM2M_RECOVER_BACKOFF_MAX_S;
			break;
		}
	}
	/* ±25% jitter: window width = base/2, centered at base.
	 * lower bound = base * 3/4 ; upper bound = base * 5/4
	 */
	uint32_t window = base / 2;
	uint32_t r = window ? (sys_rand32_get() % (window + 1)) : 0;
	uint32_t lower = (base * 3) / 4;
	return lower + r;
}

static void lwm2m_recover_work_fn(struct k_work *w)
{
	ARG_UNUSED(w);

	atomic_set(&lwm2m_recovering, 1);
	atomic_set(&lwm2m_diag_in_recovery, 1);
	/* recover_count (RID 15) NOT incremented here. It's now incremented
	 * from the event handlers that detect the failure (Bug B fix). The
	 * event is the canonical signal of "a recovery cycle started"; the
	 * work_fn is just the executor and may not always run before the
	 * engine self-recovers. */
	lwm2m_recover_attempt++;

	LOG_INF("recover_work_fn entry: attempt=%u total_recover=%u uptime=%llds",
		lwm2m_recover_attempt,
		lwm2m_diag_get_recover_count(),
		k_uptime_get() / 1000);

	if (lwm2m_recover_attempt > CONFIG_AMI_LWM2M_RECOVER_MAX_ATTEMPTS) {
		LOG_ERR("LwM2M engine: max restart attempts (%d) reached, "
			"scheduling soft (warm) reboot to preserve Thread session",
			CONFIG_AMI_LWM2M_RECOVER_MAX_ATTEMPTS);
		lwm2m_diag_record_error(-ETIMEDOUT);
		ami_reboot_drain(SYS_REBOOT_WARM, "max-recover-attempts");
		/* unreachable */
	}

	/* PRIO 9: re-resolve DNS-SD before each restart. The OTBR may have
	 * regenerated its mleid/OMR (RCP swap, dataset reapply, partition
	 * split) since boot. Restarting against a stale URI guarantees
	 * silent failure. If DNS-SD fails here, abort and retry next cycle —
	 * better to miss one cycle than to register against a dead address.
	 */
	{
		int dns_ret = lwm2m_discover_with_retry(2,
					CONFIG_AMI_LWM2M_DNS_SD_TIMEOUT_MS);
		if (dns_ret != 0) {
			LOG_WRN("recover: DNS-SD lookup failed (err=%d). "
				"Aborting cycle, will retry on next event/watchdog",
				dns_ret);
			lwm2m_diag_record_error(dns_ret);
			atomic_set(&lwm2m_recovering, 0);
			atomic_set(&lwm2m_diag_in_recovery, 0);
			/* Don't schedule a follow-up here — recover_count was
			 * already incremented by the caller (event handler /
			 * watchdog). Let the next failure event re-trigger us. */
			return;
		}
	}

	LOG_INF("LwM2M engine: restart attempt %d/%d (server=%s)",
		lwm2m_recover_attempt,
		CONFIG_AMI_LWM2M_RECOVER_MAX_ATTEMPTS,
		lwm2m_server_uri);

	LOG_INF("calling lwm2m_rd_client_stop");
	int ret_stop = lwm2m_rd_client_stop(&client_ctx, rd_client_event, false);
	LOG_INF("stop returned %d", ret_stop);

	memset(&client_ctx, 0, sizeof(client_ctx));

	/* Update Object 0 RID 0 (Server URI) with the freshly resolved address. */
	lwm2m_set_string(&LWM2M_OBJ(0, 0, 0), lwm2m_server_uri);

	lwm2m_diag_inc_reg_attempts();   /* Object 33000 RID 11 */
	LOG_INF("calling lwm2m_rd_client_start (attempt %d)", lwm2m_recover_attempt);
	int ret = lwm2m_rd_client_start(&client_ctx, endpoint_name, 0,
				      rd_client_event, observe_cb);
	LOG_INF("start returned %d (recover_count=%u)",
		ret, lwm2m_diag_get_recover_count());

	if (ret == 0 || ret == -EINPROGRESS) {
		LOG_INF("LwM2M engine: restart requested (attempt %d), "
			"scheduling health probe in %ds",
			lwm2m_recover_attempt, LWM2M_RECOVER_PROBE_S);
		/* PRIO 1.5: snapshot reg_success counter; probe will compare
		 * this value to detect "silent" failures where start() said OK
		 * but no REGISTRATION_COMPLETE event ever arrived.
		 */
		atomic_set(&lwm2m_probe_baseline_success,
			   atomic_get(&lwm2m_diag_reg_success));
		k_work_reschedule(&lwm2m_recover_probe,
				  K_SECONDS(LWM2M_RECOVER_PROBE_S));
		atomic_set(&lwm2m_recovering, 0);
		return;
	}

	/* start() returned an error inline — record it and schedule retry */
	lwm2m_diag_record_error(ret);
	uint32_t next_s = lwm2m_recover_backoff_s(lwm2m_recover_attempt + 1);
	LOG_ERR("LwM2M engine: restart failed (err=%d), "
		"next attempt %d/%d in %us",
		ret,
		lwm2m_recover_attempt + 1,
		CONFIG_AMI_LWM2M_RECOVER_MAX_ATTEMPTS,
		next_s);

	atomic_set(&lwm2m_recovering, 0);
	k_work_reschedule(&lwm2m_recover_work, K_SECONDS(next_s));
}

/*
 * PRIO 1.5 — Post-restart health probe.
 *
 * Fires LWM2M_RECOVER_PROBE_S seconds after a successful start() call.
 * Compares the snapshot of reg_success taken at restart time to the
 * current value. If unchanged, the restart silently failed (engine in
 * "alive but never registered" state) and we reschedule recover_work.
 *
 * This is the fix for the 73% non-active rate observed at field validation:
 * lwm2m_rd_client_start() can return 0 but get stuck internally without
 * emitting REGISTRATION_COMPLETE, leaving the node zombie until external
 * reset. The probe rescues those cases.
 */
static void lwm2m_recover_probe_fn(struct k_work *w)
{
	ARG_UNUSED(w);

	uint32_t baseline = (uint32_t)atomic_get(&lwm2m_probe_baseline_success);
	uint32_t current  = lwm2m_diag_get_reg_success();

	if (current > baseline) {
		LOG_INF("recover_probe: REGISTRATION_COMPLETE seen "
			"(reg_success %u→%u). Restart truly succeeded.",
			baseline, current);
		/* Counter reset already happened in REGISTRATION_COMPLETE path. */
		return;
	}

	/* Silent failure: start() said OK but no REGISTER produced.
	 * Treat as failed restart, reschedule with backoff.
	 */
	lwm2m_diag_record_error(-ENETUNREACH);
	uint32_t next_s = lwm2m_recover_backoff_s(lwm2m_recover_attempt + 1);
	LOG_WRN("recover_probe: SILENT FAILURE — reg_success unchanged at %u "
		"after %ds since start(). Re-scheduling recover in %us",
		current, LWM2M_RECOVER_PROBE_S, next_s);
	k_work_reschedule(&lwm2m_recover_work, K_SECONDS(next_s));
}

/* ---- LwM2M callbacks ---- */
static int device_reboot_cb(uint16_t obj_inst_id,
			    uint8_t *args, uint16_t args_len)
{
	LOG_INF("DEVICE: Reboot requested");
	ami_reboot_drain(SYS_REBOOT_COLD, "lwm2m-device-reboot");
	return 0;
}

static void rd_client_event(struct lwm2m_ctx *client,
			    enum lwm2m_rd_client_event client_event)
{
	uint32_t backoff_s;

	switch (client_event) {
	case LWM2M_RD_CLIENT_EVENT_NONE:
		break;
	case LWM2M_RD_CLIENT_EVENT_REGISTRATION_COMPLETE:
		LOG_INF("LwM2M Registration complete (recovery attempts reset 0)");
		lwm2m_connected = true;
		lwm2m_reg_failures = 0;
		lwm2m_recover_attempt = 0;   /* confirmed sane state — reset escalation */
		atomic_inc(&lwm2m_diag_reg_success);   /* Object 33000 RID 12 */
		if (atomic_cas(&lwm2m_diag_in_recovery, 1, 0)) {
			atomic_inc(&lwm2m_diag_restart_success);   /* Object 33000 RID 16 */
		}
		/* PRIO 8: cancel boot watchdog on first successful registration. */
		if (atomic_cas(&lwm2m_first_register_complete, 0, 1)) {
			k_work_cancel_delayable(&boot_watchdog_work);
			LOG_INF("Boot watchdog disarmed (first REGISTER complete)");
		}
		k_work_cancel_delayable(&lwm2m_recover_work);
		ami_set_rgb(AMI_RGB_GREEN);
		break;
	case LWM2M_RD_CLIENT_EVENT_REGISTRATION_FAILURE:
		LOG_ERR("LwM2M Registration FAILED");
		lwm2m_connected = false;
		ami_set_rgb(AMI_RGB_RED);
		lwm2m_reg_failures++;
		lwm2m_diag_record_error(-ECONNRESET);
		lwm2m_diag_inc_recover_count();   /* Bug B: surface recovery cycle */
		backoff_s = lwm2m_recover_backoff_s(lwm2m_recover_attempt + 1);
		LOG_INF("LwM2M engine error: scheduling restart attempt %d/%d in %us",
			lwm2m_recover_attempt + 1,
			CONFIG_AMI_LWM2M_RECOVER_MAX_ATTEMPTS, backoff_s);
		/* Always reschedule (even if currently recovering): k_work_reschedule
		 * handles the running case by re-firing after the current invocation.
		 * This avoids losing events that arrive during recovery work execution.
		 */
		k_work_reschedule(&lwm2m_recover_work, K_SECONDS(backoff_s));
		break;
	case LWM2M_RD_CLIENT_EVENT_REG_TIMEOUT:
		LOG_WRN("LwM2M Registration timeout");
		lwm2m_connected = false;
		ami_set_rgb(AMI_RGB_RED);
		lwm2m_reg_failures++;
		lwm2m_diag_record_error(-ETIMEDOUT);
		lwm2m_diag_inc_recover_count();   /* Bug B */
		backoff_s = lwm2m_recover_backoff_s(lwm2m_recover_attempt + 1);
		LOG_INF("LwM2M engine error: scheduling restart attempt %d/%d in %us",
			lwm2m_recover_attempt + 1,
			CONFIG_AMI_LWM2M_RECOVER_MAX_ATTEMPTS, backoff_s);
		k_work_reschedule(&lwm2m_recover_work, K_SECONDS(backoff_s));
		break;
	case LWM2M_RD_CLIENT_EVENT_REG_UPDATE_COMPLETE:
		LOG_DBG("LwM2M Registration update complete");
		/* Note: per design (most-conservative), we do NOT reset
		 * lwm2m_recover_attempt here. Only REGISTRATION_COMPLETE
		 * resets it. This keeps escalation tight if the engine is
		 * looping REGISTER→UPDATE-fail→REGISTER cycles.
		 *
		 * BUT we DO ping the watchdog: a successful UPDATE proves the
		 * engine is alive end-to-end, even if no Notify went out
		 * recently (e.g. all 14 resources throttled).
		 */
		lwm2m_watchdog_emit_event();
		break;
	case LWM2M_RD_CLIENT_EVENT_DISCONNECT:
		LOG_WRN("LwM2M Disconnected");
		lwm2m_connected = false;
		ami_set_rgb(AMI_RGB_YELLOW);
		lwm2m_diag_record_error(-ENOTCONN);
		lwm2m_diag_inc_recover_count();   /* Bug B */
		backoff_s = lwm2m_recover_backoff_s(lwm2m_recover_attempt + 1);
		LOG_INF("LwM2M engine error: scheduling restart attempt %d/%d in %us",
			lwm2m_recover_attempt + 1,
			CONFIG_AMI_LWM2M_RECOVER_MAX_ATTEMPTS, backoff_s);
		k_work_reschedule(&lwm2m_recover_work, K_SECONDS(backoff_s));
		break;
	case LWM2M_RD_CLIENT_EVENT_NETWORK_ERROR:
		/* NETWORK_ERROR is our best proxy for "server is overloaded
		 * or unreachable" (5.03 / 5.00 / timeout) since Zephyr LwM2M
		 * doesn't surface CoAP response codes to the app event API.
		 * PRIO 6: double the backoff to back off aggressively when
		 * the server is hot. */
		LOG_ERR("LwM2M network error — applying storm-backoff (2x)");
		lwm2m_connected = false;
		ami_set_rgb(AMI_RGB_RED);
		lwm2m_reg_failures++;
		lwm2m_diag_record_error(-ECONNREFUSED);
		lwm2m_diag_inc_recover_count();   /* Bug B */
		lwm2m_diag_inc_storm_backoff();
		backoff_s = lwm2m_recover_backoff_s(lwm2m_recover_attempt + 1) * 2u;
		if (backoff_s > (uint32_t)CONFIG_AMI_LWM2M_RECOVER_BACKOFF_MAX_S) {
			backoff_s = CONFIG_AMI_LWM2M_RECOVER_BACKOFF_MAX_S;
		}
		LOG_INF("LwM2M engine error: scheduling restart attempt %d/%d in %us "
			"(storm-backoff applied)",
			lwm2m_recover_attempt + 1,
			CONFIG_AMI_LWM2M_RECOVER_MAX_ATTEMPTS, backoff_s);
		k_work_reschedule(&lwm2m_recover_work, K_SECONDS(backoff_s));
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
	lwm2m_set_string(&LWM2M_OBJ(0, 0, 0), lwm2m_server_uri);
	lwm2m_set_u8(&LWM2M_OBJ(0, 0, 2), 3); /* NoSec mode */
	lwm2m_set_u16(&LWM2M_OBJ(0, 0, 10), 101); /* Short Server ID */

	/* Server Object (1) */
	lwm2m_set_u16(&LWM2M_OBJ(1, 0, 0), 101); /* Short Server ID */
	/* Lifetime taken from Kconfig (per-mesh overlay can override).
	 * pi4 default = 300s, r1000 = 120s — see overlays/<mesh>.conf.
	 */
	lwm2m_set_u32(&LWM2M_OBJ(1, 0, 1), CONFIG_LWM2M_ENGINE_DEFAULT_LIFETIME);
	/* Default Min Period (pmin) and Max Period (pmax) for Observe.
	 *
	 * These are the per-server defaults applied to any Observed resource
	 * that does NOT have its own pmin/pmax set via Write-Attributes from
	 * the server. Zephyr's lwm2m_observation engine reads them via
	 * lwm2m_server_get_pmin/pmax (lwm2m_observation.c:361-362).
	 *
	 * We align /1/0/2 with our local throttle floor so behavior is
	 * consistent whether or not the server sends Write-Attributes:
	 *   - server pmin set → MAX(server, local) wins (Zephyr enforces server,
	 *     PUSH_FIELD enforces local — whichever is larger)
	 *   - server pmin unset → /1/0/2 default applies, == local floor
	 * /1/0/3 = 0 → no forced periodic Notify (no heartbeat). The
	 * registration UPDATE every (lifetime - UPDATE_EARLY) keeps the
	 * server aware of liveness independently.
	 */
	lwm2m_set_u32(&LWM2M_OBJ(1, 0, 2),
		      CONFIG_AMI_LWM2M_NOTIFY_MIN_INTERVAL_MS / 1000);
	lwm2m_set_u32(&LWM2M_OBJ(1, 0, 3), 0);

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
	LOG_INF("  Server (DNS-SD):   %s", lwm2m_server_uri);
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
	shell_print(sh, "    server=%s", lwm2m_server_uri);

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
		shell_print(sh, "  server    : %s", lwm2m_server_uri);
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
	ami_reboot_drain(SYS_REBOOT_COLD, "shell-command");
	return 0;
}

static int cmd_ami_rgb(const struct shell *sh, size_t argc, char **argv)
{
	if (argc < 2) {
		shell_error(sh, "Usage: ami rgb <off|red|green|blue|yellow|cyan|magenta|white>");
		return -EINVAL;
	}

	const char *name = argv[1];
	enum ami_rgb_color color = AMI_RGB_OFF;

	if (!strcmp(name, "off")) {
		color = AMI_RGB_OFF;
	} else if (!strcmp(name, "red")) {
		color = AMI_RGB_RED;
	} else if (!strcmp(name, "green")) {
		color = AMI_RGB_GREEN;
	} else if (!strcmp(name, "blue")) {
		color = AMI_RGB_BLUE;
	} else if (!strcmp(name, "yellow")) {
		color = AMI_RGB_YELLOW;
	} else if (!strcmp(name, "cyan")) {
		color = AMI_RGB_CYAN;
	} else if (!strcmp(name, "magenta")) {
		color = AMI_RGB_MAGENTA;
	} else if (!strcmp(name, "white")) {
		color = AMI_RGB_WHITE;
	} else {
		shell_error(sh, "Unknown color '%s'", name);
		return -EINVAL;
	}

	ami_set_rgb(color);
	shell_print(sh, "RGB set to %s (%s)", name, rgb_led_is_ready() ? "WS2812" : "led0 fallback");
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
	SHELL_CMD_ARG(rgb, NULL, "Set status color: rgb <off|red|green|blue|yellow|cyan|magenta|white>", cmd_ami_rgb, 2, 0),
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
#if defined(CONFIG_AMI_MESH_PI4)
	static const uint8_t otbr_tlvs[] = {
		/* UNAL-Thread on Pi4 EKH01 — Ch25, PAN 0x23ED
		 * Mesh-local: fdf5:bffd:0bd6:ef74::/64
		 * Exported via: ssh root@192.168.1.111 'ot-ctl dataset active -x'
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
	const char *mesh_label = "UNAL-Thread (Pi4 EKH01, Ch25)";
	const char *mesh_local_str = "fdf5:bffd:0bd6:ef74::/64";
#elif defined(CONFIG_AMI_MESH_R1000)
	static const uint8_t otbr_tlvs[] = {
		/* UNAL-R1000 on Seeed R1000 — Ch21, PAN 0x41AE
		 * Mesh-local: fdf1:a391:6243:2a67::/64
		 * Exported via: ssh root@192.168.1.175 'ot-ctl dataset active -x'
		 */
		0x0e, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00,
		0x4a, 0x03, 0x00, 0x00, 0x1a, 0x35, 0x06, 0x00, 0x04, 0x00,
		0x1f, 0xff, 0xe0, 0x02, 0x08, 0xb6, 0x83, 0x33, 0xbc, 0x10,
		0x1c, 0x7c, 0x53, 0x07, 0x08, 0xfd, 0xf1, 0xa3, 0x91, 0x62,
		0x43, 0x2a, 0x67, 0x05, 0x10, 0xb3, 0x1c, 0x15, 0x88, 0xa1,
		0xb2, 0xf6, 0x8c, 0x62, 0x24, 0x01, 0x71, 0x1f, 0x73, 0xaf,
		0xd0, 0x01, 0x02, 0x41, 0xae, 0x04, 0x10, 0x6c, 0x9b, 0x91,
		0xf7, 0x1e, 0x59, 0x36, 0x02, 0x2a, 0x86, 0xaf, 0x24, 0xb3,
		0xc9, 0x2c, 0xd3, 0x00, 0x03, 0x00, 0x00, 0x15, 0x03, 0x0a,
		0x55, 0x4e, 0x41, 0x4c, 0x2d, 0x52, 0x31, 0x30, 0x30, 0x30,
		0x0c, 0x04, 0x02, 0xa0, 0xf7, 0x78,
	};
	const char *mesh_label = "UNAL-R1000 (Seeed R1000, Ch21)";
	const char *mesh_local_str = "fdf1:a391:6243:2a67::/64";
#else
#error "No AMI_MESH target selected (set CONFIG_AMI_MESH_PI4=y or CONFIG_AMI_MESH_R1000=y via overlay)"
#endif

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

	LOG_INF("OTBR dataset applied: %s", mesh_label);
	LOG_INF("Mesh-local: %s", mesh_local_str);
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

	LOG_INF("=== AMI LwM2M Node v%s ===", CLIENT_FIRMWARE_VER);
	LOG_INF("Board: %s", CONFIG_BOARD);
	LOG_INF("Network: Thread Ch%d PAN 0x%04X",
		CONFIG_OPENTHREAD_CHANNEL, CONFIG_OPENTHREAD_PANID);

	/* Suppress DLMS/RS485 noise at startup — INF+DBG filtered out. Re-enable via 'ami log verbose' */
	set_dlms_log_level(LOG_LEVEL_WRN);

	/* Suppress cosmetic LwM2M registry re-sync errors at startup (restored after 25 s) */
	suppress_lwm2m_registry_startup_noise();

	/* PRIO 7: settings subsystem + reset cause capture.
	 * Done EARLY so total_resets is persisted before any subsystem could
	 * crash and cause a reset loop with no observability. */
	{
		int s_ret = settings_subsys_init();
		if (s_ret == 0) {
			(void)settings_load_subtree("ami");
		} else {
			LOG_WRN("settings_subsys_init failed: %d", s_ret);
		}
		capture_reset_reason();
	}

	/* PRIO 8: boot watchdog ARMED here, BEFORE Thread attach starts.
	 * Cancelled in REGISTRATION_COMPLETE handler. If the boot path takes
	 * longer than CONFIG_AMI_BOOT_REGISTER_DEADLINE_S to register, the
	 * node sys_reboot WARM to retry the entire boot sequence. */
	if (CONFIG_AMI_BOOT_REGISTER_DEADLINE_S > 0) {
		LOG_INF("Boot watchdog ARMED: %ds deadline to first REGISTER",
			CONFIG_AMI_BOOT_REGISTER_DEADLINE_S);
		k_work_reschedule(&boot_watchdog_work,
				  K_SECONDS(CONFIG_AMI_BOOT_REGISTER_DEADLINE_S));
	}

	/* LED init */
	ami_led_init();
	ami_set_rgb(AMI_RGB_BLUE);

	/* Apply OTBR dataset (mesh-local prefix + PSKc) before Thread attaches */
	apply_otbr_dataset();

	/* Poll OpenThread role until attached (Child/Router/Leader) */
	LOG_INF("Waiting for Thread network...");
	for (int i = 0; i < 120; i++) {
		static bool wait_blink;

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
			ami_set_rgb(AMI_RGB_CYAN);
			break;
		}
		wait_blink = !wait_blink;
		ami_set_rgb(wait_blink ? AMI_RGB_BLUE : AMI_RGB_OFF);
		k_sleep(K_SECONDS(2));
	}

	/* Extra wait for IPv6 address propagation */
	LOG_INF("Extra 5s wait for IPv6 addresses...");
	k_sleep(K_SECONDS(5));

	/* Build unique endpoint name from MAC */
	build_endpoint_name();
	LOG_INF("Endpoint: %s", endpoint_name);

	/* DNS-SD only — resolve LwM2M server via OTBR's SRP/Advertising Proxy.
	 * No Kconfig fallback (deprecated in v0.6.0). On terminal failure
	 * (CONFIG_AMI_LWM2M_DNS_SD_RETRY_MAX consecutive lookups failed),
	 * persist last_error_code = -EHOSTUNREACH and warm-reboot to retry
	 * the entire boot sequence (preserves Thread NVS).
	 */
	if (lwm2m_discover_with_retry(CONFIG_AMI_LWM2M_DNS_SD_RETRY_MAX,
				      CONFIG_AMI_LWM2M_DNS_SD_TIMEOUT_MS) != 0) {
		LOG_ERR("DNS-SD: %d attempts failed; warm-reboot to retry boot",
			CONFIG_AMI_LWM2M_DNS_SD_RETRY_MAX);
		lwm2m_diag_record_error(-EHOSTUNREACH);
		ami_reboot_drain(SYS_REBOOT_WARM, "dns-sd-boot-fail");
		/* unreachable */
	}

	/* Setup LwM2M objects */
	ret = lwm2m_setup();
	if (ret < 0) {
		LOG_ERR("LwM2M setup failed: %d", ret);
		return ret;
	}

	/* Start LwM2M RD client */
	memset(&client_ctx, 0, sizeof(client_ctx));

	/* PRIO 6: initial-register jitter [0..30 s].
	 * Without this, 30 boards power-cycled by an OTBR restart would all
	 * REGISTER within the same ~5 s window → server avalanche. The
	 * jitter spreads them over ~30 s → 1 REGISTER/sec sustained,
	 * which the TB Edge Leshan stack handles cleanly.
	 */
	uint32_t initial_jitter_ms = sys_rand32_get() % 30000U;
	LOG_INF("LwM2M initial REGISTER jitter: %u ms (anti boot-storm)",
		initial_jitter_ms);
	k_sleep(K_MSEC(initial_jitter_ms));

	atomic_inc(&lwm2m_diag_reg_attempts);   /* Object 33000 RID 11 */
	lwm2m_rd_client_start(&client_ctx, endpoint_name, 0,
			      rd_client_event, observe_cb);

	/* Watchdog (PRIO 5): start the system_workq-based liveness check
	 * AFTER initial REGISTER is in flight. Boot grace inside the watchdog
	 * prevents false positives during the first attach + register cycle.
	 */
	lwm2m_watchdog_init();

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
			meter_dump_throttle_stats();   /* v0.20.0 — visibility on suppression rate */
			last_conn_update_ms = now;
		}
	}

	return 0;
}
