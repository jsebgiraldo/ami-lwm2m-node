/*
 * CoAP-level keepalive — see header for design notes.
 *
 * Why notify on TD_UPTIME_S_RID rather than introduce a new resource:
 *   - It's already declared observable on TB Edge (we depend on this
 *     for liveness in tools/tb_edge_monitoring_setup.py OBSERVE_ADDITIONS).
 *   - conn_monitor already updates it every ~60s, so the value is
 *     non-stale; we just bump the notify cadence floor to 300s without
 *     duplicating state.
 *   - Engine pmin/pmax write-attributes on /33000/0/10 (typical pmin=30,
 *     pmax=300 from TB Edge) won't suppress us — we fire exactly at pmax.
 *
 * Boot grace: wait 60s before the first keepalive so the LwM2M engine
 * has time to complete REGISTER. If we fire before REGISTER ACK, the
 * notify is silently dropped (no observer to notify against), which is
 * fine but pollutes early-boot logs.
 */

#include "coap_keepalive.h"

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/lwm2m.h>
#include <zephyr/sys/atomic.h>

#include "lwm2m_obj_thread_diag.h"
#include "lwm2m_watchdog.h"
#include "hw_watchdog.h"

LOG_MODULE_REGISTER(coap_keepalive, LOG_LEVEL_INF);

#define KEEPALIVE_PERIOD_S    CONFIG_AMI_COAP_KEEPALIVE_PERIOD_S
#define KEEPALIVE_BOOT_GRACE_S 60
/* v0.6.69: was 1024 — code-review audit caught the same pattern that
 * bit lwm2m_wdog in v0.6.63: LOG_WRN/LOG_ERR with %s/%u args triggers
 * cbprintf_package_convert recursion needing ~3 KB headroom. First fail
 * burst (~270 s after socket dies) overflowed the 1024-byte stack and
 * silently killed the keepalive thread → board sat as zombie because
 * no further notifies attempted → silence watchdog never saw heartbeat
 * absence. This was a candidate root cause of the 8/30 stuck boards in
 * v0.6.67's 2h soak. 4096 mirrors lwm2m_wdog headroom. */
#define KEEPALIVE_STACK_SZ    4096
/* Preemptive prio — not coop — so it cannot starve LwM2M engine threads.
 * Priority chosen well below lwm2m_wdog (PREEMPT 7) so the silence
 * watchdog still has precedence if it ever decides to act. */
#define KEEPALIVE_PRIO        K_PRIO_PREEMPT(9)

/* v0.6.68: when notify_observer() fails N times in a row, the engine is
 * almost certainly in a stuck state (ECANCELED, socket dead, etc.). Fire
 * recover_work directly instead of waiting for the silence watchdog.
 * N=3 chosen to match the engine's internal CoAP CON retransmit budget. */
#define KEEPALIVE_FAIL_THRESHOLD 3

/* Diag — increment on every keepalive emit so the operator can verify
 * via Object 33000 that the worker is actually running. */
static atomic_t keepalive_emit_count = ATOMIC_INIT(0);
/* v0.6.68: consecutive failed notify_observer() returns. Reset on success. */
static atomic_t keepalive_consec_fail = ATOMIC_INIT(0);

/* Hook into main.c's recover_work — same path the silence watchdog uses. */
extern struct k_work_delayable lwm2m_recover_work;
extern void lwm2m_diag_record_error(int err);

uint32_t coap_keepalive_get_emit_count(void)
{
	return (uint32_t)atomic_get(&keepalive_emit_count);
}

uint32_t coap_keepalive_get_consec_fail(void)
{
	return (uint32_t)atomic_get(&keepalive_consec_fail);
}

