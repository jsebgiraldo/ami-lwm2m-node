/*
 * Zephyr API Stubs for Native Unit Testing
 *
 * Provides minimal replacements for Zephyr kernel, logging, and
 * errno symbols so DLMS/HDLC/COSEM source files compile natively.
 */
#ifndef ZEPHYR_STUBS_H_
#define ZEPHYR_STUBS_H_

#include <stdio.h>
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>
#include <stdlib.h>
#include <math.h>

/* ---- Zephyr errno codes ---- */
#ifndef EINVAL
#define EINVAL   22
#endif
#ifndef ENOMEM
#define ENOMEM   12
#endif
#ifndef ENODATA
#define ENODATA  61
#endif
#ifndef EPROTO
#define EPROTO   71
#endif
#ifndef EACCES
#define EACCES   13
#endif
#ifndef EIO
#define EIO       5
#endif
#ifndef ENOTSUP
#define ENOTSUP  95
#endif
#ifndef ENOTCONN
#define ENOTCONN 107
#endif
#ifndef EAGAIN
#define EAGAIN   11
#endif

/* ---- Zephyr kernel stubs ---- */
#define K_MSEC(x) (x)
#define K_SECONDS(x) ((x) * 1000)
static inline int64_t k_uptime_get(void) { return 0; }
static inline void k_sleep(int ms) { (void)ms; }

/* ---- Zephyr ARRAY_SIZE ---- */
#ifndef ARRAY_SIZE
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#endif

/* ---- Zephyr logging stubs (no-op) ---- */
#define LOG_MODULE_REGISTER(name, level)
#define LOG_MODULE_DECLARE(name, level)
#define LOG_INF(...)   do {} while (0)
#define LOG_WRN(...)   do {} while (0)
#define LOG_ERR(...)   do {} while (0)
#define LOG_DBG(...)   do {} while (0)
#define LOG_HEXDUMP_DBG(data, len, msg) do {} while (0)
#define LOG_LEVEL_DBG  4
#define LOG_LEVEL_INF  3
#define LOG_LEVEL_WRN  2
#define LOG_LEVEL_ERR  1

/* ---- Zephyr IS_ENABLED / Kconfig stubs ---- */
#ifndef IS_ENABLED
#define _IS_ENABLED1(cfg_val) _IS_ENABLED2(_IS_EMPTY_##cfg_val)
#define _IS_ENABLED2(one_or_two_args) _IS_ENABLED3(one_or_two_args 1, 0)
#define _IS_ENABLED3(ignore_this, val, ...) val
#define _IS_EMPTY_ ~,1

/* Simplified IS_ENABLED: returns 1 if macro is defined to 1 */
#define IS_ENABLED(cfg) _IS_ENABLED1(cfg)
#endif

/* Enable single-phase mode for tests (matches prj.conf) */
#define CONFIG_AMI_SINGLE_PHASE 1

/* ---- Zephyr kernel header replacement ---- */
/* When source files #include <zephyr/kernel.h>, redirect to this file */

#endif /* ZEPHYR_STUBS_H_ */
