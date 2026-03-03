/*
 * Unit Tests — HDLC Framing Layer (dlms_hdlc.c)
 *
 * Tests CRC-16 computation, frame building (SNRM, DISC, I-frame),
 * frame parsing, and frame finding in byte streams.
 */
#include "test_framework.h"
#include "dlms_hdlc.h"

/* ==== CRC-16/CCITT Tests ==== */

void test_crc16_empty(void)
{
	/* CRC of empty data should be 0xFFFF ^ 0xFFFF = 0x0000
	 * Wait — initial value is 0xFFFF, and final XOR is 0xFFFF,
	 * so CRC of zero-length data = 0xFFFF ^ 0xFFFF = 0x0000 */
	uint16_t crc = hdlc_crc16(NULL, 0);
	/* Actually, the loop doesn't execute, so crc stays 0xFFFF,
	 * then XOR with 0xFFFF → 0x0000 */
	ASSERT_EQ(0x0000, crc);
}

void test_crc16_known_vector(void)
{
	/* "123456789" should produce CRC-16/X.25 = 0x906E */
	const uint8_t data[] = "123456789";
	uint16_t crc = hdlc_crc16(data, 9);
	ASSERT_EQ(0x906E, crc);
}

void test_crc16_single_byte(void)
{
	uint8_t data[] = { 0x00 };
	uint16_t crc = hdlc_crc16(data, 1);
	/* Should be deterministic and nonzero */
	ASSERT_NE(0x0000, crc);
}

void test_crc16_consistency(void)
{
	/* Same input → same output */
	uint8_t data[] = { 0xA0, 0x07, 0x03, 0x03, 0x93 };
	uint16_t crc1 = hdlc_crc16(data, sizeof(data));
	uint16_t crc2 = hdlc_crc16(data, sizeof(data));
	ASSERT_EQ(crc1, crc2);
}

void test_crc16_different_inputs(void)
{
	uint8_t d1[] = { 0x01, 0x02, 0x03 };
	uint8_t d2[] = { 0x01, 0x02, 0x04 };
	uint16_t crc1 = hdlc_crc16(d1, 3);
	uint16_t crc2 = hdlc_crc16(d2, 3);
	ASSERT_NE(crc1, crc2);
}

/* ==== SNRM Frame Build Tests ==== */

void test_build_snrm_minimal(void)
{
	uint8_t buf[64];
	uint8_t client = HDLC_CLIENT_ADDR(1);  /* 0x03 */
	uint8_t server = HDLC_SERVER_ADDR_1B(0) | 0x01; /* 0x03 actually: (0<<1)|1=1... */
	/* For address encoding: client_sap=1 → (1<<1)|1 = 0x03 */
	/* server logical=0, physical=1 → combined=(0<<7)|1=1 → (1<<1)|1=0x03 */

	int len = hdlc_build_snrm(buf, sizeof(buf), client, server, NULL);

	/* Frame should be: 7E <format 2B> <dst> <src> <ctrl=93> <HCS 2B> 7E */
	ASSERT_GT(len, 0);
	ASSERT_EQ(0x7E, buf[0]);          /* Opening flag */
	ASSERT_EQ(0x7E, buf[len - 1]);    /* Closing flag */
	ASSERT_EQ(HDLC_CTRL_SNRM, buf[5]); /* Control = 0x93 */

	/* Format type should be 0xA0 high nibble */
	ASSERT_EQ(0xA0, buf[1] & 0xF0);

	/* Destination = server addr, Source = client addr */
	ASSERT_EQ(server, buf[3]);
	ASSERT_EQ(client, buf[4]);

	/* Minimum SNRM without info: 7E + format(2) + dst(1) + src(1) + ctrl(1) + HCS(2) + 7E = 9 */
	ASSERT_EQ(9, len);
}

void test_build_snrm_with_params(void)
{
	uint8_t buf[64];
	struct hdlc_params params = {
		.max_info_tx = 128,
		.max_info_rx = 128,
		.window_tx = 1,
		.window_rx = 1,
	};

	int len = hdlc_build_snrm(buf, sizeof(buf), 0x03, 0x03, &params);

	ASSERT_GT(len, 9);  /* Should be longer than minimal */
	ASSERT_EQ(0x7E, buf[0]);
	ASSERT_EQ(0x7E, buf[len - 1]);
	ASSERT_EQ(HDLC_CTRL_SNRM, buf[5]);

	/* Verify it has info field (SNRM negotiation params) */
	/* After HCS (bytes 6-7), there should be info starting with 0x81 0x80 */
	ASSERT_EQ(0x81, buf[8]);
	ASSERT_EQ(0x80, buf[9]);
}

