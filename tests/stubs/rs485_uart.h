/*
 * Stub: rs485_uart.h
 * No-op RS485 driver stubs for native unit testing.
 */
#ifndef RS485_UART_H_
#define RS485_UART_H_

#include <stdint.h>
#include <stddef.h>

static inline int rs485_init(void) { return 0; }

static inline int rs485_send(const uint8_t *data, size_t len)
{
	(void)data; (void)len;
	return (int)len;
}

static inline int rs485_recv(uint8_t *buf, size_t buf_size, int timeout_ms)
{
	(void)buf; (void)buf_size; (void)timeout_ms;
	return 0;  /* No data — stub */
}

static inline void rs485_flush_rx(void) {}

#endif /* RS485_UART_H_ */
