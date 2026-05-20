/*
 * Hardware Watchdog — see hw_watchdog.h for design notes.
 *
 * Two task_wdt channels backed by the TG0_WDT hardware timer:
 *   chan_kernel  fed from a dedicated preemptive thread (prio 4).
 *                Independent of every workq in the system; only a
 *                kernel-wide stall (or a hostile thread starving prio>=4)
 *                can silence it.
 *   chan_workq   fed from a self-rescheduling delayed work on
 *                system_workq. Detects "system_workq is stuck" — which
 *                soft layers cannot recover from because they themselves
 *                schedule recovery on system_workq.
 *
 * On timeout the default task_wdt callback (SYS_RESET via sys_reboot
 * SYS_REBOOT_COLD) fires. We pass a small custom callback that records
 * which channel was the culprit via the diag log, then sys_reboot. If
 * even the kernel timer subsystem is dead, the HW WDT bites at
 * TASK_WDT_MIN_TIMEOUT + TASK_WDT_HW_FALLBACK_DELAY after the missed
 * SW reload — fully independent of any RTOS state.
 */

#include "hw_watchdog.h"

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/watchdog.h>
#include <zephyr/task_wdt/task_wdt.h>
#include <zephyr/sys/reboot.h>
#include <zephyr/logging/log.h>
#include <zephyr/init.h>

LOG_MODULE_REGISTER(hw_wdog, LOG_LEVEL_INF);

#define HW_WDOG_TIMEOUT_MS   (CONFIG_AMI_HW_WATCHDOG_TIMEOUT_S * 1000U)
/* Feed at 1/3 the timeout so we tolerate two missed wakeups. */
#define HW_WDOG_FEED_PERIOD  K_SECONDS(CONFIG_AMI_HW_WATCHDOG_TIMEOUT_S / 3)

#define HW_WDOG_STACK_SZ     768
#define HW_WDOG_PRIO         K_PRIO_PREEMPT(4)

static int chan_kernel = -1;
static int chan_workq  = -1;
/* v0.6.26: boot-survival channel, armed at SYS_INIT(POST_KERNEL) BEFORE
 * main() touches NVS. Fed exactly once from hw_watchdog_note_boot_survived()
 * when main() proves it cleared settings_load + post_mortem. If main() hangs
 * there, this channel never gets that first feed and TG0_WDT bites. After
 * the first feed the kernel feeder keeps it alive (redundant with chan_kernel
 * but harmless). -1 until armed. */
static int chan_boot = -1;
static bool wdt_inited;
/* Set true once main() passes the NVS danger zone; gates the feeder so it
 * keeps chan_boot fed only after the one-shot survival feed. */
static atomic_t boot_survived = ATOMIC_INIT(0);

/* ── Real-liveness gate (v0.6.17) ──────────────────────────────────
 * The original two channels only proved "the kernel scheduler runs" and
 * "system_workq runs". A node whose USB/radio peripheral wedges while the
 * CPU stays healthy keeps feeding both — so the watchdog never bit. Field
 * proof: a node hung ~1.5 h with total_resets unchanged.
 *
 * The fix: gate the kernel-alive channel on a signal that CANNOT be faked
 * by a half-dead node — a server-ACKed LwM2M event (REGISTRATION_COMPLETE
 * / REG_UPDATE_COMPLETE). main.c calls hw_watchdog_note_liveness() from the
 * rd_client_event handler on exactly those two events. If no such event
 * arrives for CONFIG_AMI_REAL_LIVENESS_TIMEOUT_S, the kernel feeder stops
 * feeding and triggers a cold reset directly.
 *
 * last_liveness_uptime == 0 means "never registered yet" -> boot grace,
 * feed unconditionally so the boot path (Thread attach + first REGISTER)
 * is not cut short. The PRIO 8 boot watchdog covers that window instead.
 */
static atomic_t last_liveness_uptime = ATOMIC_INIT(0);

void hw_watchdog_note_liveness(void)
{
	atomic_set(&last_liveness_uptime, (uint32_t)(k_uptime_get() / 1000));
}