void test_build_snrm_buffer_too_small(void)
{
	uint8_t buf[4];  /* Too small */
	int len = hdlc_build_snrm(buf, sizeof(buf), 0x03, 0x03, NULL);
	ASSERT_TRUE(len < 0);  /* Should return -ENOMEM */
}

/* ==== DISC Frame Build Tests ==== */

void test_build_disc(void)
{
	uint8_t buf[32];
	int len = hdlc_build_disc(buf, sizeof(buf), 0x03, 0x03);

	ASSERT_GT(len, 0);
	ASSERT_EQ(0x7E, buf[0]);
	ASSERT_EQ(0x7E, buf[len - 1]);
	ASSERT_EQ(HDLC_CTRL_DISC, buf[5]);  /* Control = 0x53 */
	ASSERT_EQ(9, len);  /* Same size as minimal SNRM (no info field) */
}

/* ==== I-Frame Build Tests ==== */

void test_build_iframe(void)
{
	uint8_t buf[64];
	uint8_t info[] = { 0xE6, 0xE6, 0x00, 0x60, 0x01 };  /* Fake LLC + AARQ start */

	int len = hdlc_build_iframe(buf, sizeof(buf), 0x03, 0x03, 0, 0, info, sizeof(info));

	ASSERT_GT(len, 0);
	ASSERT_EQ(0x7E, buf[0]);
	ASSERT_EQ(0x7E, buf[len - 1]);

	/* Control byte for I-frame: SSS P RRR 0 → send=0, recv=0, P=1 → 0x10 */
	uint8_t expected_ctrl = HDLC_CTRL_I_FRAME(0, 0, 1);
	ASSERT_EQ(expected_ctrl, buf[5]);

	/* Info field should be present: len > 9 (no-info frame size) */
	ASSERT_GT(len, 9);
}

void test_build_iframe_seq_numbers(void)
{
	uint8_t buf[64];
	uint8_t info[] = { 0x01 };

	int len = hdlc_build_iframe(buf, sizeof(buf), 0x03, 0x03, 3, 5, info, 1);

	ASSERT_GT(len, 0);
	uint8_t expected_ctrl = HDLC_CTRL_I_FRAME(3, 5, 1);
	ASSERT_EQ(expected_ctrl, buf[5]);
}

void test_build_iframe_null_info(void)
{
	uint8_t buf[64];
	int len = hdlc_build_iframe(buf, sizeof(buf), 0x03, 0x03, 0, 0, NULL, 0);
	ASSERT_TRUE(len < 0);  /* Should return -EINVAL */
}

void test_build_iframe_info_too_large(void)
{
	uint8_t buf[512];
	uint8_t info[HDLC_MAX_INFO_LEN + 1];
	memset(info, 0xAA, sizeof(info));

	int len = hdlc_build_iframe(buf, sizeof(buf), 0x03, 0x03, 0, 0,
				    info, sizeof(info));
	ASSERT_TRUE(len < 0);  /* Should return -EINVAL */
}

/* ==== Frame Parse Tests ==== */

void test_parse_snrm_roundtrip(void)
{
	uint8_t buf[64];
	int len = hdlc_build_snrm(buf, sizeof(buf), 0x03, 0x03, NULL);
	ASSERT_GT(len, 0);

	struct hdlc_frame frame;
	int ret = hdlc_parse_frame(buf, len, &frame);

	ASSERT_EQ(0, ret);
	ASSERT_TRUE(frame.valid);
	ASSERT_EQ(HDLC_CTRL_SNRM, frame.control);
	ASSERT_EQ(0x03, frame.dst_addr);
	ASSERT_EQ(0x03, frame.src_addr);
	ASSERT_EQ(0, frame.info_len);  /* Minimal SNRM has no info */
}

void test_parse_disc_roundtrip(void)
{
	uint8_t buf[32];
	int len = hdlc_build_disc(buf, sizeof(buf), 0x03, 0x03);
	ASSERT_GT(len, 0);

	struct hdlc_frame frame;
	int ret = hdlc_parse_frame(buf, len, &frame);

	ASSERT_EQ(0, ret);
	ASSERT_TRUE(frame.valid);
	ASSERT_EQ(HDLC_CTRL_DISC, frame.control);
}

