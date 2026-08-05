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
#include <zephyr/random/random.h>

LOG_MODULE_REGISTER(hw_wdog, LOG_LEVEL_INF);

#define HW_WDOG_TIMEOUT_MS   (CONFIG_AMI_HW_WATCHDOG_TIMEOUT_S * 1000U)
/* Feed at 1/3 the timeout so we tolerate two missed wakeups. */
#define HW_WDOG_FEED_PERIOD  K_SECONDS(CONFIG_AMI_HW_WATCHDOG_TIMEOUT_S / 3)

/* v0.6.69: was 768 — code-review audit flagged this as the last line of
 * defense and noted that hw_wdog_kernel_thread emits LOG_ERR with multiple
 * arguments on the liveness-fail path. cbprintf_package_convert recursion
 * during that LOG_ERR could overflow 768 B and crash the thread before
 * sys_reboot fires — the only fallback would then be the HW TG0_WDT
 * timeout. Bumped to 2048 to give cbprintf headroom while staying lean. */
#define HW_WDOG_STACK_SZ     2048
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

/* ── Delivery-liveness gate (v0.7.9) ───────────────────────────────
 * The REG-based gate above proves the OUTBOUND path (server ACKs our
 * REGISTER/REG_UPDATE). It is fooled by the "stuck session" failure mode:
 * a node stays registered (REG_UPDATE keeps round-tripping → gate fed,
 * total_resets frozen) while its observe session is dead — zero observers,
 * so it delivers no telemetry for hours and never self-reboots. Field proof
 * 2026-06-19 (Lab 17): only a manual power-cycle cleared it.
 *
 * This gate proves the INBOUND/delivery path. coap_keepalive calls
 * hw_watchdog_note_delivery() ONLY when lwm2m_notify_observer() returns >0
 * (real observers notified). The first such call latches ever_had_observer;
 * thereafter, if no delivery for CONFIG_AMI_DELIVERY_LIVENESS_TIMEOUT_S
 * (+ per-node jitter), the kernel feeder triggers a cold reset → fresh
 * REGISTER re-establishes the observes. A build that is never observed
 * never latches → never penalized (avoids the v0.6.75 inversion bug).
 */
static atomic_t last_delivery_uptime = ATOMIC_INIT(0);
static atomic_t ever_had_observer    = ATOMIC_INIT(0);

/* v0.7.9 M1: escalation budget for the delivery-stall path. The gate does NOT
 * cold-reboot as a first response — it first nudges a SOFT re-REGISTER (which
 * re-establishes observes with no SoC reset / Thread re-attach), then allows a
 * CAPPED number of cold reboots, then stops (a server-side outage cannot be
 * fixed by rebooting). Mirrors lwm2m_watchdog.c's NOREG_MAX_REBOOTS so this
 * gate can never drive an unbounded fleet reboot+REGISTER storm. Reset on any
 * real delivery so a future stall gets the full sequence again. */
static atomic_t dlv_soft_tries   = ATOMIC_INIT(0);   /* per-episode, in-RAM */
#define DLV_SOFT_TRIES_MAX   2U
#define DLV_HARD_REBOOTS_MAX 5U

/* recover_work lives in main.c; coap_keepalive.c already drives it. The soft
 * step is a stop/start re-REGISTER; recover_work_fn itself gates on Thread
 * role >= CHILD, so a detached node will not storm-reattach from here. */
extern struct k_work_delayable lwm2m_recover_work;
/* Persisted delivery-stall reboot budget (defined in main.c, NVS-backed).
 * Survives the cold reboot so the cap is meaningful ACROSS boots — an in-RAM
 * counter would reset every boot and never cap. The sustained-stable marker in
 * main.c (same one that clears boot_burst) resets it; brief observe-then-drop
 * flaps never reach that marker, so they accumulate toward the cap. */
extern uint32_t lwm2m_diag_get_dlv_reboots(void);
extern void lwm2m_diag_inc_dlv_reboots(void);
extern void lwm2m_diag_reset_dlv_reboots(void);

