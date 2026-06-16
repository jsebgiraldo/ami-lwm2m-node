/*
 * RGB LED - WS2812 single-pixel driver (DISABLED in v0.6.55)
 *
 * v0.6.55: full no-op implementation. The custom bit-bang driver and the
 * Zephyr SPI-backed driver both had unworkable trade-offs (custom = stuck-
 * colour latch under load; SPI = broke 802.15.4 radio). Firmware never
 * touches GPIO 8, so the WS2812 retains its power-up latch indefinitely.
 *
 * Per-board the LED state is whatever the chip latched at cold-boot from
 * the floating-data-line race, which differs across boards. NOT uniform,
 * but the firmware is no longer responsible for the LED visual at all.
 *
 * For uniform OFF in the field, physically tape over the LED, or depopulate
 * it in a future PCB rev.
 *
 * Public API preserved so callers (main.c, lwm2m_obj_light_control.c) compile
 * unchanged. All entry points are no-ops.
 */

#include <stdbool.h>
#include <stdint.h>
#include "rgb_led.h"

int rgb_led_init(void)
{
	return 0;
}

void rgb_led_set(uint8_t r, uint8_t g, uint8_t b)
{
	(void)r;
	(void)g;
	(void)b;
}

void rgb_led_off(void)
{
}

bool rgb_led_is_ready(void)
{
	return true;
}
