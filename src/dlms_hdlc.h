/*
 * DLMS/COSEM HDLC Framing Layer — IEC 62056-46
 *
 * Implements HDLC frame encoding/decoding for DLMS over serial RS485.
 * Supports SNRM, UA, I-frame, DISC, and DM frame types.
 * Uses CRC-16/CCITT for HCS and FCS.
 */

#ifndef DLMS_HDLC_H_
#define DLMS_HDLC_H_

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

/* HDLC constants */
#define HDLC_FLAG           0x7E
#define HDLC_FORMAT_TYPE    0xA0    /* Type 3 frame format */
#define HDLC_MAX_INFO_LEN   256
#define HDLC_MAX_FRAME_LEN  300

/* HDLC control byte values (U-frames) */
#define HDLC_CTRL_SNRM      0x93   /* Set Normal Response Mode */
#define HDLC_CTRL_UA         0x73   /* Unnumbered Acknowledge */
#define HDLC_CTRL_DISC       0x53   /* Disconnect */
#define HDLC_CTRL_DM         0x1F   /* Disconnected Mode */

/* I-frame control byte construction */
#define HDLC_CTRL_I_FRAME(send_seq, recv_seq, pf) \
	(((recv_seq) << 5) | ((pf) ? 0x10 : 0x00) | ((send_seq) << 1))

/* RR (Receive Ready) S-frame */
#define HDLC_CTRL_RR(recv_seq) \
	(0x01 | ((recv_seq) << 5))

/* HDLC address encoding */
#define HDLC_CLIENT_ADDR(logical_addr)  (uint8_t)(((logical_addr) << 1) | 1)
#define HDLC_SERVER_ADDR_1B(logical_addr) (uint8_t)(((logical_addr) << 1) | 1)

/* SNRM information field (negotiation parameters) */
struct hdlc_params {
	uint16_t max_info_tx;   /* Max info field length transmit */
	uint16_t max_info_rx;   /* Max info field length receive */
	uint8_t  window_tx;     /* Transmit window size */
	uint8_t  window_rx;     /* Receive window size */
};

/* Parsed HDLC frame */
struct hdlc_frame {
	uint8_t  dst_addr;
	uint8_t  src_addr;
	uint8_t  control;
	uint8_t  info[HDLC_MAX_INFO_LEN];
	uint16_t info_len;
	bool     segmented;     /* S-bit in format type */
	bool     valid;         /* CRC checks passed */
};

/**
 * @brief Calculate CRC-16/CCITT (HDLC FCS polynomial)
 *
 * @param data  Data to calculate CRC over
 * @param len   Length of data
 * @return CRC-16 value
 */
uint16_t hdlc_crc16(const uint8_t *data, size_t len);

/**
 * @brief Build an SNRM frame (connection setup)
 *
 * @param buf         Output buffer
 * @param buf_size    Size of output buffer
 * @param client_addr Client HDLC address
 * @param server_addr Server HDLC address (1-byte)
 * @param params      Optional negotiation params (NULL for defaults)
 * @return Frame length, or negative errno
 */
int hdlc_build_snrm(uint8_t *buf, size_t buf_size,
		     uint8_t client_addr, uint8_t server_addr,
		     const struct hdlc_params *params);

/**
 * @brief Build a DISC frame (disconnect)
 *
 * @param buf         Output buffer
 * @param buf_size    Size of output buffer
 * @param client_addr Client HDLC address
 * @param server_addr Server HDLC address
 * @return Frame length, or negative errno
 */
int hdlc_build_disc(uint8_t *buf, size_t buf_size,
		     uint8_t client_addr, uint8_t server_addr);

/**
 * @brief Build an I-frame containing COSEM APDU data
 *
 * @param buf         Output buffer
 * @param buf_size    Size of output buffer
 * @param client_addr Client HDLC address
 * @param server_addr Server HDLC address
 * @param send_seq    Send sequence number (0-7)
 * @param recv_seq    Receive sequence number (0-7)
 * @param info        Information field (COSEM APDU)
 * @param info_len    Length of information field
 * @return Frame length, or negative errno
 */
int hdlc_build_iframe(uint8_t *buf, size_t buf_size,
		      uint8_t client_addr, uint8_t server_addr,
		      uint8_t send_seq, uint8_t recv_seq,
		      const uint8_t *info, size_t info_len);

/**
 * @brief Parse a received HDLC frame
 *
 * @param data  Raw received data (should start/end with 0x7E)
 * @param len   Length of received data
 * @param frame Output parsed frame structure
 * @return 0 on success, negative errno on failure
 */
int hdlc_parse_frame(const uint8_t *data, size_t len, struct hdlc_frame *frame);

/**
 * @brief Extract HDLC frame from raw buffer (find 7E...7E boundaries)
 *
 * Scans for opening and closing 0x7E flags and returns the frame.
 *
 * @param data      Raw data buffer
 * @param len       Length of raw data
 * @param frame_start  Output: offset of frame start
 * @param frame_len    Output: length of complete frame
 * @return 0 if frame found, -EAGAIN if incomplete, -EINVAL if corrupt
 */
int hdlc_find_frame(const uint8_t *data, size_t len,
		    size_t *frame_start, size_t *frame_len);

#endif /* DLMS_HDLC_H_ */
