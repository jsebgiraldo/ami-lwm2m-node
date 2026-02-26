/*
 * DLMS/COSEM HDLC Framing Layer — IEC 62056-46
 *
 * Implements HDLC frame encoding/decoding for DLMS over serial RS485.
 * CRC-16/CCITT with polynomial 0x8408 (reflected).
 */

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <string.h>

#include "dlms_hdlc.h"

LOG_MODULE_REGISTER(dlms_hdlc, LOG_LEVEL_DBG);

/* ---- CRC-16/CCITT lookup table (polynomial 0x8408, bit-reversed) ---- */
static const uint16_t crc16_table[256] = {
	0x0000, 0x1189, 0x2312, 0x329B, 0x4624, 0x57AD, 0x6536, 0x74BF,
	0x8C48, 0x9DC1, 0xAF5A, 0xBED3, 0xCA6C, 0xDBE5, 0xE97E, 0xF8F7,
	0x1081, 0x0108, 0x3393, 0x221A, 0x56A5, 0x472C, 0x75B7, 0x643E,
	0x9CC9, 0x8D40, 0xBFDB, 0xAE52, 0xDAED, 0xCB64, 0xF9FF, 0xE876,
	0x2102, 0x308B, 0x0210, 0x1399, 0x6726, 0x76AF, 0x4434, 0x55BD,
	0xAD4A, 0xBCC3, 0x8E58, 0x9FD1, 0xEB6E, 0xFAE7, 0xC87C, 0xD9F5,
	0x3183, 0x200A, 0x1291, 0x0318, 0x77A7, 0x662E, 0x54B5, 0x453C,
	0xBDCB, 0xAC42, 0x9ED9, 0x8F50, 0xFBEF, 0xEA66, 0xD8FD, 0xC974,
	0x4204, 0x538D, 0x6116, 0x709F, 0x0420, 0x15A9, 0x2732, 0x36BB,
	0xCE4C, 0xDFC5, 0xED5E, 0xFCD7, 0x8868, 0x99E1, 0xAB7A, 0xBAF3,
	0x5285, 0x430C, 0x7197, 0x601E, 0x14A1, 0x0528, 0x37B3, 0x263A,
	0xDECD, 0xCF44, 0xFDDF, 0xEC56, 0x98E9, 0x8960, 0xBBFB, 0xAA72,
	0x6306, 0x728F, 0x4014, 0x519D, 0x2522, 0x34AB, 0x0630, 0x17B9,
	0xEF4E, 0xFEC7, 0xCC5C, 0xDDD5, 0xA96A, 0xB8E3, 0x8A78, 0x9BF1,
	0x7387, 0x620E, 0x5095, 0x411C, 0x35A3, 0x242A, 0x16B1, 0x0738,
	0xFFCF, 0xEE46, 0xDCDD, 0xCD54, 0xB9EB, 0xA862, 0x9AF9, 0x8B70,
	0x8408, 0x9581, 0xA71A, 0xB693, 0xC22C, 0xD3A5, 0xE13E, 0xF0B7,
	0x0840, 0x19C9, 0x2B52, 0x3ADB, 0x4E64, 0x5FED, 0x6D76, 0x7CFF,
	0x9489, 0x8500, 0xB79B, 0xA612, 0xD2AD, 0xC324, 0xF1BF, 0xE036,
	0x18C1, 0x0948, 0x3BD3, 0x2A5A, 0x5EE5, 0x4F6C, 0x7DF7, 0x6C7E,
	0xA50A, 0xB483, 0x8618, 0x9791, 0xE32E, 0xF2A7, 0xC03C, 0xD1B5,
	0x2942, 0x38CB, 0x0A50, 0x1BD9, 0x6F66, 0x7EEF, 0x4C74, 0x5DFD,
	0xB58B, 0xA402, 0x9699, 0x8710, 0xF3AF, 0xE226, 0xD0BD, 0xC134,
	0x39C3, 0x284A, 0x1AD1, 0x0B58, 0x7FE7, 0x6E6E, 0x5CF5, 0x4D7C,
	0xC60C, 0xD785, 0xE51E, 0xF497, 0x8028, 0x91A1, 0xA33A, 0xB2B3,
	0x4A44, 0x5BCD, 0x6956, 0x78DF, 0x0C60, 0x1DE9, 0x2F72, 0x3EFB,
	0xD68D, 0xC704, 0xF59F, 0xE416, 0x90A9, 0x8120, 0xB3BB, 0xA232,
	0x5AC5, 0x4B4C, 0x79D7, 0x685E, 0x1CE1, 0x0D68, 0x3FF3, 0x2E7A,
	0xE70E, 0xF687, 0xC41C, 0xD595, 0xA12A, 0xB0A3, 0x8238, 0x93B1,
	0x6B46, 0x7ACF, 0x4854, 0x59DD, 0x2D62, 0x3CEB, 0x0E70, 0x1FF9,
	0xF78F, 0xE606, 0xD49D, 0xC514, 0xB1AB, 0xA022, 0x92B9, 0x8330,
	0x7BC7, 0x6A4E, 0x58D5, 0x495C, 0x3DE3, 0x2C6A, 0x1EF1, 0x0F78,
};

