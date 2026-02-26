/*
 * RS485 UART Driver — Half-duplex control for Seeed XIAO RS485 Expansion Board
 *
 * Hardware: UART1 on GPIO22(RX)/GPIO23(TX), DE/RE on GPIO2
 * DLMS meters typically use 9600 baud, 8E1 or 8N1
 */

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <stdlib.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/logging/log.h>

#include "rs485_uart.h"

LOG_MODULE_REGISTER(rs485, LOG_LEVEL_DBG);

/* UART1 device (configured in overlay) */
static const struct device *uart_dev;

/* DE/RE control pin: GPIO2 (XIAO D2) */
static const struct gpio_dt_spec de_pin =
	GPIO_DT_SPEC_GET(DT_NODELABEL(rs485_de), gpios);

/* Receive ring buffer */
#define RS485_RX_BUF_SIZE  512
static uint8_t rx_ring_buf[RS485_RX_BUF_SIZE];
static volatile uint16_t rx_head;
static volatile uint16_t rx_tail;

/* Semaphore to signal data available */
static K_SEM_DEFINE(rx_sem, 0, 1);

/* ---- UART ISR callback ---- */
static void uart_isr_cb(const struct device *dev, void *user_data)
{
	ARG_UNUSED(user_data);

	while (uart_irq_update(dev) && uart_irq_is_pending(dev)) {
		if (uart_irq_rx_ready(dev)) {
			uint8_t byte;
			while (uart_fifo_read(dev, &byte, 1) == 1) {
				uint16_t next = (rx_head + 1) % RS485_RX_BUF_SIZE;
				if (next != rx_tail) {
					rx_ring_buf[rx_head] = byte;
					rx_head = next;
				}
				/* else: buffer full, drop byte */
			}
			k_sem_give(&rx_sem);
		}
	}
}

/* ---- Public API ---- */

int rs485_init(void)
{
	int ret;

	/* Get UART1 device */
	uart_dev = DEVICE_DT_GET(DT_NODELABEL(uart1));
	if (!device_is_ready(uart_dev)) {
		LOG_ERR("UART1 device not ready");
		return -ENODEV;
	}

	/* Configure DE/RE pin as output, default LOW (receive mode) */
	if (!gpio_is_ready_dt(&de_pin)) {
		LOG_ERR("RS485 DE pin GPIO not ready");
		return -ENODEV;
	}

	ret = gpio_pin_configure_dt(&de_pin, GPIO_OUTPUT_INACTIVE);
	if (ret < 0) {
		LOG_ERR("Failed to configure DE pin: %d", ret);
		return ret;
	}

	/* Initialize ring buffer */
	rx_head = 0;
	rx_tail = 0;

	/* Set up UART interrupt-driven RX */
	uart_irq_callback_set(uart_dev, uart_isr_cb);
	uart_irq_rx_enable(uart_dev);

	LOG_INF("RS485 initialized: UART1 @ 9600 baud, DE=GPIO2");
	return 0;
}

int rs485_send(const uint8_t *data, size_t len)
{
	if (!uart_dev || !data || len == 0) {
		return -EINVAL;
	}

	/* Assert DE pin (transmit mode) */
	gpio_pin_set_dt(&de_pin, 1);

	/* Small delay for transceiver to switch (typ. 1-5 µs, use 100 µs margin) */
	k_busy_wait(100);

	/* Transmit all bytes */
	for (size_t i = 0; i < len; i++) {
		uart_poll_out(uart_dev, data[i]);
	}

	/*
	 * Wait for ALL bytes to drain from the UART TX FIFO.
	 * uart_poll_out() returns as soon as the byte enters the FIFO,
	 * but the ESP32-C6 has a 128-byte FIFO — so all bytes are queued
	 * almost instantly while physical TX at 9600 baud takes ~1.04 ms
	 * per byte (10 bits: start + 8data + stop).
	 *
	 * If we de-assert DE before the FIFO drains, the RS485 transceiver
	 * disables its driver and the remaining bytes never reach the bus.
	 *
	 * Wait time = (bytes * 10 bits / 9600 baud) + 2ms margin.
	 */
	uint32_t tx_drain_us = ((uint32_t)len * 10417U) / 10U + 2000U;
	k_busy_wait(tx_drain_us);

	/* De-assert DE pin (receive mode) */
	gpio_pin_set_dt(&de_pin, 0);

	LOG_DBG("RS485 TX: %u bytes (drain wait %u us)", (unsigned)len, tx_drain_us);
	LOG_HEXDUMP_DBG(data, len, "RS485 TX");
	return (int)len;
}

