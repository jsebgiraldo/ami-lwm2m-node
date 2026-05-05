/*
 * LwM2M Liveness Watchdog (v0.20.0 / PRIO 5)
 *
 * External watchdog that detects "engine alive but mute" failure mode:
 * the LwM2M state machine reports no error events to the application,
 * but stops emitting Updates and Notifies. Without this watchdog, the
 * node is zombie until reset.
 *
 * Liveness signal: lwm2m_watchdog_emit_event() is called by the
 * application every time a Notify or Update is successfully emitted
 * (e.g. from meter_push_to_lwm2m()). The watchdog timer fires every
 * LWM2M_WATCHDOG_PERIOD_S and submits lwm2m_recover_work if the silence
 * exceeds 2 × CONFIG_LWM2M_ENGINE_DEFAULT_LIFETIME.
 *
 * Runs on a dedicated preemptive thread (K_PRIO_PREEMPT(7)) with its own
 * stack. Independent of every Zephyr workq — neither the LwM2M engine
 * queue nor the system workq can block it from firing.
 */

#ifndef LWM2M_WATCHDOG_H_
#define LWM2M_WATCHDOG_H_

#include <stdint.h>

/**
 * @brief Initialize and start the watchdog. Call once from main().
 */
void lwm2m_watchdog_init(void);

/**
 * @brief Signal liveness: a Notify or Update was just emitted.
 *
 * Resets the silence-since timer. Called from meter_push_to_lwm2m()
 * (each successful PUSH_FIELD) and from rd_client_event() on
 * REG_UPDATE_COMPLETE.
 */
void lwm2m_watchdog_emit_event(void);

/**
 * @brief Last emit uptime in seconds since boot. 0 if never emitted.
 *
 * Useful for diagnostics and for the post-restart health probe to
 * cross-check liveness independently of REGISTRATION_COMPLETE.
 */
uint32_t lwm2m_watchdog_get_last_emit_uptime(void);

#endif /* LWM2M_WATCHDOG_H_ */