uint16_t hdlc_crc16(const uint8_t *data, size_t len)
{
	uint16_t crc = 0xFFFF;

	for (size_t i = 0; i < len; i++) {
		crc = (crc >> 8) ^ crc16_table[(crc ^ data[i]) & 0xFF];
	}

	return crc ^ 0xFFFF;
}

/* ---- Internal: build header (format + addresses) ---- */
static int build_header(uint8_t *buf, size_t buf_size,
			uint8_t dst_addr, uint8_t src_addr,
			uint8_t control, bool has_info, uint16_t info_len)
{
	/*
	 * Frame layout:
	 *   7E | Format(2) | DstAddr(1) | SrcAddr(1) | Control(1) | HCS(2) |
	 *   [Info(N) | FCS(2)] | 7E
	 *
	 * Format field: ASSL LLLL LLLL LLLL
	 *   A = format type (1 = Type 3)
	 *   S = segmented flag
	 *   L = frame length (excluding flags)
	 */
	uint16_t header_len = 2 + 1 + 1 + 1;  /* Format + DstAddr + SrcAddr + Control */
	uint16_t frame_len = header_len + 2;    /* + HCS */

	if (has_info) {
		frame_len += info_len + 2;  /* + Info + FCS */
	}

	/* Total with flags */
	if ((size_t)(frame_len + 2) > buf_size) {
		return -ENOMEM;
	}

	/* Opening flag */
	buf[0] = HDLC_FLAG;

	/* Format type (2 bytes): type 3, frame_len in low 11 bits */
	buf[1] = HDLC_FORMAT_TYPE | ((frame_len >> 8) & 0x07);
	buf[2] = frame_len & 0xFF;

	/* Destination address (server when sending, client when receiving) */
	buf[3] = dst_addr;

	/* Source address */
	buf[4] = src_addr;

	/* Control byte */
	buf[5] = control;

	return 6;  /* Header length in buffer (including opening flag) */
}

