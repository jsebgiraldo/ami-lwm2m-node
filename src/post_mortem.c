/*
 * Post-mortem snapshot persistence — see post_mortem.h for design.
 *
 * Implementation notes:
 *   - Settings prefix is "pm" to keep this module's writes separated from
 *     the existing "ami/total_resets" key main.c owns. Both ride the
 *     same Zephyr settings backend (NVS); the prefix is namespacing only.
 *   - One workq item drives the periodic snapshot — runs on system_workq,
 *     so if system_workq is wedged the snapshot stops updating. That's
 *     fine: a stuck system_workq IS the failure mode chan_workq HW WDT
 *     catches, and the last-good snapshot we already have on NVS is the
 *     hang context the next boot will report.
 *   - Heap accounting uses sys_heap_runtime_stats_get() via the libc
 *     heap exported as `_system_heap`. Returns 0/0 if NEWLIB+heap-stats
 *     are not built in; we degrade silently rather than failing.
 */

#include "post_mortem.h"

#include <zephyr/kernel.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/settings/settings.h>
#include <zephyr/logging/log.h>
#include <string.h>

/* sys_heap_runtime_stats_get() requires CONFIG_SYS_HEAP_RUNTIME_STATS=y in
 * the build. The libc heap may or may not have it enabled; gate the include
 * + use so we still compile cleanly if not. With it disabled, heap_free /
 * heap_min_free in the snapshot stay 0 and the field is reported as such
 * — degraded but not broken. */
#if defined(CONFIG_SYS_HEAP_RUNTIME_STATS)
#include <zephyr/sys/sys_heap.h>
extern struct sys_heap _system_heap;
#endif

LOG_MODULE_REGISTER(post_mortem, LOG_LEVEL_INF);

#define PM_SETTINGS_PREFIX   "pm"
#define PM_SETTINGS_KEY      PM_SETTINGS_PREFIX "/last_hang"

/* Periodic snapshot interval. 60 s gives ~1440 writes/day — comfortably
 * inside NVS wear budget while keeping diagnostic resolution tight. */
#define PM_SNAPSHOT_PERIOD   K_SECONDS(60)

/* Loaded record from the previous boot (valid only after pm_init()). */
static struct pm_record pm_last;
static bool pm_last_valid;

/* Live state we mirror into the next snapshot. Atomic so callers from
 * any thread/IRQ-safe context can mutate without locks. */
static atomic_t pm_lwm2m_state    = ATOMIC_INIT(PM_LWM2M_STATE_NONE);
static atomic_t pm_thread_role    = ATOMIC_INIT(PM_OT_ROLE_DISABLED);
static atomic_t pm_last_reg_uptime = ATOMIC_INIT(0); /* seconds */

/* Cached "last record we wrote" to suppress no-op NVS writes on eager
 * callers. The periodic path always writes (it's the heartbeat the
 * server uses to detect "node has been alive at least this long"). */
static struct pm_record pm_cached_write;

/* Live minimum heap-free tracker. sys_heap doesn't natively expose
 * "min ever free" — we sample at each snapshot and remember the min.
 * Stays UINT32_MAX-equivalent (i.e. uninitialised) if heap stats aren't
 * compiled in; the snapshot then reports heap_free=0/heap_min_free=0,
 * which is the documented "stats not available" indicator. */
static atomic_t pm_heap_min_free = ATOMIC_INIT(0xFFFFFFFF);

static uint32_t pm_now_s(void)
{
	return (uint32_t)(k_uptime_get() / 1000);
}

static void pm_query_heap(uint32_t *free_out, uint32_t *min_free_out)
{
#if defined(CONFIG_SYS_HEAP_RUNTIME_STATS)
	struct sys_memory_stats st = {0};
	int rc = sys_heap_runtime_stats_get(&_system_heap, &st);
	uint32_t cur = (rc == 0) ? (uint32_t)st.free_bytes : 0U;
	*free_out = cur;
	if (cur > 0 && cur < (uint32_t)atomic_get(&pm_heap_min_free)) {
		atomic_set(&pm_heap_min_free, (atomic_val_t)cur);
	}
	uint32_t mn = (uint32_t)atomic_get(&pm_heap_min_free);
	*min_free_out = (mn == 0xFFFFFFFF) ? 0U : mn;
#else
	/* Heap stats not built in. Report zeros — the boot-side log line
	 * `post-mortem: heap_free=0` is the signal that says "we don't
	 * know, rebuild with CONFIG_SYS_HEAP_RUNTIME_STATS=y to capture". */
	*free_out = 0U;
	*min_free_out = 0U;
#endif
}

