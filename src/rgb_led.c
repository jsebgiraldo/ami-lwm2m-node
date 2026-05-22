/*
 * RGB LED - WS2812 single-pixel driver (GPIO bit-bang)
 *
 * Sends 24 bits (GRB order, MSB-first) over GPIO8 using the ESP32-C6
 * RISC-V performance counter (CSR 0x7E2, 160 MHz CPU clock) for timing.
 * Direct GPIO register writes (W1TS/W1TC) for sub-µs pin toggling.
 * Interrupts are locked for the 24-bit frame (~30 us).
 *
 * WS2812B timing (+/-150 ns tolerance):
 *   0-bit : HIGH 400 ns, LOW 850 ns
 *   1-bit : HIGH 800 ns, LOW 450 ns
 *   Reset : LOW  >= 50 us
 *
 * ESP32-C6 RISC-V performance counter CSRs:
 *   0x7E0 = PCER  (event enable — bit 0 = cycle counter)
 *   0x7E1 = PCMR  (mode — bit 0 = enable counting, MUST be 1)
 *   0x7E2 = PCCR  (count register — reads CPU cycles at 160 MHz)
 *   NOTE: standard mcycle (0xB00) is NOT available on ESP32-C6.
 *
 * Target: ESP32-C6 (WROOM DevKitC / Super Mini) - WS2812B on GPIO 8
 *
 * Reference: IoT-UNal/Unal-Flash-tool firmware/zephyr-rgb
 */

#include <zephyr/kernel.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/logging/log.h>
#include <soc.h>

#include "rgb_led.h"

LOG_MODULE_REGISTER(rgb_led, LOG_LEVEL_INF);

/* ---- hardware constants ------------------------------------------------ */

#define WS2812_GPIO_NODE  DT_NODELABEL(gpio0)
#define WS2812_GPIO_PIN   8          /* ESP32-C6 onboard WS2812 LED */
#define WS2812_GPIO_MASK  (1U << WS2812_GPIO_PIN)

/* ESP32-C6 GPIO registers (direct register access for speed)
 * DR_REG_GPIO_BASE = 0x60091000 (from soc/gpio_reg.h)
 * GPIO_OUT_W1TS_REG = base + 0x08
 * GPIO_OUT_W1TC_REG = base + 0x0C
 */
#define GPIO_OUT_W1TS_REG  (*(volatile uint32_t *)0x60091008)
#define GPIO_OUT_W1TC_REG  (*(volatile uint32_t *)0x6009100C)

/* ESP32-C6 CPU frequency. v0.6.34: dropped 160 -> 80 MHz to cut self-heating
 * (see boards/xiao_esp32c6_hpcore.overlay &cpu0). MUST match the actual clock
 * set in the overlay — the WS2812 bit-bang below derives its cycle counts from
 * this, and a mismatch corrupts the LED timing. */
#define CPU_FREQ_MHZ  80

/* Nanosecond timing targets → CPU cycle counts at CPU_FREQ_MHZ.
 * At 80 MHz: T0H=32, T0L=68, T1H=64, T1L=36 cycles (1 cyc = 12.5 ns). */
#define T0H_CYC  ((400 * CPU_FREQ_MHZ) / 1000)   /* 400 ns */
#define T0L_CYC  ((850 * CPU_FREQ_MHZ) / 1000)   /* 850 ns */
#define T1H_CYC  ((800 * CPU_FREQ_MHZ) / 1000)   /* 800 ns */
#define T1L_CYC  ((450 * CPU_FREQ_MHZ) / 1000)   /* 450 ns */

/* ---- module state ------------------------------------------------------ */

static const struct device *gpio_dev;
static bool initialised;

/* ---- helpers ----------------------------------------------------------- */

/*
 * ESP32-C6 RISC-V performance counter.
 * PCER (0x7E0) bit 0 = enable cycle-count event.
 * PCMR (0x7E1) bit 0 = enable counter increment (CRITICAL: must be 1).
 * PCCR (0x7E2) = cycle count (160 MHz).
 */
static inline void cpu_perf_counter_init(void)
{
	/* Enable cycle-count event + enable counting mode */
	__asm__ volatile("csrw 0x7E0, %0" :: "r"(1));
	__asm__ volatile("csrw 0x7E1, %0" :: "r"(1));  /* was 0 → hung! */
}

static inline uint32_t cpu_cycle(void)
{
	uint32_t val;
	__asm__ volatile("csrr %0, 0x7E2" : "=r"(val));
	return val;
}

static inline void spin_until(uint32_t start, uint32_t cycles)
{
	while ((cpu_cycle() - start) < cycles) {
		/* busy-wait */
	}
}

/* Send a single bit - must be called with IRQs locked */
static inline void send_bit(int bit)
{
	uint32_t t;

	GPIO_OUT_W1TS_REG = WS2812_GPIO_MASK;     /* HIGH */
	t = cpu_cycle();
	spin_until(t, bit ? T1H_CYC : T0H_CYC);

	GPIO_OUT_W1TC_REG = WS2812_GPIO_MASK;     /* LOW */
	t = cpu_cycle();
	spin_until(t, bit ? T1L_CYC : T0L_CYC);
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

	/* Enable RISC-V performance cycle counter (PCER=1, PCMR=1) */
	cpu_perf_counter_init();

	LOG_INF("WS2812 on GPIO%d ready (CPU %d MHz)",
		WS2812_GPIO_PIN, CPU_FREQ_MHZ);

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
