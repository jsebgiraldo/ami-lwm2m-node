/*
 * Post-mortem snapshot persistence.
 *
 * Why this exists: when the hardware watchdog (TG0_WDT) fires, the chip
 * does a hard reset — no software runs afterwards, so we can't dump
 * stack/heap/state from a panic hook. The only thing that survives the
 * reset is whatever was already written to flash.
 *
 * What this module does: every PM_SNAPSHOT_PERIOD_S, and on every
 * meaningful state change (LwM2M register/recover, errno != 0, role
 * transitions), persist a tiny struct to NVS under `ami/last_hang`.
 * After a watchdog reset, the next boot reads that record back; what it
 * shows IS approximately what the firmware was doing at the moment the
 * CPU got stuck.
 *
 * Cost: ~24 B / write, 1440 writes/day with the periodic snapshot only,
 * Zephyr settings does wear-levelling on NVS so a single 4 KB sector
 * lasts >70 days at 100 k cycles. Eager snapshots add maybe 20-50/day.
 *
 * Pairs with Object 33000 v2.4 RIDs 23-28 — see lwm2m_obj_thread_diag.h.
 * After boot, main.c calls pm_get_last() and pushes the fields as LwM2M
 * resources so the snapshot becomes visible in TB Edge.
 */

#ifndef POST_MORTEM_H
#define POST_MORTEM_H

#include <stdbool.h>
#include <stdint.h>

/* Magic in the NVS record — distinguishes a valid record from a freshly
 * erased page or a struct-layout mismatch after a firmware upgrade. */
#define PM_MAGIC                 0x504D5631U   /* 'P','M','V','1' */

/* Bit-packed LwM2M client state (RID 27). */
#define PM_LWM2M_STATE_NONE       0x00U
#define PM_LWM2M_STATE_STARTED    0x01U   /* lwm2m_rd_client_start() called */
#define PM_LWM2M_STATE_REGISTERED 0x02U   /* REGISTRATION_COMPLETE seen */
#define PM_LWM2M_STATE_OBSERVING  0x04U   /* >= 1 active observe set */
#define PM_LWM2M_STATE_RECOVERING 0x08U   /* recover_work_fn running */
#define PM_LWM2M_STATE_LAST_ERR   0x80U   /* last_error_code != 0 */

/* Same numeric values OpenThread uses for otDeviceRole — see openthread/thread.h.
 * Kept here so callers don't have to drag in OT headers. */
#define PM_OT_ROLE_DISABLED       0
#define PM_OT_ROLE_DETACHED       1
#define PM_OT_ROLE_CHILD          2
#define PM_OT_ROLE_ROUTER         3
#define PM_OT_ROLE_LEADER         4

/* On-NVS layout. Keep this stable; bump PM_MAGIC if you change it. */
struct pm_record {
	uint32_t magic;          /* PM_MAGIC */
	uint32_t uptime_s;       /* k_uptime_get()/1000 at snapshot */
	uint32_t heap_free;      /* sys_heap free bytes at snapshot */
	uint32_t heap_min_free;  /* historical min sys_heap free */
	uint32_t reg_age_s;      /* seconds since last REG_UPDATE_COMPLETE */
	uint8_t  lwm2m_state;    /* PM_LWM2M_STATE_* bitmap */
	uint8_t  thread_role;    /* PM_OT_ROLE_* */
	uint8_t  reserved[2];    /* keep struct 4-byte aligned */
};                           /* 24 B */

/* Initialise the module: register the settings handler. Must be called
 * AFTER settings_subsys_init(), BEFORE the periodic snapshot timer can
 * fire and BEFORE pm_get_last() is meaningful.
 *
 * Returns 0 on success or a negative errno from settings registration.
 */
int  pm_init(void);

/* Copy the last-loaded NVS record into `out`. Returns true if a valid
 * record was loaded at pm_init() time (magic + size matched). False
 * means "first boot ever after a chip-erase / corrupt NVS / mismatched
 * struct version" — the caller should treat the fields as undefined. */
bool pm_get_last(struct pm_record *out);

/* Mark the LwM2M state. Idempotent — only triggers an NVS write if the
 * effective state actually changed. */
void pm_set_lwm2m_state(uint8_t state_bits);
void pm_clear_lwm2m_state(uint8_t state_bits);

/* Record that the application just saw a server-ACKed event (REGISTER
 * or REG_UPDATE complete). Stamps `last_reg_uptime_s` so subsequent
 * snapshots can compute reg_age_s. */
void pm_note_register_success(void);

/* Force a write of the current state to NVS. Cheap (~ms) — call after
 * notable transitions (recover entered/exited, errno recorded). The
 * periodic snapshot calls this on its timer; the caller doesn't need
 * to throttle. */
void pm_snapshot(void);

#endif /* POST_MORTEM_H */