void test_parse_iframe_roundtrip(void)
{
	uint8_t buf[128];
	uint8_t info[] = { 0xE6, 0xE6, 0x00, 0xC0, 0x01, 0x00 };

	int len = hdlc_build_iframe(buf, sizeof(buf), 0x03, 0x03, 0, 0,
				    info, sizeof(info));
	ASSERT_GT(len, 0);

	struct hdlc_frame frame;
	int ret = hdlc_parse_frame(buf, len, &frame);

	ASSERT_EQ(0, ret);
	ASSERT_TRUE(frame.valid);
	ASSERT_EQ(sizeof(info), frame.info_len);
	ASSERT_MEM_EQ(info, frame.info, sizeof(info));
}

void test_parse_invalid_too_short(void)
{
	uint8_t buf[] = { 0x7E, 0x7E };
	struct hdlc_frame frame;
	int ret = hdlc_parse_frame(buf, sizeof(buf), &frame);
	ASSERT_TRUE(ret < 0);
}

void test_parse_invalid_no_flags(void)
{
	uint8_t buf[] = { 0x00, 0xA0, 0x07, 0x03, 0x03, 0x93, 0x00, 0x00, 0x00 };
	struct hdlc_frame frame;
	int ret = hdlc_parse_frame(buf, sizeof(buf), &frame);
	ASSERT_TRUE(ret < 0);
}

void test_parse_null_args(void)
{
	struct hdlc_frame frame;
	ASSERT_TRUE(hdlc_parse_frame(NULL, 10, &frame) < 0);
	ASSERT_TRUE(hdlc_parse_frame((uint8_t[]){0x7E}, 9, NULL) < 0);
}

/* ==== Frame Find Tests ==== */

void test_find_frame_simple(void)
{
	uint8_t buf[32];
	int len = hdlc_build_snrm(buf, sizeof(buf), 0x03, 0x03, NULL);
	ASSERT_GT(len, 0);

	size_t fstart, flen;
	int ret = hdlc_find_frame(buf, len, &fstart, &flen);

	ASSERT_EQ(0, ret);
	ASSERT_EQ(0, (int)fstart);
	ASSERT_EQ(len, (int)flen);
}

void test_find_frame_with_garbage_prefix(void)
{
	/* Prepend garbage bytes before a valid frame */
	uint8_t raw[64];
	raw[0] = 0xFF;
	raw[1] = 0x00;
	raw[2] = 0xAA;

	uint8_t frame_buf[32];
	int flen = hdlc_build_snrm(frame_buf, sizeof(frame_buf), 0x03, 0x03, NULL);
	ASSERT_GT(flen, 0);

	memcpy(&raw[3], frame_buf, flen);

	size_t fstart, found_len;
	int ret = hdlc_find_frame(raw, 3 + flen, &fstart, &found_len);

	ASSERT_EQ(0, ret);
	ASSERT_EQ(3, (int)fstart);  /* Frame starts after garbage */
	ASSERT_EQ(flen, (int)found_len);
}

void test_find_frame_incomplete(void)
{
	/* Only opening flag, no closing */
	uint8_t buf[] = { 0x7E, 0xA0, 0x07, 0x03 };

	size_t fstart, flen;
	int ret = hdlc_find_frame(buf, sizeof(buf), &fstart, &flen);

	ASSERT_TRUE(ret < 0);  /* Should return -EAGAIN */
}

void test_find_frame_null_args(void)
{
	size_t s, l;
	ASSERT_TRUE(hdlc_find_frame(NULL, 10, &s, &l) < 0);
	ASSERT_TRUE(hdlc_find_frame((uint8_t[]){0x7E}, 1, NULL, &l) < 0);
}

/* ==== HDLC Address Macros ==== */

void test_client_addr_macro(void)
{
	/* Client SAP 1 → (1 << 1) | 1 = 0x03 */
	ASSERT_EQ(0x03, HDLC_CLIENT_ADDR(1));

	/* Client SAP 16 → (16 << 1) | 1 = 0x21 */
	ASSERT_EQ(0x21, HDLC_CLIENT_ADDR(16));

	/* Client SAP 0 → (0 << 1) | 1 = 0x01 */
	ASSERT_EQ(0x01, HDLC_CLIENT_ADDR(0));
}

void test_server_addr_macro(void)
{
	/* Server logical 0 → (0 << 1) | 1 = 0x01 */
	ASSERT_EQ(0x01, HDLC_SERVER_ADDR_1B(0));

	/* Server logical 1 → (1 << 1) | 1 = 0x03 */
	ASSERT_EQ(0x03, HDLC_SERVER_ADDR_1B(1));
}

/* ==== I-Frame Control Byte Macros ==== */

