/*
 * RGB LED - WS2812 single-pixel driver (GPIO bit-bang)
 *
 * Sends 24 bits (GRB order, MSB-first) over a single GPIO pin using
 * Zephyr's cycle counter for tight timing.  Interrupts are locked for
 * the duration of the 24-bit frame (~30 us) to avoid jitter.
 *
 * WS2812B timing (+/-150 ns tolerance):
 *   0-bit : HIGH 400 ns, LOW 850 ns
 *   1-bit : HIGH 800 ns, LOW 450 ns
 *   Reset : LOW  >= 50 us
 *
 * Target: ESP32-C6 Super Mini - WS2812B on GPIO 8
 *
 * Reference: IoT-UNal/Unal-Flash-tool firmware/zephyr-rgb
 */

#include <zephyr/kernel.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/logging/log.h>

#include "rgb_led.h"

LOG_MODULE_REGISTER(rgb_led, LOG_LEVEL_INF);

/* ---- hardware constants ------------------------------------------------ */

#define WS2812_GPIO_NODE  DT_NODELABEL(gpio0)
#define WS2812_GPIO_PIN   8          /* ESP32-C6 Super Mini onboard LED */

/* Nanosecond timing targets */
#define T0H_NS   400
#define T0L_NS   850
#define T1H_NS   800
#define T1L_NS   450

/* ---- module state ------------------------------------------------------ */

static const struct device *gpio_dev;
static bool initialised;

/* Pre-computed cycle counts (filled in rgb_led_init) */
static uint32_t t0h_cyc;
static uint32_t t0l_cyc;
static uint32_t t1h_cyc;
static uint32_t t1l_cyc;

/* ---- helpers ----------------------------------------------------------- */

static inline uint32_t ns_to_cycles(uint32_t ns)
{
	uint64_t freq = sys_clock_hw_cycles_per_sec();
	return (uint32_t)((uint64_t)ns * freq / 1000000000ULL);
}

static inline uint32_t cycles_now(void)
{
	return k_cycle_get_32();
}

static inline void spin_until(uint32_t start, uint32_t cycles)
{
	while ((cycles_now() - start) < cycles) {
		/* busy-wait */
	}
}

/* Send a single bit - must be called with IRQs locked */
static inline void send_bit(int bit)
{
	uint32_t t;

	gpio_pin_set_raw(gpio_dev, WS2812_GPIO_PIN, 1);
	t = cycles_now();
	spin_until(t, bit ? t1h_cyc : t0h_cyc);

	gpio_pin_set_raw(gpio_dev, WS2812_GPIO_PIN, 0);
	t = cycles_now();
	spin_until(t, bit ? t1l_cyc : t0l_cyc);
}

/* Send one byte, MSB first */
static inline void send_byte(uint8_t val)
{
	for (int i = 7; i >= 0; i--) {
		send_bit((val >> i) & 1);
	}
}

/* ---- public API -------------------------------------------------------- */

int rgb_led_init(void)
{
	gpio_dev = DEVICE_DT_GET(WS2812_GPIO_NODE);
	if (!device_is_ready(gpio_dev)) {
		LOG_ERR("GPIO device not ready");
		return -ENODEV;
	}

	int ret = gpio_pin_configure(gpio_dev, WS2812_GPIO_PIN,
				     GPIO_OUTPUT_LOW);
	if (ret < 0) {
		LOG_ERR("Failed to configure GPIO%d (%d)", WS2812_GPIO_PIN, ret);
		return ret;
	}

	/* Pre-compute cycle counts for the current clock speed */
	t0h_cyc = ns_to_cycles(T0H_NS);
	t0l_cyc = ns_to_cycles(T0L_NS);
	t1h_cyc = ns_to_cycles(T1H_NS);
	t1l_cyc = ns_to_cycles(T1L_NS);

	uint32_t freq = sys_clock_hw_cycles_per_sec();
	LOG_INF("WS2812 on GPIO%d - hw_cycles/sec: %u "
		"t0h:%u t0l:%u t1h:%u t1l:%u",
		WS2812_GPIO_PIN, freq,
		t0h_cyc, t0l_cyc, t1h_cyc, t1l_cyc);

	initialised = true;
	return 0;
}

void rgb_led_set(uint8_t r, uint8_t g, uint8_t b)
{
	if (!initialised) {
		return;
	}

	/* WS2812 expects GRB byte order */
	unsigned int key = irq_lock();
	send_byte(g);
	send_byte(r);
	send_byte(b);
	irq_unlock(key);

	/* Reset pulse: >50 us low (pin is already low after last bit) */
	k_busy_wait(80);
}

void rgb_led_off(void)
{
	rgb_led_set(0, 0, 0);
}

bool rgb_led_is_ready(void)
{
	return initialised;
}
