/*
 * Hardware Watchdog (v0.6.13+ / PRIO 9)
 *
 * Last-resort defense layer below the LwM2M soft watchdog. Drives the
 * ESP32-C6 Timer-Group 0 watchdog through Zephyr's task_wdt subsystem.
 * Two channels are fed:
 *   - kernel-alive  : dedicated K_PRIO_PREEMPT(4) thread, GATED on real
 *                     liveness (v0.6.17) — see hw_watchdog_note_liveness()
 *   - workq-alive   : self-rescheduling delayed work on system_workq
 *
 * If either channel is silent for more than CONFIG_AMI_HW_WATCHDOG_TIMEOUT_S
 * the SoC is hard-reset (SYS_RESET). Survives kernel scheduler stalls,
 * system_workq deadlocks, USB-CDC backpressure deadlocks, and a hung
 * ami_reboot_drain — none of which the soft watchdog can recover from.
 *
 * v0.6.17: the kernel-alive channel is no longer fed unconditionally. A
 * node whose USB/radio peripheral wedges while the CPU stays healthy used
 * to keep feeding both channels forever (field proof: ~1.5 h hang, zero
 * resets). The kernel feeder now also verifies REAL end-to-end liveness —
 * see hw_watchdog_note_liveness().
 *
 * Call hw_watchdog_init() ONCE from main(), early — before Thread attach.
 */

#ifndef HW_WATCHDOG_H_
#define HW_WATCHDOG_H_

void hw_watchdog_init(void);

/*
 * v0.6.26: Signal that main() has safely passed the early-boot danger zone
 * (settings_subsys_init + settings_load + post_mortem deserialization). Call
 * this ONCE from main() as early as possible after those NVS operations
 * complete.
 *
 * Why: a corrupt NVS / settings partition can hang settings_load_subtree()
 * indefinitely. That hang happens in main() BEFORE hw_watchdog_init() ran,
 * so historically no watchdog was armed and the node sat dead forever
 * (field proof v0.6.25: nodes with reg_success=0, last_reset_reason=POR,
 * silent for hours). hw_watchdog_boot_arm() (a SYS_INIT at POST_KERNEL)
 * now arms a "boot survival" task_wdt channel BEFORE main() runs. That
 * channel is fed for the first and only time here. If main() hangs in the
 * NVS path, the channel is never fed → TG0_WDT bites at
 * CONFIG_AMI_HW_WATCHDOG_TIMEOUT_S → SoC reset. After this call the normal
 * kernel/workq feeders (started by hw_watchdog_init) keep it alive.
 */
void hw_watchdog_note_boot_survived(void);

/*
 * Report end-to-end liveness: the LwM2M server has just ACKed us.
 *
 * Call ONLY from events that prove the full radio + mesh + routing +
 * server + CoAP path is alive — i.e. LWM2M_RD_CLIENT_EVENT_REGISTRATION_
 * COMPLETE and LWM2M_RD_CLIENT_EVENT_REG_UPDATE_COMPLETE. Do NOT call it
 * from lwm2m_set / notify paths: those only queue data locally and return
 * success even when the radio is dead - that false signal is exactly what
 * let the original watchdog be fooled.
 *
 * If this is not called for CONFIG_AMI_REAL_LIVENESS_TIMEOUT_S, the
 * kernel-alive feeder withholds its feed and triggers a cold reset.
 */
void hw_watchdog_note_liveness(void);

#endif /* HW_WATCHDOG_H_ */