void test_iframe_control_macro(void)
{
	/* send=0, recv=0, P=1 → 0x10 */
	ASSERT_EQ(0x10, HDLC_CTRL_I_FRAME(0, 0, 1));

	/* send=1, recv=0, P=1 → 0x12 */
	ASSERT_EQ(0x12, HDLC_CTRL_I_FRAME(1, 0, 1));

	/* send=0, recv=1, P=1 → 0x30 */
	ASSERT_EQ(0x30, HDLC_CTRL_I_FRAME(0, 1, 1));

	/* send=0, recv=0, P=0 → 0x00 */
	ASSERT_EQ(0x00, HDLC_CTRL_I_FRAME(0, 0, 0));
}

void test_rr_control_macro(void)
{
	/* recv=0 → 0x01 */
	ASSERT_EQ(0x01, HDLC_CTRL_RR(0));

	/* recv=1 → 0x21 */
	ASSERT_EQ(0x21, HDLC_CTRL_RR(1));
}

/* ==== CRC Integrity: Build → Parse verifies CRC ==== */

void test_crc_integrity_snrm_with_params(void)
{
	uint8_t buf[64];
	struct hdlc_params params = {
		.max_info_tx = 256,
		.max_info_rx = 256,
		.window_tx = 7,
		.window_rx = 7,
	};

	int len = hdlc_build_snrm(buf, sizeof(buf), 0x21, 0x03, &params);
	ASSERT_GT(len, 0);

	struct hdlc_frame frame;
	int ret = hdlc_parse_frame(buf, len, &frame);
	ASSERT_EQ(0, ret);
	ASSERT_TRUE(frame.valid);
}

void test_crc_integrity_iframe_large_info(void)
{
	uint8_t buf[HDLC_MAX_FRAME_LEN];
	uint8_t info[128];
	/* Fill with pattern */
	for (int i = 0; i < 128; i++) {
		info[i] = (uint8_t)(i & 0xFF);
	}

	int len = hdlc_build_iframe(buf, sizeof(buf), 0x03, 0x03, 7, 7, info, 128);
	ASSERT_GT(len, 0);

	struct hdlc_frame frame;
	int ret = hdlc_parse_frame(buf, len, &frame);
	ASSERT_EQ(0, ret);
	ASSERT_TRUE(frame.valid);
	ASSERT_EQ(128, (int)frame.info_len);
	ASSERT_MEM_EQ(info, frame.info, 128);
}

/* ==== Test Suite Runner ==== */

void run_hdlc_tests(void)
{
	TEST_SUITE_BEGIN("HDLC");

	/* CRC-16 */
	RUN_TEST(test_crc16_empty);
	RUN_TEST(test_crc16_known_vector);
	RUN_TEST(test_crc16_single_byte);
	RUN_TEST(test_crc16_consistency);
	RUN_TEST(test_crc16_different_inputs);

	/* SNRM build */
	RUN_TEST(test_build_snrm_minimal);
	RUN_TEST(test_build_snrm_with_params);
	RUN_TEST(test_build_snrm_buffer_too_small);

	/* DISC build */
	RUN_TEST(test_build_disc);

	/* I-frame build */
	RUN_TEST(test_build_iframe);
	RUN_TEST(test_build_iframe_seq_numbers);
	RUN_TEST(test_build_iframe_null_info);
	RUN_TEST(test_build_iframe_info_too_large);

	/* Frame parse roundtrips */
	RUN_TEST(test_parse_snrm_roundtrip);
	RUN_TEST(test_parse_disc_roundtrip);
	RUN_TEST(test_parse_iframe_roundtrip);
	RUN_TEST(test_parse_invalid_too_short);
	RUN_TEST(test_parse_invalid_no_flags);
	RUN_TEST(test_parse_null_args);

	/* Frame find */
	RUN_TEST(test_find_frame_simple);
	RUN_TEST(test_find_frame_with_garbage_prefix);
	RUN_TEST(test_find_frame_incomplete);
	RUN_TEST(test_find_frame_null_args);

	/* Address macros */
	RUN_TEST(test_client_addr_macro);
	RUN_TEST(test_server_addr_macro);

	/* Control byte macros */
	RUN_TEST(test_iframe_control_macro);
	RUN_TEST(test_rr_control_macro);

	/* CRC integrity (build → parse) */
	RUN_TEST(test_crc_integrity_snrm_with_params);
	RUN_TEST(test_crc_integrity_iframe_large_info);

	TEST_SUITE_END("HDLC");
}