int hdlc_build_snrm(uint8_t *buf, size_t buf_size,
		     uint8_t client_addr, uint8_t server_addr,
		     const struct hdlc_params *params)
{
	/*
	 * SNRM frame with optional information field for parameter negotiation.
	 * Format: 7E | Header | HCS | [SNRM-Info | FCS] | 7E
	 *
	 * SNRM info field (optional):
	 *   81 80 <len> <parameters>
	 * where parameters are TLV:
	 *   05 <len> <max_info_tx>     (max transmit info field length)
	 *   06 <len> <max_info_rx>     (max receive info field length)
	 *   07 01 <window_tx>          (transmit window size)
	 *   08 01 <window_rx>          (receive window size)
	 */
	uint8_t snrm_info[32];
	uint16_t info_len = 0;
	bool has_info = false;

	if (params) {
		has_info = true;
		snrm_info[0] = 0x81;
		snrm_info[1] = 0x80;
		/* Length placeholder at [2] */
		uint8_t plen = 0;

		/* Max info field transmit */
		snrm_info[3] = 0x05;
		if (params->max_info_tx <= 0xFF) {
			snrm_info[4] = 0x01;
			snrm_info[5] = (uint8_t)params->max_info_tx;
			plen += 3;
		} else {
			snrm_info[4] = 0x02;
			snrm_info[5] = (params->max_info_tx >> 8) & 0xFF;
			snrm_info[6] = params->max_info_tx & 0xFF;
			plen += 4;
		}

		/* Max info field receive */
		uint8_t idx = 3 + plen;
		snrm_info[idx++] = 0x06;
		if (params->max_info_rx <= 0xFF) {
			snrm_info[idx++] = 0x01;
			snrm_info[idx++] = (uint8_t)params->max_info_rx;
		} else {
			snrm_info[idx++] = 0x02;
			snrm_info[idx++] = (params->max_info_rx >> 8) & 0xFF;
			snrm_info[idx++] = params->max_info_rx & 0xFF;
		}

		/* Window sizes */
		snrm_info[idx++] = 0x07;
		snrm_info[idx++] = 0x01;
		snrm_info[idx++] = params->window_tx;

		snrm_info[idx++] = 0x08;
		snrm_info[idx++] = 0x01;
		snrm_info[idx++] = params->window_rx;

		snrm_info[2] = idx - 3;  /* Content length */
		info_len = idx;
	}

	int pos = build_header(buf, buf_size, server_addr, client_addr,
			       HDLC_CTRL_SNRM, has_info, info_len);
	if (pos < 0) {
		return pos;
	}

	/* HCS (over format + addresses + control = bytes 1..5) */
	uint16_t hcs = hdlc_crc16(&buf[1], 5);
	buf[pos++] = hcs & 0xFF;
	buf[pos++] = (hcs >> 8) & 0xFF;

	if (has_info) {
		/* Copy SNRM info */
		memcpy(&buf[pos], snrm_info, info_len);
		pos += info_len;

		/* FCS (over everything from format to end of info) */
		uint16_t fcs = hdlc_crc16(&buf[1], pos - 1);
		buf[pos++] = fcs & 0xFF;
		buf[pos++] = (fcs >> 8) & 0xFF;
	}

	/* Closing flag */
	buf[pos++] = HDLC_FLAG;

	LOG_DBG("SNRM frame built: %d bytes", pos);
	return pos;
}

int hdlc_build_disc(uint8_t *buf, size_t buf_size,
		     uint8_t client_addr, uint8_t server_addr)
{
	int pos = build_header(buf, buf_size, server_addr, client_addr,
			       HDLC_CTRL_DISC, false, 0);
	if (pos < 0) {
		return pos;
	}

	/* HCS (also serves as FCS for frames without info field) */
	uint16_t hcs = hdlc_crc16(&buf[1], 5);
	buf[pos++] = hcs & 0xFF;
	buf[pos++] = (hcs >> 8) & 0xFF;

	/* Closing flag */
	buf[pos++] = HDLC_FLAG;

	LOG_DBG("DISC frame built: %d bytes", pos);
	return pos;
}

int hdlc_build_iframe(uint8_t *buf, size_t buf_size,
		      uint8_t client_addr, uint8_t server_addr,
		      uint8_t send_seq, uint8_t recv_seq,
		      const uint8_t *info, size_t info_len)
{
	if (!info || info_len == 0 || info_len > HDLC_MAX_INFO_LEN) {
		return -EINVAL;
	}

	uint8_t ctrl = HDLC_CTRL_I_FRAME(send_seq, recv_seq, 1);

	int pos = build_header(buf, buf_size, server_addr, client_addr,
			       ctrl, true, info_len);
	if (pos < 0) {
		return pos;
	}

	/* HCS */
	uint16_t hcs = hdlc_crc16(&buf[1], 5);
	buf[pos++] = hcs & 0xFF;
	buf[pos++] = (hcs >> 8) & 0xFF;

	/* Information field */
	memcpy(&buf[pos], info, info_len);
	pos += info_len;

	/* FCS (over format + addresses + control + HCS + info) */
	uint16_t fcs = hdlc_crc16(&buf[1], pos - 1);
	buf[pos++] = fcs & 0xFF;
	buf[pos++] = (fcs >> 8) & 0xFF;

	/* Closing flag */
	buf[pos++] = HDLC_FLAG;

	LOG_DBG("I-frame built: %d bytes, SSS=%d RRR=%d", pos, send_seq, recv_seq);
	return pos;
}

