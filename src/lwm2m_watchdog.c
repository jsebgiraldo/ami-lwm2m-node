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
#include <zephyr/random/random.h>

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

/* v0.6.30: anti reboot-storm — persisted count of consecutive boots that
 * failed to reach a first REGISTER ACK (defined in main.c). */
extern uint32_t lwm2m_diag_get_noreg_boots(void);
extern void lwm2m_diag_inc_noreg_boots(void);

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

/* v0.6.30: anti reboot-storm tunables. The base 300 s deadline above is the
 * FIRST-boot value; subsequent consecutive failures back it off exponentially
 * (capped) and add per-node jitter so a fleet-wide server-side outage does NOT
 * make all nodes cold-reboot in lockstep every 300 s. After
 * NOREG_MAX_REBOOTS consecutive no-register boots we stop rebooting entirely
 * and stay mesh-attached, nudging the recover path instead — rebooting cannot
 * fix a server outage and only wears flash (it bricked a node in the field). */
#define LWM2M_WATCHDOG_NOREG_MAX_REBOOTS   5
#define LWM2M_WATCHDOG_NOREG_BACKOFF_CAP_S 3600

/* v0.6.63 Bug C fix: was 1024. lwm2m_wdog overflowed at ~5-6 min uptime on
 * all 5 v0.6.62 boards. Thread-analyzer reported 42% steady-state (432/1024)
 * but peak during LOG_INF with %s args triggers cbprintf_package_convert
 * recursion that needs ~3 KB headroom. 4096 matches main-thread stack and
 * the v0.6.48 ot_radio_workq bump. */
#define LWM2M_WATCHDOG_STACK_SZ       4096
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

/* v0.6.74: silence threshold lowered to catch the sub-15-min wedge cycle.
 *
 * v0.6.68 set floor at 900s (15 min) thinking the failure mode was minutes-
 * long deadlocks. The v0.6.73 soak (7 boards, 2h, HUB-induced brownouts)
 * exposed a faster failure: the LwM2M engine enters a CON-timeout cycle
 * that lasts ~10 min before self-recovering via lwm2m_rd_client_timeout()
 * → ENGINE_DO_REGISTRATION. While in this cycle:
 *   - conn_monitor_last_tick keeps advancing (thread alive)
 *   - notify_observer returns 0 (queue accepted)
 *   - REG_UPDATE feeds last_emit_uptime (silence watchdog NEVER fires
 *     because last_emit advances during the natural cycle)
 *   - BUT actual CoAP CON ACKs don't arrive → TB telemetry frozen
 *
 * Net effect: 50% TB-visibility downtime as the cycle repeats. NOT 24/7-
 * ready. v0.6.74 lowers the floor to 480s (8 min) so silence_watchdog
 * preempts the natural 10-min engine cycle and forces a faster recovery
 * via recover_work_fn. Trade-off: slightly higher CON re-transmit traffic;
 * acceptable for the gain in data freshness.
 *
 * Boards in stable steady state still see last_emit refreshed every 300s
 * (keepalive period); 480s gives 60% headroom over one keepalive cycle,
 * so false positives stay rare. Adjust upward (e.g. 720s = 12 min) if
 * production logs show spurious silence_watchdog firings.
 */
#define LWM2M_WATCHDOG_SILENCE_S                                      \
	(((CONFIG_AMI_COAP_KEEPALIVE_PERIOD_S) * 3U) > 480U             \
	 ? ((CONFIG_AMI_COAP_KEEPALIVE_PERIOD_S) * 3U)                  \
	 : 480U)

static void lwm2m_watchdog_thread(void *p1, void *p2, void *p3)
{
	ARG_UNUSED(p1);
	ARG_UNUSED(p2);
	ARG_UNUSED(p3);

	/* v0.6.30: compute the (backed-off + jittered) first-register deadline
	 * ONCE at boot, from the persisted consecutive-no-register count.
	 * Multiplier sequence by noreg_boots: 1,2,4,8,12 (then held), i.e.
	 * 300s, 600s, 1200s, 2400s, 3600s(capped). Jitter adds 0..25%. */
	uint32_t noreg = lwm2m_diag_get_noreg_boots();
	uint32_t mult = (noreg >= 4U) ? 12U : (1U << noreg);
	uint32_t first_reg_deadline = LWM2M_WATCHDOG_FIRST_REGISTER_DEADLINE_S * mult;
	if (first_reg_deadline > LWM2M_WATCHDOG_NOREG_BACKOFF_CAP_S) {
		first_reg_deadline = LWM2M_WATCHDOG_NOREG_BACKOFF_CAP_S;
	}
	first_reg_deadline += sys_rand32_get() % (first_reg_deadline / 4U + 1U);

	LOG_INF("LwM2M watchdog thread up: period=%us, silence_threshold=%us, "
		"boot_grace=%us, first_register_deadline=%us (noreg_boots=%u)",
		LWM2M_WATCHDOG_PERIOD_S, LWM2M_WATCHDOG_SILENCE_S,
		LWM2M_WATCHDOG_BOOT_GRACE_S, first_reg_deadline, noreg);

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
			if (now_s >= first_reg_deadline) {
				/* v0.6.30: once we've cold-rebooted enough times
				 * without ever registering, stop — the cause is
				 * almost certainly server-side (e.g. the Edge SRP
				 * service not advertised after a power event), and
				 * rebooting in a fleet-wide loop only wears flash.
				 * Stay mesh-attached and keep nudging the recover
				 * path; reset_noreg_boots() on the eventual first
				 * REGISTER restores the base deadline. */
				if (lwm2m_diag_get_noreg_boots() >=
				    LWM2M_WATCHDOG_NOREG_MAX_REBOOTS) {
					LOG_WRN("watchdog: %u consecutive no-register "
						"boots — NOT rebooting (likely "
						"server-side outage); staying up, "
						"nudging recover.",
						lwm2m_diag_get_noreg_boots());
					k_work_reschedule(&lwm2m_recover_work,
							  K_NO_WAIT);
					continue;
				}
				LOG_ERR("watchdog: %us without first REGISTER ACK "
					"(deadline=%us, noreg_boots=%u) — forcing "
					"COLD reboot", now_s, first_reg_deadline,
					lwm2m_diag_get_noreg_boots());
				lwm2m_diag_inc_watchdog_count();
				lwm2m_diag_inc_noreg_boots();  /* persist backoff */
				lwm2m_diag_record_error(-ETIMEDOUT);
				/* Independent of system_workq — direct call. */
				ami_reboot_drain(SYS_REBOOT_COLD,
						 "no-first-register");
				/* unreachable */
			}
			LOG_DBG("watchdog: pre-register guard (uptime=%us "
				"deadline=%us)", now_s, first_reg_deadline);
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