/* v0.7.18: stamp WHICH watchdog path rebooted us (Object 33000 RID 37).
 *
 * The four sys_reboot() calls below deliberately bypass ami_reboot_drain() —
 * it sleeps, and by definition we get here when something is already wedged.
 * ami_reboot_set_tag() carries none of that risk: it is two plain stores into
 * a __noinit struct, no workqueue, no logging, no scheduler interaction. The
 * panic handler (main.c) already calls it with interrupts possibly disabled.
 *
 * Without this, all four paths land in RID 37 == 0, which is the same bucket
 * as a real power loss — measured 2026-08-05: 27 of 35 live nodes sat in that
 * bucket, making "the node hung" indistinguishable from "the supply died". */
extern void ami_reboot_set_tag(uint32_t code);
#define AMI_RBOOT_HWWDT_BOOT_GRACE   12   /* boot grace expired, never REGISTERed */
#define AMI_RBOOT_HWWDT_DELIVERY     13   /* registered but ZERO observers */
#define AMI_RBOOT_HWWDT_SILENCE      14   /* server stopped ACKing REG_UPDATE */
#define AMI_RBOOT_HWWDT_CHANNEL      15   /* a task_wdt channel went silent */

/* Uptime (s) at which the current UNBROKEN real-delivery streak began; 0 = no
 * streak running. Set by note_delivery on the first delivery of a streak;
 * cleared by the kernel thread on ANY liveness fail (incl. a delivery-stall).
 * Once a streak outlasts one gate deadline, the persisted reboot cap is reset —
 * the board has genuinely recovered DELIVERY (not just registration), which is
 * the only signal that may clear the cap. */
static atomic_t delivery_streak_start = ATOMIC_INIT(0);

void hw_watchdog_note_delivery(void)
{
	uint32_t now_s = (uint32_t)(k_uptime_get() / 1000);
	atomic_set(&last_delivery_uptime, now_s);
	atomic_set(&ever_had_observer, 1);
	/* Start a streak only if none is running (a prior stall cleared it). Don't
	 * overwrite an in-progress streak: we want elapsed time since the FIRST
	 * delivery of the current healthy run. */
	(void)atomic_cas(&delivery_streak_start, 0, (atomic_val_t)now_s);
	/* Real delivery resumed → refresh the per-episode SOFT budget only. The
	 * HARD (persisted) budget is cleared by the kernel thread after a SUSTAINED
	 * streak, NOT on a single delivery, so a brief flap can't dodge the cap. */
	atomic_set(&dlv_soft_tries, 0);
}