int rs485_recv(uint8_t *buf, size_t buf_size, int timeout_ms)
{
	if (!buf || buf_size == 0) {
		return -EINVAL;
	}

	/* Wait for data with timeout */
	k_timeout_t timeout;
	if (timeout_ms < 0) {
		timeout = K_FOREVER;
	} else if (timeout_ms == 0) {
		timeout = K_NO_WAIT;
	} else {
		timeout = K_MSEC(timeout_ms);
	}

	/* Wait for at least one byte */
	if (rx_head == rx_tail) {
		if (k_sem_take(&rx_sem, timeout) != 0) {
			return -EAGAIN;  /* Timeout */
		}
	}

	/*
	 * After getting the semaphore, poll for the complete HDLC frame.
	 * HDLC frames are delimited by 0x7E.  At 9600 baud a 57-byte frame
	 * (the largest AARE) takes ~60 ms.  We poll every 10 ms and bail out
	 * as soon as we see the closing 0x7E or 150 ms have elapsed.
	 */
	for (int waited = 0; waited < 150; waited += 10) {
		k_sleep(K_MSEC(10));

		/* Peek at ring buffer for closing 0x7E (skip first byte which
		 * is also 0x7E as the opening flag) */
		unsigned int key = irq_lock();
		size_t avail = (rx_head >= rx_tail)
			? (rx_head - rx_tail)
			: (RS485_RX_BUF_SIZE - rx_tail + rx_head);
		bool frame_complete = false;
		if (avail >= 2) {
			/* Check last received byte */
			size_t last = (rx_head + RS485_RX_BUF_SIZE - 1) % RS485_RX_BUF_SIZE;
			if (rx_ring_buf[last] == 0x7E) {
				frame_complete = true;
			}
		}
		irq_unlock(key);

		if (frame_complete) {
			break;
		}
	}

	/* Copy available data from ring buffer */
	size_t count = 0;
	unsigned int key = irq_lock();

	while (rx_tail != rx_head && count < buf_size) {
		buf[count++] = rx_ring_buf[rx_tail];
		rx_tail = (rx_tail + 1) % RS485_RX_BUF_SIZE;
	}

	/* Reset semaphore if buffer is empty */
	if (rx_tail == rx_head) {
		k_sem_reset(&rx_sem);
	}

	irq_unlock(key);

	if (count > 0) {
		LOG_HEXDUMP_DBG(buf, count, "RS485 RX");
	}
	LOG_DBG("RS485 RX: %u bytes", (unsigned)count);
	return (int)count;
}

void rs485_flush_rx(void)
{
	unsigned int key = irq_lock();
	rx_head = 0;
	rx_tail = 0;
	k_sem_reset(&rx_sem);
	irq_unlock(key);
}

/* ---- Shell diagnostic commands ---- */
#include <zephyr/shell/shell.h>

/* Build a minimal SNRM frame for a given client SAP */
static int build_test_snrm(uint8_t *buf, size_t buf_size, uint8_t client_sap)
{
	/* SNRM: 7E Format ServerAddr ClientAddr Control 7E */
	uint8_t client_hdlc = (uint8_t)((client_sap << 1) | 1);
	uint8_t server_hdlc = 0x03; /* logical device 1, 1-byte */

	if (buf_size < 7) return -1;
	buf[0] = 0x7E;
	buf[1] = 0xA0;
	buf[2] = 0x07;       /* length */
	buf[3] = server_hdlc; /* destination */
	buf[4] = client_hdlc; /* source */
	buf[5] = 0x93;       /* SNRM control byte */
	buf[6] = 0x7E;
	return 7;
}