int hdlc_parse_frame(const uint8_t *data, size_t len, struct hdlc_frame *frame)
{
	if (!data || !frame || len < 9) {
		/* Minimum: flag(1) + format(2) + dst(1) + src(1) + ctrl(1) + HCS(2) + flag(1) = 9 */
		return -EINVAL;
	}

	memset(frame, 0, sizeof(*frame));

	/* Verify flags */
	if (data[0] != HDLC_FLAG || data[len - 1] != HDLC_FLAG) {
		LOG_WRN("HDLC: Missing frame flags");
		return -EINVAL;
	}

	/* Parse format type */
	uint8_t format_hi = data[1];
	uint8_t format_lo = data[2];

	if ((format_hi & 0xF0) != HDLC_FORMAT_TYPE) {
		LOG_WRN("HDLC: Invalid format type: 0x%02X", format_hi);
		return -EINVAL;
	}

	frame->segmented = (format_hi & 0x08) != 0;
	uint16_t frame_len = ((format_hi & 0x07) << 8) | format_lo;

	/* Verify frame length against actual data */
	if (frame_len + 2 != len) {
		LOG_WRN("HDLC: Length mismatch: format says %u, got %u",
			frame_len, (unsigned)len - 2);
		/* Be lenient — some meters send slightly different lengths */
	}

	/* Parse addresses (1-byte each for simple meters) */
	frame->dst_addr = data[3];
	frame->src_addr = data[4];

	/* Control byte */
	frame->control = data[5];

	/* Verify HCS (over bytes 1..5) */
	uint16_t hcs_calc = hdlc_crc16(&data[1], 5);
	uint16_t hcs_recv = data[6] | (data[7] << 8);

	if (hcs_calc != hcs_recv) {
		LOG_WRN("HDLC: HCS mismatch: calc=0x%04X recv=0x%04X", hcs_calc, hcs_recv);
		frame->valid = false;
		return -EIO;
	}

	/* Check if there's an information field */
	/* Frame without info: flag(1) + format(2) + dst(1) + src(1) + ctrl(1) + HCS(2) + flag(1) = 9 */
	/* Frame with info: ... + info(N) + FCS(2) */
	if (len > 9) {
		/* Information field starts at offset 8, ends before FCS(2) + flag(1) */
		frame->info_len = len - 9 - 2;  /* total - header(8) - flag(1) - FCS(2) */
		if (frame->info_len > HDLC_MAX_INFO_LEN) {
			LOG_WRN("HDLC: Info field too large: %u", frame->info_len);
			return -ENOMEM;
		}

		memcpy(frame->info, &data[8], frame->info_len);

		/* Verify FCS (over bytes 1 to end-of-info, i.e. data[1..len-3]) */
		uint16_t fcs_calc = hdlc_crc16(&data[1], len - 4);
		uint16_t fcs_recv = data[len - 3] | (data[len - 2] << 8);

		if (fcs_calc != fcs_recv) {
			LOG_WRN("HDLC: FCS mismatch: calc=0x%04X recv=0x%04X",
				fcs_calc, fcs_recv);
			frame->valid = false;
			return -EIO;
		}
	}

	frame->valid = true;
	LOG_DBG("HDLC: Parsed frame: dst=0x%02X src=0x%02X ctrl=0x%02X info_len=%u",
		frame->dst_addr, frame->src_addr, frame->control, frame->info_len);

	return 0;
}

int hdlc_find_frame(const uint8_t *data, size_t len,
		    size_t *frame_start, size_t *frame_len)
{
	if (!data || len < 2 || !frame_start || !frame_len) {
		return -EINVAL;
	}

	/* Find opening flag */
	size_t start = 0;
	while (start < len && data[start] != HDLC_FLAG) {
		start++;
	}

	if (start >= len) {
		return -EAGAIN;
	}

	/* Find closing flag (skip consecutive opening flags) */
	size_t end = start + 1;
	while (end < len && data[end] == HDLC_FLAG) {
		start = end;  /* Skip consecutive flags */
		end++;
	}

	/* Now find the actual closing flag */
	while (end < len && data[end] != HDLC_FLAG) {
		end++;
	}

	if (end >= len) {
		return -EAGAIN;  /* Incomplete frame */
	}

	*frame_start = start;
	*frame_len = end - start + 1;

	return 0;
}
