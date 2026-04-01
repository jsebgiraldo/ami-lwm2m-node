/*
 * RGB LED - WS2812 single-pixel driver (GPIO bit-bang)
 *
 * Bit-bangs the WS2812 protocol on GPIO 8.
 * Uses Zephyr's hardware cycle counter for sub-us timing.
 *
 * Target: ESP32-C6 Super Mini - WS2812B on GPIO 8
 */

#ifndef RGB_LED_H
#define RGB_LED_H

#include <stdint.h>

/** Initialise the WS2812 GPIO pin.  Returns 0 on success. */
int rgb_led_init(void);

/** Set the LED colour immediately (blocking, ~30 us with IRQs off). */
void rgb_led_set(uint8_t r, uint8_t g, uint8_t b);

/** Turn the LED off. */
void rgb_led_off(void);

/** Returns true if the WS2812 driver initialized successfully. */
bool rgb_led_is_ready(void);

#endif /* RGB_LED_H */