static void keepalive_thread_fn(void *p1, void *p2, void *p3)
{
	ARG_UNUSED(p1); ARG_UNUSED(p2); ARG_UNUSED(p3);

	LOG_INF("CoAP keepalive thread up: period=%us, grace=%us, "
		"target=/33000/0/%u (uptime_s)",
		KEEPALIVE_PERIOD_S, KEEPALIVE_BOOT_GRACE_S,
		TD_UPTIME_S_RID);

	/* Don't fight the boot REGISTER. */
	k_sleep(K_SECONDS(KEEPALIVE_BOOT_GRACE_S));

	for (;;) {
		/* Notify the engine that /33000/0/uptime_s "changed" — engine
		 * will queue a CoAP CON message. If the socket is dead the
		 * engine's notify_message_timeout_cb() fires after 3 retries
		 * and calls lwm2m_rd_client_timeout(), which kicks the state
		 * machine back to ENGINE_DO_REGISTRATION. Our recover_work in
		 * main.c rides the same state machine.
		 */
		int ret = lwm2m_notify_observer(THREAD_DIAG_OBJECT_ID, 0,
						TD_UPTIME_S_RID);
		atomic_inc(&keepalive_emit_count);

		if (ret >= 0) {
			/* v0.6.75: ret is the COUNT of observers notified, NOT a
			 * 0=OK status. lwm2m_notify_observer_path() returns the
			 * number of matching observers (0 = none) or <0 on a real
			 * engine error. The v0.6.68 code tested `ret == 0` as
			 * "queued OK", which INVERTED the logic: a board that IS
			 * being observed on /33000/0/uptime_s returns ret>0 and was
			 * mis-counted as a failure → 3 strikes → forced recover →
			 * COLD reboot every ~12 min (3 × keepalive_period after the
			 * 60 s boot grace). Boards with NO observer (ret==0, e.g.
			 * minimal builds without Object 33000) were the only ones
			 * that survived — the perverse opposite of what we want.
			 * Forensic proof: 1494 (Object 33000 + observed) looped at
			 * uptime≈706 s / reg_age≈660 s with heap 100 % free; f7b4/
			 * fbb8 (minimal, ret==0) stayed up. ret>=0 means the engine
			 * processed the notify (alive) — feed the watchdog. */
			lwm2m_watchdog_emit_event();
			atomic_set(&keepalive_consec_fail, 0);
			/* v0.7.9: ret>0 = REAL observers were notified, so telemetry
			 * is actually being delivered (not just queued). Feed the
			 * inbound delivery-liveness gate. ret==0 = registered but
			 * NOBODY is observing us — the stuck-session signature — so we
			 * deliberately do NOT feed it: after
			 * CONFIG_AMI_DELIVERY_LIVENESS_TIMEOUT_S the HW feeder cold-
			 * resets, forcing a fresh REGISTER that re-establishes observes.
			 * The note_delivery() latch (ever_had_observer) means a build
			 * that is never observed never arms this gate. */
			if (ret > 0) {
				hw_watchdog_note_delivery();
			}
			LOG_DBG("keepalive: notify ok, %d observer(s) (cnt=%u)",
				ret, (uint32_t)atomic_get(&keepalive_emit_count));
		} else {
			/* v0.6.68: real engine error (ret < 0, e.g. -ENOMEM /
			 * socket dead). Count consecutive fails; after
			 * KEEPALIVE_FAIL_THRESHOLD, kick recover directly. This
			 * catches stuck states the silence watchdog cannot see fast
			 * enough (engine internally cancelled / socket dead). */
			int fails = (int)atomic_inc(&keepalive_consec_fail) + 1;
			LOG_WRN("keepalive: notify failed ret=%d "
				"(consec=%d/%d, cnt=%u)",
				ret, fails, KEEPALIVE_FAIL_THRESHOLD,
				(uint32_t)atomic_get(&keepalive_emit_count));
			if (fails >= KEEPALIVE_FAIL_THRESHOLD) {
				LOG_ERR("keepalive: %d consecutive failures — "
					"forcing recover_work", fails);
				lwm2m_diag_record_error(ret);
				atomic_set(&keepalive_consec_fail, 0);
				k_work_reschedule(&lwm2m_recover_work,
						  K_NO_WAIT);
			}
		}

		k_sleep(K_SECONDS(KEEPALIVE_PERIOD_S));
	}
}

K_THREAD_STACK_DEFINE(keepalive_stack, KEEPALIVE_STACK_SZ);
static struct k_thread keepalive_thread_data;

void coap_keepalive_init(void)
{
	k_thread_create(&keepalive_thread_data,
			keepalive_stack,
			K_THREAD_STACK_SIZEOF(keepalive_stack),
			keepalive_thread_fn,
			NULL, NULL, NULL,
			KEEPALIVE_PRIO,
			0,
			K_NO_WAIT);
	k_thread_name_set(&keepalive_thread_data, "coap_keepalive");
}
