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
/* v0.6.24: invoke from this dedicated watchdog thread so the cold reboot
 * is independent of system_workq. ami_reboot_drain handles USB-Serial-JTAG
 * drain + sys_reboot. */
#include <zephyr/sys/reboot.h>
extern void ami_reboot_drain(int reboot_type, const char *reason);

/* Tunables */
#define LWM2M_WATCHDOG_PERIOD_S       60   /* check cadence */
#define LWM2M_WATCHDOG_BOOT_GRACE_S   60   /* don't bark before this uptime */

/* v0.6.24: hard deadline for first REGISTER. If the engine has not produced
 * a single REGISTRATION_COMPLETE / REG_UPDATE_COMPLETE event within this
 * many seconds since boot, the dedicated watchdog thread forces a COLD
 * reboot. This covers the failure mode we observed in production: node
 * boots → REGISTER fails (DNS-SD timeout, mesh re-attach mid-flight, etc.)
 * → engine retries internally, eventually goes silent → lwm2m_recover_work
 * never fires (event handlers depend on engine emitting events) → silence
 * watchdog stays idle (last_emit_uptime == 0) → node sits forever as a
 * mesh-attached zombie. The PRIO 8 boot_watchdog covers the same window
 * but lives on system_workq; if anything stalls that queue we lose its
 * protection too. This dedicated-thread path has no such dependency. */
#define LWM2M_WATCHDOG_FIRST_REGISTER_DEADLINE_S  300

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

		/* v0.6.24: ACTIVE-FROM-BOOT mode.
		 *
		 * Previously the watchdog was idle until the first
		 * lwm2m_watchdog_emit_event() — which only fires on a
		 * server-ACKed REGISTRATION_COMPLETE / REG_UPDATE_COMPLETE.
		 * Field forensics on the 30-node fleet caught the failure case
		 * that mode could not handle: three nodes booted, attempted
		 * REGISTER once, failed silently (no event delivered to the
		 * application layer), and then sat forever as zombies — silence
		 * watchdog stayed idle (last == 0), recover_work never scheduled
		 * (no event triggered it), and the PRIO 8 boot_watchdog (which
		 * also fires SYS_REBOOT_WARM on first-register deadline) is
		 * gated on system_workq which can itself stall. Net result: no
		 * recovery path.
		 *
		 * New behavior:
		 *   - last != 0 (we've been registered at least once):
		 *       silence-since-last-emit must stay below SILENCE_S
		 *       (same as before — 2× lifetime).
		 *   - last == 0 (never registered):
		 *       uptime must stay below FIRST_REGISTER_DEADLINE_S
		 *       (300 s). Past that, force a COLD reboot directly from
		 *       this thread. We use COLD (not WARM) here because the
		 *       hang is pre-REGISTER, so NVS-preserved Thread state is
		 *       not load-bearing — a full kernel reinit is the most
		 *       robust recovery.
		 */
		if (last == 0) {
			if (now_s >= LWM2M_WATCHDOG_FIRST_REGISTER_DEADLINE_S) {
				LOG_ERR("watchdog: %us without first REGISTER ACK "
					"(deadline=%us) — forcing COLD reboot",
					now_s,
					LWM2M_WATCHDOG_FIRST_REGISTER_DEADLINE_S);
				lwm2m_diag_inc_watchdog_count();
				lwm2m_diag_record_error(-ETIMEDOUT);
				/* Independent of system_workq — direct call. */
				ami_reboot_drain(SYS_REBOOT_COLD,
						 "no-first-register");
				/* unreachable */
			}
			LOG_DBG("watchdog: pre-register guard (uptime=%us "
				"deadline=%us)", now_s,
				LWM2M_WATCHDOG_FIRST_REGISTER_DEADLINE_S);
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