/* ── Channel A: kernel-alive feeder thread, gated on real liveness ── */
static void hw_wdog_kernel_thread(void *p1, void *p2, void *p3)
{
	ARG_UNUSED(p1);
	ARG_UNUSED(p2);
	ARG_UNUSED(p3);

	for (;;) {
		k_sleep(HW_WDOG_FEED_PERIOD);

		uint32_t last  = (uint32_t)atomic_get(&last_liveness_uptime);
		uint32_t now_s = (uint32_t)(k_uptime_get() / 1000);

		bool liveness_ok;
		const char *reason = "";
		if (last == 0) {
			/* v0.6.25 HARD BOOT-GRACE CAP.
			 *
			 * Original behavior: while last_liveness_uptime == 0 (never
			 * registered), feed unconditionally — boot watchdog owns
			 * this window. But if boot_watchdog itself stalls (e.g.,
			 * system_workq dead) the node stays here forever. Field
			 * proof v0.6.24: 14/30 nodes dead-on-arrival, watchdog_count
			 * stuck at 0 for hours.
			 *
			 * Cap: past BOOT_GRACE_HARD_S seconds without ANY server-ACK
			 * event, stop feeding chan_kernel — TG0_WDT bites at the
			 * hardware level. Final backstop against every software
			 * recovery being dead. */
			liveness_ok =
				(now_s < CONFIG_AMI_HW_WATCHDOG_BOOT_GRACE_HARD_S);
			reason = "boot-grace";
		} else {
			liveness_ok =
				((now_s - last) < CONFIG_AMI_REAL_LIVENESS_TIMEOUT_S);
			reason = "post-register-silence";
		}

		/* v0.6.26: keep chan_boot alive once main() proved boot survival.
		 * Before that one-shot signal we must NOT feed it — that's the
		 * whole point (a hung NVS load leaves it unfed → TG0_WDT bites). */
		if (chan_boot >= 0 && atomic_get(&boot_survived)) {
			(void)task_wdt_feed(chan_boot);
		}

		if (liveness_ok) {
			if (chan_kernel >= 0) {
				(void)task_wdt_feed(chan_kernel);
			}
			continue;
		}

		/* Liveness fail: either we never registered (boot-grace expired)
		 * or we registered once and the server stopped ACKing us. Reboot
		 * directly — dependency-free, does not go through system_workq
		 * or ami_reboot_drain (either could itself be stuck). If even
		 * sys_reboot stalls, chan_kernel is no longer fed, so the HW WDT
		 * bites within the channel timeout as the final backstop. */
		if (last == 0) {
			LOG_ERR("HW watchdog: %s expired at uptime=%us (hard cap=%us) — "
				"no first REGISTER ever, SOC cold reset",
				reason, now_s,
				CONFIG_AMI_HW_WATCHDOG_BOOT_GRACE_HARD_S);
		} else {
			LOG_ERR("HW watchdog: %s for %us (limit %us) — "
				"node cut off, SOC cold reset",
				reason, now_s - last,
				CONFIG_AMI_REAL_LIVENESS_TIMEOUT_S);
		}
		sys_reboot(SYS_REBOOT_COLD);
	}
}

K_THREAD_STACK_DEFINE(hw_wdog_stack, HW_WDOG_STACK_SZ);
static struct k_thread hw_wdog_thread_data;

/* ── Channel B: workq-alive feeder (delayed work) ──────────────── */
static void hw_wdog_workq_fn(struct k_work *w);
static K_WORK_DELAYABLE_DEFINE(hw_wdog_workq, hw_wdog_workq_fn);

static void hw_wdog_workq_fn(struct k_work *w)
{
	ARG_UNUSED(w);
	if (chan_workq >= 0) {
		(void)task_wdt_feed(chan_workq);
	}
	k_work_reschedule(&hw_wdog_workq, HW_WDOG_FEED_PERIOD);
}

/* ── Timeout callback ──────────────────────────────────────────── */
static void hw_wdog_timeout(int channel_id, void *user_data)
{
	const char *name = (const char *)user_data;
	/* Best-effort log; if logging is dead, the next sys_reboot still
	 * fires. The HW WDT will bite anyway after FALLBACK_DELAY if even
	 * sys_reboot stalls. */
	LOG_ERR("HW watchdog BIT: channel=%d (%s) silent > %us — cold reboot",
		channel_id, name ? name : "?",
		CONFIG_AMI_HW_WATCHDOG_TIMEOUT_S);
	sys_reboot(SYS_REBOOT_COLD);
}