static int cmd_rs485_test(const struct shell *sh, size_t argc, char **argv)
{
	(void)argc; (void)argv;
	int ret;
	uint8_t frame[16];
	uint8_t buf[256];

	if (!uart_dev) {
		shell_error(sh, "RS485 not initialized. Run 'rs485 init' first.");
		return -1;
	}

	shell_print(sh, "=== RS485 SNRM test (SAP 1) ===");
	int flen = build_test_snrm(frame, sizeof(frame), 1);
	shell_print(sh, "Sending %d bytes: client=0x03 server=0x03", flen);
	shell_hexdump(sh, frame, flen);

	rs485_flush_rx();
	ret = rs485_send(frame, flen);
	shell_print(sh, "Send returned: %d", ret);

	k_sleep(K_MSEC(100));
	ret = rs485_recv(buf, sizeof(buf), 3000);
	if (ret > 0) {
		shell_print(sh, "*** RESPONSE: %d bytes ***", ret);
		shell_hexdump(sh, buf, ret);
	} else {
		shell_print(sh, "No response (ret=%d)", ret);
	}
	return 0;
}

/* Scan: try multiple SAPs and baud rates */
static int cmd_rs485_scan(const struct shell *sh, size_t argc, char **argv)
{
	(void)argc; (void)argv;
	uint8_t frame[16];
	uint8_t buf[256];
	int ret;

	static const uint8_t saps[] = { 1, 17, 32, 16 };
	static const uint32_t bauds[] = { 9600, 4800, 2400, 1200, 19200, 300 };
	static const char *sap_names[] = { "1", "17", "32", "16" };

	if (!uart_dev) {
		shell_error(sh, "RS485 not initialized");
		return -1;
	}

	struct uart_config ucfg;
	ret = uart_config_get(uart_dev, &ucfg);
	if (ret < 0) {
		shell_error(sh, "Cannot get UART config: %d", ret);
		return ret;
	}
	uint32_t orig_baud = ucfg.baudrate;

	shell_print(sh, "=== RS485 Full Scan ===");
	shell_print(sh, "Testing %d baud rates x %d client SAPs...",
		    (int)ARRAY_SIZE(bauds), (int)ARRAY_SIZE(saps));

	for (int b = 0; b < (int)ARRAY_SIZE(bauds); b++) {
		ucfg.baudrate = bauds[b];
		ret = uart_configure(uart_dev, &ucfg);
		if (ret < 0) {
			shell_print(sh, "  Cannot set %u baud: %d", bauds[b], ret);
			continue;
		}
		shell_print(sh, "--- Baud: %u ---", bauds[b]);

		for (int s = 0; s < (int)ARRAY_SIZE(saps); s++) {
			int flen = build_test_snrm(frame, sizeof(frame), saps[s]);
			rs485_flush_rx();
			rs485_send(frame, flen);

			k_sleep(K_MSEC(100));
			ret = rs485_recv(buf, sizeof(buf), 2000);
			if (ret > 0) {
				shell_print(sh, "  *** HIT *** SAP=%s baud=%u: %d bytes!",
					    sap_names[s], bauds[b], ret);
				shell_hexdump(sh, buf, ret);
			} else {
				shell_print(sh, "  SAP=%s: no response", sap_names[s]);
			}
		}
	}

	/* Restore original baud rate */
	ucfg.baudrate = orig_baud;
	uart_configure(uart_dev, &ucfg);
	shell_print(sh, "Scan complete. Restored baud=%u", orig_baud);
	return 0;
}