/* ── Channel A: kernel-alive feeder thread, gated on real liveness ── */
static void hw_wdog_kernel_thread(void *p1, void *p2, void *p3)
{
	ARG_UNUSED(p1);
	ARG_UNUSED(p2);
	ARG_UNUSED(p3);

	/* v0.7.9 M2: floor the deadline at >= 3 keepalive periods so the gate can
	 * NEVER out-run the feeder that is supposed to satisfy it — a deadline
	 * below the keepalive cadence would cold-reboot a perfectly healthy
	 * observed node every cycle. Mirrors lwm2m_watchdog.c's max(period*3, 480)
	 * floor. The BUILD_ASSERT makes a bad overlay loud at build time; the MAX()
	 * is the runtime belt-and-suspenders. */
	BUILD_ASSERT(CONFIG_AMI_DELIVERY_LIVENESS_TIMEOUT_S >=
		     3 * CONFIG_AMI_COAP_KEEPALIVE_PERIOD_S,
		     "AMI_DELIVERY_LIVENESS_TIMEOUT_S must be >= 3 keepalive periods "
		     "or the delivery gate reboot-loops every observed node");
	const uint32_t dlv_base =
		MAX((uint32_t)CONFIG_AMI_DELIVERY_LIVENESS_TIMEOUT_S,
		    3U * (uint32_t)CONFIG_AMI_COAP_KEEPALIVE_PERIOD_S);
	/* Wide per-node jitter (0..100% of base) so a fleet-wide Edge restart does
	 * NOT collapse 30 recovery actions into one narrow window (a synchronized
	 * REGISTER burst is itself an OTBR address-resolution stressor). */
	const uint32_t delivery_deadline_s =
		dlv_base + (sys_rand32_get() % (dlv_base + 1U));

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
			/* OUTBOUND gate: server still ACKs our REGISTER/REG_UPDATE. */
			bool reg_ok =
				((now_s - last) < CONFIG_AMI_REAL_LIVENESS_TIMEOUT_S);

			/* INBOUND/delivery gate (v0.7.9): only enforced once we have
			 * proven this node IS observed (first ret>0). Until then the
			 * latch is clear and delivery_ok stays true — minimal/never-
			 * observed builds are never penalized. */
			bool delivery_ok = true;
			if (atomic_get(&ever_had_observer)) {
				uint32_t ld = (uint32_t)atomic_get(&last_delivery_uptime);
				delivery_ok = ((now_s - ld) < delivery_deadline_s);
			}

			liveness_ok = reg_ok && delivery_ok;
			reason = !reg_ok ? "post-register-silence"
					 : "delivery-stall";
		}

		/* v0.6.26: keep chan_boot alive once main() proved boot survival.
		 * Before that one-shot signal we must NOT feed it — that's the
		 * whole point (a hung NVS load leaves it unfed → TG0_WDT bites). */
		if (chan_boot >= 0 && atomic_get(&boot_survived)) {
			(void)task_wdt_feed(chan_boot);
		}

		if (liveness_ok) {
			/* v0.7.9 M1: clear the persisted reboot cap ONLY after a streak of
			 * CONTINUOUS real delivery outlasting one gate deadline. Two guards
			 * make "continuous" real, not coasting:
			 *   - GAP guard: delivery_ok tolerates a stale last_delivery for up
			 *     to a full (wide) deadline, but a real delivery GAP > 2 keepalive
			 *     periods means we are NOT actually delivering now — break the
			 *     streak so a brief-deliver-then-go-quiet board (still inside the
			 *     delivery_ok grace) can never clear the cap.
			 *   - AGE guard: streak measured from the FIRST delivery of the run.
			 * A still-stalled board re-arms last_delivery only via soft tries,
			 * which run on the fail path where streak is already 0. */
			uint32_t ld     = (uint32_t)atomic_get(&last_delivery_uptime);
			uint32_t streak = (uint32_t)atomic_get(&delivery_streak_start);
			if (streak != 0 &&
			    (now_s - ld) > 2U * (uint32_t)CONFIG_AMI_COAP_KEEPALIVE_PERIOD_S) {
				atomic_set(&delivery_streak_start, 0);  /* real gap → streak broken */
				streak = 0;
			}
			/* Require 2x the deadline of UNBROKEN delivery. Since delivery_ok
			 * tolerates a stale last_delivery for up to one (wide) deadline,
			 * any flapping board STALLS — and the gap guard breaks its streak —
			 * before the streak can reach 2x deadline. So only a board that has
			 * genuinely delivered continuously well past the stall horizon (and
			 * therefore never stalled) can clear the cap; a delta<deadline flap
			 * can never reach this and stays capped. */
			if (streak != 0 && (now_s - streak) >= 2U * delivery_deadline_s &&
			    lwm2m_diag_get_dlv_reboots() != 0) {
				lwm2m_diag_reset_dlv_reboots();
			}
			if (chan_kernel >= 0) {
				(void)task_wdt_feed(chan_kernel);
			}
			continue;
		}

		/* Fail path: delivery is NOT sustained → break the streak so the cap
		 * can only be cleared by a fresh, full-length healthy run. */
		atomic_set(&delivery_streak_start, 0);

		/* Liveness fail. Action depends on WHY. Direct sys_reboot is
		 * dependency-free (no system_workq / ami_reboot_drain — either could
		 * itself be stuck); if even sys_reboot stalls, chan_kernel is no
		 * longer fed so the HW TG0_WDT bites as the final backstop. */

		/* (a) boot-grace expired: never registered → direct cold reset. */
		if (last == 0) {
			LOG_ERR("HW watchdog: %s expired at uptime=%us (hard cap=%us) — "
				"no first REGISTER ever, SOC cold reset",
				reason, now_s,
				CONFIG_AMI_HW_WATCHDOG_BOOT_GRACE_HARD_S);
			ami_reboot_set_tag(AMI_RBOOT_HWWDT_BOOT_GRACE);
			sys_reboot(SYS_REBOOT_COLD);
		}

		/* (b) delivery-stall: registered but ZERO observers. ESCALATE —
		 * soft re-REGISTER first (no SoC reset), then capped cold reboots,
		 * then stop. This makes the common case (Edge restart) a cheap
		 * per-node re-REGISTER and bounds the worst case (M1). */
		if (reason[0] == 'd') {
			uint32_t ld = (uint32_t)atomic_get(&last_delivery_uptime);

			if ((uint32_t)atomic_get(&dlv_soft_tries) < DLV_SOFT_TRIES_MAX) {
				atomic_inc(&dlv_soft_tries);
				LOG_WRN("HW watchdog: delivery-stall %us (limit %us) — soft "
					"re-REGISTER attempt %ld/%u (no reboot)",
					now_s - ld, delivery_deadline_s,
					(long)atomic_get(&dlv_soft_tries),
					DLV_SOFT_TRIES_MAX);
				/* re-arm so the soft attempt has a full window to work. */
				atomic_set(&last_delivery_uptime, now_s);
				k_work_reschedule(&lwm2m_recover_work, K_NO_WAIT);
				if (chan_kernel >= 0) {
					(void)task_wdt_feed(chan_kernel);
				}
				continue;
			}

			if (lwm2m_diag_get_dlv_reboots() >= DLV_HARD_REBOOTS_MAX) {
				/* Rebooting cannot fix a server-side outage — stop and keep
				 * nudging the soft path (mirror lwm2m_watchdog NOREG cap). */
				LOG_WRN("HW watchdog: delivery-stall persists after %u "
					"reboots — likely server-side; staying up, nudging "
					"recover", DLV_HARD_REBOOTS_MAX);
				atomic_set(&last_delivery_uptime, now_s);
				k_work_reschedule(&lwm2m_recover_work, K_NO_WAIT);
				if (chan_kernel >= 0) {
					(void)task_wdt_feed(chan_kernel);
				}
				continue;
			}

			/* Persist the reboot count BEFORE rebooting so the cap survives
			 * (best-effort: if the NVS write stalls, chan_kernel goes unfed
			 * and the HW TG0_WDT bites — we reboot either way). */
			lwm2m_diag_inc_dlv_reboots();
			LOG_ERR("HW watchdog: delivery-stall %us (limit %us) — registered "
				"but ZERO observers after %u soft tries, SOC cold reset "
				"#%u/%u", now_s - ld, delivery_deadline_s,
				DLV_SOFT_TRIES_MAX, lwm2m_diag_get_dlv_reboots(),
				DLV_HARD_REBOOTS_MAX);
			ami_reboot_set_tag(AMI_RBOOT_HWWDT_DELIVERY);
			sys_reboot(SYS_REBOOT_COLD);
		}

		/* (c) post-register-silence: server stopped ACKing REG_UPDATE
		 * (REAL_LIVENESS gate, pre-existing) → direct cold reset. */
		LOG_ERR("HW watchdog: %s for %us (limit %us) — node cut off, "
			"SOC cold reset", reason, now_s - last,
			CONFIG_AMI_REAL_LIVENESS_TIMEOUT_S);
		ami_reboot_set_tag(AMI_RBOOT_HWWDT_SILENCE);
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
	ami_reboot_set_tag(AMI_RBOOT_HWWDT_CHANNEL);
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