/* ── v0.6.26: earliest-possible HW watchdog arming ──────────────────
 * Runs at POST_KERNEL — BEFORE main() and therefore before main()'s
 * settings_subsys_init / settings_load / post_mortem deserialization.
 * Arms task_wdt + a single "boot" channel. That channel is intentionally
 * left UNFED here: main() must call hw_watchdog_note_boot_survived() to
 * feed it the first time. A hang in the NVS path → channel never fed →
 * TG0_WDT bites. This is the only watchdog layer that covers code running
 * before hw_watchdog_init().
 */
static int hw_watchdog_boot_arm(void)
{
	const struct device *wdt = DEVICE_DT_GET(DT_NODELABEL(wdt0));
	if (!device_is_ready(wdt)) {
		/* Can't log reliably this early on all backends; main() will
		 * also try to init and will log there. */
		return 0;
	}

	int ret = task_wdt_init(wdt);
	if (ret < 0) {
		return 0;
	}
	wdt_inited = true;

	chan_boot = task_wdt_add(HW_WDOG_TIMEOUT_MS, hw_wdog_timeout,
				 (void *)"boot");
	return 0;
}
SYS_INIT(hw_watchdog_boot_arm, POST_KERNEL, 90);

void hw_watchdog_note_boot_survived(void)
{
	/* One-shot: feed chan_boot now (proves NVS path cleared), then let
	 * the kernel feeder keep it alive. */
	if (chan_boot >= 0) {
		(void)task_wdt_feed(chan_boot);
	}
	atomic_set(&boot_survived, 1);
	LOG_INF("HW watchdog: boot survival confirmed (chan_boot=%d fed)",
		chan_boot);
}

/* ── Public init ───────────────────────────────────────────────── */
void hw_watchdog_init(void)
{
	const struct device *wdt = DEVICE_DT_GET(DT_NODELABEL(wdt0));
	if (!device_is_ready(wdt)) {
		LOG_ERR("wdt0 not ready — HW watchdog NOT armed");
		return;
	}

	/* task_wdt may already be initialized by hw_watchdog_boot_arm()
	 * (SYS_INIT POST_KERNEL). Re-initializing would re-arm the HW timer
	 * and double-install — skip it in that case. */
	if (!wdt_inited) {
		int ret = task_wdt_init(wdt);
		if (ret < 0) {
			LOG_ERR("task_wdt_init failed: %d — HW watchdog NOT armed", ret);
			return;
		}
		wdt_inited = true;
	}

	chan_kernel = task_wdt_add(HW_WDOG_TIMEOUT_MS, hw_wdog_timeout,
				   (void *)"kernel");
	chan_workq  = task_wdt_add(HW_WDOG_TIMEOUT_MS, hw_wdog_timeout,
				   (void *)"workq");
	if (chan_kernel < 0 || chan_workq < 0) {
		LOG_ERR("task_wdt_add failed (k=%d w=%d) — HW watchdog DEGRADED",
			chan_kernel, chan_workq);
		/* Don't return: even one channel armed is better than none. */
	}

	/* Spawn the kernel-alive feeder thread. */
	k_thread_create(&hw_wdog_thread_data, hw_wdog_stack,
			K_THREAD_STACK_SIZEOF(hw_wdog_stack),
			hw_wdog_kernel_thread, NULL, NULL, NULL,
			HW_WDOG_PRIO, 0, K_NO_WAIT);
	k_thread_name_set(&hw_wdog_thread_data, "hw_wdog");

	/* Arm the workq feeder. */
	k_work_reschedule(&hw_wdog_workq, HW_WDOG_FEED_PERIOD);

	LOG_INF("HW watchdog armed: timeout=%us, channels=[kernel=%d workq=%d], "
		"feed_period=%us",
		CONFIG_AMI_HW_WATCHDOG_TIMEOUT_S, chan_kernel, chan_workq,
		CONFIG_AMI_HW_WATCHDOG_TIMEOUT_S / 3);
}