/* Loopback test: short A-B on the RS485 connector, or UART TX-RX directly */
static int cmd_rs485_loopback(const struct shell *sh, size_t argc, char **argv)
{
	(void)argc; (void)argv;
	uint8_t tx_data[] = { 0xAA, 0x55, 0x01, 0x02, 0x03, 0x04 };
	uint8_t buf[32];
	int ret;

	if (!uart_dev) {
		shell_error(sh, "RS485 not initialized");
		return -1;
	}

	shell_print(sh, "=== Loopback Test ===");
	shell_print(sh, "Short A<->B on RS485 terminal, or TX<->RX on XIAO pins");
	shell_print(sh, "Sending 6 bytes: AA 55 01 02 03 04");

	/* For loopback: keep RE enabled during TX so we can receive our own data */
	/* Temporarily send with DE HIGH but also don't disable RX */
	rs485_flush_rx();

	/* Set DE HIGH (transmit) */
	gpio_pin_set_dt(&de_pin, 1);
	k_busy_wait(100);

	/* Send bytes */
	for (size_t i = 0; i < sizeof(tx_data); i++) {
		uart_poll_out(uart_dev, tx_data[i]);
	}
	k_busy_wait(2000); /* Wait for last byte */

	/* Switch to receive */
	gpio_pin_set_dt(&de_pin, 0);
	k_sleep(K_MSEC(100));

	/* Check if we got anything (echo from shorted bus) */
	ret = rs485_recv(buf, sizeof(buf), 1000);
	if (ret > 0) {
		shell_print(sh, "Received %d bytes (loopback OK!):", ret);
		shell_hexdump(sh, buf, ret);
		if (ret == sizeof(tx_data) && memcmp(buf, tx_data, sizeof(tx_data)) == 0) {
			shell_print(sh, "*** PERFECT MATCH - hardware works! ***");
		} else {
			shell_print(sh, "Data mismatch - partial loopback");
		}
	} else {
		shell_print(sh, "No echo received (ret=%d)", ret);
		shell_print(sh, "  If A-B are shorted: transceiver may not be working");
		shell_print(sh, "  Try shorting D4(RX) to D5(TX) directly to test UART");
	}
	return 0;
}

/* Set baud rate */
static int cmd_rs485_baud(const struct shell *sh, size_t argc, char **argv)
{
	if (argc < 2) {
		shell_print(sh, "Usage: rs485 baud <rate>");
		shell_print(sh, "  Common: 300 1200 2400 4800 9600 19200");
		return -1;
	}
	uint32_t baud = (uint32_t)atoi(argv[1]);
	struct uart_config ucfg;
	int ret = uart_config_get(uart_dev, &ucfg);
	if (ret < 0) {
		shell_error(sh, "Cannot get config: %d", ret);
		return ret;
	}
	ucfg.baudrate = baud;
	ret = uart_configure(uart_dev, &ucfg);
	if (ret < 0) {
		shell_error(sh, "Cannot set baud %u: %d", baud, ret);
	} else {
		shell_print(sh, "Baud rate set to %u", baud);
	}
	return ret;
}

static int cmd_rs485_init(const struct shell *sh, size_t argc, char **argv)
{
	(void)argc; (void)argv;
	int ret = rs485_init();
	shell_print(sh, "rs485_init() returned %d", ret);
	return ret;
}

static int cmd_rs485_de(const struct shell *sh, size_t argc, char **argv)
{
	if (argc < 2) {
		shell_print(sh, "Usage: rs485 de <0|1>");
		return -1;
	}
	int val = atoi(argv[1]);
	gpio_pin_set_dt(&de_pin, val);
	shell_print(sh, "DE pin set to %d", val);
	return 0;
}

SHELL_STATIC_SUBCMD_SET_CREATE(rs485_cmds,
	SHELL_CMD(init, NULL, "Initialize RS485", cmd_rs485_init),
	SHELL_CMD(test, NULL, "Send SNRM SAP=1 and listen", cmd_rs485_test),
	SHELL_CMD(scan, NULL, "Scan all SAPs and baud rates", cmd_rs485_scan),
	SHELL_CMD(loopback, NULL, "Loopback test (short A-B)", cmd_rs485_loopback),
	SHELL_CMD(baud, NULL, "Set baud rate <rate>", cmd_rs485_baud),
	SHELL_CMD(de, NULL, "Set DE pin <0|1>", cmd_rs485_de),
	SHELL_SUBCMD_SET_END
);

SHELL_CMD_REGISTER(rs485, &rs485_cmds, "RS485 diagnostics", NULL);