static void pm_build_record(struct pm_record *r)
{
	uint32_t now_s = pm_now_s();
	uint32_t last_reg = (uint32_t)atomic_get(&pm_last_reg_uptime);
	r->magic         = PM_MAGIC;
	r->uptime_s      = now_s;
	r->reg_age_s     = (last_reg && now_s >= last_reg) ? (now_s - last_reg) : 0U;
	r->lwm2m_state   = (uint8_t)atomic_get(&pm_lwm2m_state);
	r->thread_role   = (uint8_t)atomic_get(&pm_thread_role);
	r->reserved[0]   = 0;
	r->reserved[1]   = 0;
	pm_query_heap(&r->heap_free, &r->heap_min_free);
}

static int pm_write_now(void)
{
	struct pm_record r;
	pm_build_record(&r);
	int rc = settings_save_one(PM_SETTINGS_KEY, &r, sizeof(r));
	if (rc == 0) {
		memcpy(&pm_cached_write, &r, sizeof(r));
	} else {
		LOG_WRN("settings_save_one(%s) failed: %d", PM_SETTINGS_KEY, rc);
	}
	return rc;
}

/* ───────────────────────── periodic snapshot work ──────────────────── */
static void pm_periodic_fn(struct k_work *w);
static K_WORK_DELAYABLE_DEFINE(pm_periodic_work, pm_periodic_fn);

static void pm_periodic_fn(struct k_work *w)
{
	ARG_UNUSED(w);
	(void)pm_write_now();
	k_work_reschedule(&pm_periodic_work, PM_SNAPSHOT_PERIOD);
}

/* ───────────────────────── settings hooks ──────────────────────────── */
static int pm_settings_load_cb(const char *name, size_t len,
			       settings_read_cb read_cb, void *cb_arg)
{
	/* name comes in without the prefix — strncmp on the leaf only. */
	if (strcmp(name, "last_hang") != 0) {
		return 0;
	}
	if (len != sizeof(pm_last)) {
		LOG_INF("post_mortem: stored record size %u != expected %u "
			"(struct version changed?) — discarding",
			(unsigned)len, (unsigned)sizeof(pm_last));
		return 0;
	}
	struct pm_record tmp;
	ssize_t got = read_cb(cb_arg, &tmp, sizeof(tmp));
	if (got != sizeof(tmp)) {
		LOG_WRN("post_mortem: short read (%d)", (int)got);
		return 0;
	}
	if (tmp.magic != PM_MAGIC) {
		LOG_INF("post_mortem: bad magic 0x%08x — first boot or "
			"struct mismatch", tmp.magic);
		return 0;
	}
	memcpy(&pm_last, &tmp, sizeof(pm_last));
	pm_last_valid = true;
	LOG_INF("post_mortem: loaded last_hang uptime=%us reg_age=%us "
		"heap_free=%u min=%u lwm2m=0x%02x role=%u",
		pm_last.uptime_s, pm_last.reg_age_s,
		pm_last.heap_free, pm_last.heap_min_free,
		pm_last.lwm2m_state, pm_last.thread_role);
	return 0;
}

SETTINGS_STATIC_HANDLER_DEFINE(pm_settings, PM_SETTINGS_PREFIX, NULL,
			       pm_settings_load_cb, NULL, NULL);

/* ───────────────────────── public API ──────────────────────────────── */
int pm_init(void)
{
	/* settings_subsys_init() + settings_load() are owned by main.c;
	 * by the time we get called the static handler above has already
	 * been invoked for any stored value, so pm_last_valid is set.
	 * We just kick off the periodic snapshot here. */
	k_work_reschedule(&pm_periodic_work, PM_SNAPSHOT_PERIOD);
	/* Period is a compile-time constant we set ourselves, log it directly. */
	LOG_INF("post_mortem armed: snapshot period=60s, last_record_valid=%d",
		pm_last_valid);
	return 0;
}

bool pm_get_last(struct pm_record *out)
{
	if (!out || !pm_last_valid) {
		return false;
	}
	memcpy(out, &pm_last, sizeof(*out));
	return true;
}

void pm_set_lwm2m_state(uint8_t state_bits)
{
	uint8_t prev = (uint8_t)atomic_get(&pm_lwm2m_state);
	uint8_t now = prev | state_bits;
	if (now != prev) {
		atomic_set(&pm_lwm2m_state, (atomic_val_t)now);
		pm_snapshot();
	}
}

void pm_clear_lwm2m_state(uint8_t state_bits)
{
	uint8_t prev = (uint8_t)atomic_get(&pm_lwm2m_state);
	uint8_t now = prev & (uint8_t)~state_bits;
	if (now != prev) {
		atomic_set(&pm_lwm2m_state, (atomic_val_t)now);
		pm_snapshot();
	}
}

void pm_note_register_success(void)
{
	atomic_set(&pm_last_reg_uptime, (atomic_val_t)pm_now_s());
	pm_set_lwm2m_state(PM_LWM2M_STATE_REGISTERED);
}

void pm_snapshot(void)
{
	(void)pm_write_now();
}
