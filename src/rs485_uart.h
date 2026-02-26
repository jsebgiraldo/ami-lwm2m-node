/*
 * RS485 UART Driver — Half-duplex control for Seeed XIAO RS485 Expansion Board
 *
 * Provides send/receive functions with automatic DE/RE pin toggling
 * for the RS485 transceiver. DE pin HIGH = transmit mode, LOW = receive mode.
 */

#ifndef RS485_UART_H_
#define RS485_UART_H_

#include <zephyr/kernel.h>
#include <stdint.h>
#include <stddef.h>

/**
 * @brief Initialize RS485 UART interface
 *
 * Configures UART1 and the DE/RE GPIO pin for RS485 half-duplex communication.
 *
 * @return 0 on success, negative errno on failure
 */
int rs485_init(void);

/**
 * @brief Send data over RS485
 *
 * Asserts DE pin, transmits data, waits for completion, then de-asserts DE.
 *
 * @param data   Pointer to data buffer
 * @param len    Number of bytes to send
 * @return Number of bytes sent, or negative errno on failure
 */
int rs485_send(const uint8_t *data, size_t len);

/**
 * @brief Receive data from RS485
 *
 * Waits for data in receive mode (DE pin LOW) with a timeout.
 *
 * @param buf        Pointer to receive buffer
 * @param buf_size   Maximum bytes to receive
 * @param timeout_ms Timeout in milliseconds (0 = no wait, K_FOREVER equivalent = -1)
 * @return Number of bytes received, or negative errno on timeout/failure
 */
int rs485_recv(uint8_t *buf, size_t buf_size, int timeout_ms);

/**
 * @brief Flush RX buffer
 *
 * Discards any pending data in the UART receive buffer.
 */
void rs485_flush_rx(void);

#endif /* RS485_UART_H_ */
