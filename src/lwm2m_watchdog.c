/*
 * LwM2M Liveness Watchdog — see header for design notes.
 *
 * v0.5.0: dedicated preemptive thread (K_PRIO_PREEMPT(7)) instead of
 * system_workq. Rationale: if the system_workq is itself blocked (deadlock,
 * starvation, long-running item), a watchdog scheduled on it cannot fire.
 * A dedicated thread with its own stack is independent of every workq in
 * the system; only a kernel-wide stall can silence it.
 */

#include "lwm2m_watchdog.h"

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/atomic.h>

LOG_MODULE_REGISTER(lwm2m_wdog, LOG_LEVEL_INF);

/* Forward decls into main.c */
extern struct k_work_delayable lwm2m_recover_work;
extern void lwm2m_diag_inc_watchdog_count(void);   /* defined in main.c */
extern void lwm2m_diag_inc_recover_count(void);    /* Bug B */
extern void lwm2m_diag_record_error(int err);

/* Tunables */
#define LWM2M_WATCHDOG_PERIOD_S       60   /* check cadence */
#define LWM2M_WATCHDOG_BOOT_GRACE_S   60   /* don't bark before this uptime */

#define LWM2M_WATCHDOG_STACK_SZ       1024
/* Cooperative thread (negative prio) cannot be preempted by other coop
 * threads but can preempt preemptive ones. Pick a preemptive priority so
 * it yields to interrupts but still runs ahead of regular workq workers
 * (system_workq runs at COOP by default; preemptive prio 7 is high enough
 * to not be starved by application threads).
 */
#define LWM2M_WATCHDOG_PRIO           K_PRIO_PREEMPT(7)

/* uptime_s of the most recent successful emit. 0 = never */
static atomic_t last_emit_uptime = ATOMIC_INIT(0);

uint32_t lwm2m_watchdog_get_last_emit_uptime(void)
{
	return (uint32_t)atomic_get(&last_emit_uptime);
}

void lwm2m_watchdog_emit_event(void)
{
	atomic_set(&last_emit_uptime, (uint32_t)(k_uptime_get() / 1000));
}

/* Computed silence threshold = 2 × default lifetime. e.g. 240s for r1000
 * (lifetime=120). Generous: Updates expected at ~0.8 × lifetime, plus
 * normal CoAP retransmit budget.
 */
#define LWM2M_WATCHDOG_SILENCE_S \
	(2 * CONFIG_LWM2M_ENGINE_DEFAULT_LIFETIME)

static void lwm2m_watchdog_thread(void *p1, void *p2, void *p3)
{
	ARG_UNUSED(p1);
	ARG_UNUSED(p2);
	ARG_UNUSED(p3);

	LOG_INF("LwM2M watchdog thread up: period=%us, silence_threshold=%us, "
		"boot_grace=%us",
		LWM2M_WATCHDOG_PERIOD_S, LWM2M_WATCHDOG_SILENCE_S,
		LWM2M_WATCHDOG_BOOT_GRACE_S);

	for (;;) {
		k_sleep(K_SECONDS(LWM2M_WATCHDOG_PERIOD_S));

		uint32_t now_s = (uint32_t)(k_uptime_get() / 1000);
		uint32_t last  = (uint32_t)atomic_get(&last_emit_uptime);

		/* Watchdog idle until first liveness signal arrives.
		 * lwm2m_watchdog_emit_event() is called from REGISTRATION_COMPLETE
		 * (REG_UPDATE_COMPLETE) and from each PUSH_FIELD success. Until
		 * the engine has registered at least once, there's nothing to
		 * watch — the boot path may legitimately take >60s under mesh
		 * attach contention, and complaining before first REGISTER would
		 * be a false positive.
		 *
		 * Boot grace remains as a redundant lower bound for first emit.
		 */
		if (last == 0) {
			LOG_DBG("watchdog: idle (no emit yet, uptime=%us)", now_s);
			continue;
		}
		if (now_s < LWM2M_WATCHDOG_BOOT_GRACE_S) {
			continue;
		}

		uint32_t silence = now_s - last;

		if (silence > LWM2M_WATCHDOG_SILENCE_S) {
			LOG_WRN("watchdog: %us since last emit (max=%us). "
				"Forcing recover.",
				silence, LWM2M_WATCHDOG_SILENCE_S);
			lwm2m_diag_inc_watchdog_count();
			lwm2m_diag_inc_recover_count();   /* Bug B: count cycle */
			/* errno: -ESHUTDOWN as a watchdog-canonical signal that
			 * this cycle was started by silence detection, not by an
			 * engine event. Operator can correlate via watchdog_count. */
			lwm2m_diag_record_error(-ESHUTDOWN);
			/* Submit recovery work; recover_work_fn handles
			 * backoff + escalation. The work is scheduled on the
			 * system_workq, but our detection above is independent
			 * of it.
			 */
			k_work_reschedule(&lwm2m_recover_work, K_NO_WAIT);
		} else {
			LOG_DBG("watchdog: silence=%us (max=%us) — healthy",
				silence, LWM2M_WATCHDOG_SILENCE_S);
		}
	}
}

K_THREAD_STACK_DEFINE(lwm2m_wdog_stack, LWM2M_WATCHDOG_STACK_SZ);
static struct k_thread lwm2m_wdog_thread_data;

void lwm2m_watchdog_init(void)
{
	k_thread_create(&lwm2m_wdog_thread_data,
			lwm2m_wdog_stack,
			K_THREAD_STACK_SIZEOF(lwm2m_wdog_stack),
			lwm2m_watchdog_thread,
			NULL, NULL, NULL,
			LWM2M_WATCHDOG_PRIO,
			0,
			K_NO_WAIT);
	k_thread_name_set(&lwm2m_wdog_thread_data, "lwm2m_wdog");
}
