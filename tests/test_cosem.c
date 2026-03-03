/*
 * Unit Tests — COSEM Application Layer (dlms_cosem.c)
 *
 * Tests AARQ PDU construction, AARE response parsing,
 * GET.request building, data type decoding (all COSEM types),
 * GET.response parsing, and RLRQ building.
 */
#include "test_framework.h"
#include "dlms_cosem.h"

/* ==== AARQ Build Tests ==== */

void test_aarq_no_password(void)
{
	uint8_t buf[128];
	int len = cosem_build_aarq(buf, sizeof(buf), NULL, 0);

	ASSERT_GT(len, 0);
	ASSERT_EQ(COSEM_TAG_AARQ, buf[0]);  /* 0x60 */

	/* Overall length in byte 1 */
	ASSERT_EQ(len - 2, buf[1]);

	/* Should contain Application Context [A1] */
	int found_a1 = 0;
	for (int i = 2; i < len; i++) {
		if (buf[i] == 0xA1) { found_a1 = 1; break; }
	}
	ASSERT_TRUE(found_a1);

	/* Should contain User Info [BE] */
	int found_be = 0;
	for (int i = 2; i < len; i++) {
		if (buf[i] == 0xBE) { found_be = 1; break; }
	}
	ASSERT_TRUE(found_be);

	/* Should NOT contain authentication tags when no password */
	int found_8a = 0;
	for (int i = 2; i < len; i++) {
		if (buf[i] == 0x8A) { found_8a = 1; break; }
	}
	ASSERT_FALSE(found_8a);
}

void test_aarq_with_password(void)
{
	uint8_t buf[128];
	const uint8_t pwd[] = "22222222";
	int len = cosem_build_aarq(buf, sizeof(buf), pwd, 8);

	ASSERT_GT(len, 0);
	ASSERT_EQ(COSEM_TAG_AARQ, buf[0]);

	/* Should be longer than without password */
	uint8_t buf2[128];
	int len_no_pwd = cosem_build_aarq(buf2, sizeof(buf2), NULL, 0);
	ASSERT_GT(len, len_no_pwd);

	/* Should contain sender ACSE requirements [8A] */
	int found_8a = 0;
	for (int i = 2; i < len; i++) {
		if (buf[i] == 0x8A) { found_8a = 1; break; }
	}
	ASSERT_TRUE(found_8a);

	/* Should contain mechanism name [8B] */
	int found_8b = 0;
	for (int i = 2; i < len; i++) {
		if (buf[i] == 0x8B) { found_8b = 1; break; }
	}
	ASSERT_TRUE(found_8b);

	/* Should contain authentication value [AC] */
	int found_ac = 0;
	for (int i = 2; i < len; i++) {
		if (buf[i] == 0xAC) { found_ac = 1; break; }
	}
	ASSERT_TRUE(found_ac);

	/* Password bytes should appear in the PDU */
	int found_pwd = 0;
	for (int i = 0; i < len - 8; i++) {
		if (memcmp(&buf[i], pwd, 8) == 0) { found_pwd = 1; break; }
	}
	ASSERT_TRUE(found_pwd);
}

void test_aarq_buffer_too_small(void)
{
	uint8_t buf[10];  /* Too small */
	int len = cosem_build_aarq(buf, sizeof(buf), NULL, 0);
	ASSERT_TRUE(len < 0);
}

void test_aarq_null_buffer(void)
{
	int len = cosem_build_aarq(NULL, 128, NULL, 0);
	ASSERT_TRUE(len < 0);
}

/* ==== AARE Parse Tests ==== */

void test_aare_accepted(void)
{
	/* Minimal AARE with Association Result = 0 (accepted) */
	uint8_t aare[] = {
		0x61, 0x0A,                     /* AARE tag + length */
		0xA1, 0x03, 0x06, 0x01, 0x00,  /* filler */
		0xA2, 0x03, 0x02, 0x01, 0x00,  /* Association Result = 0 (accepted) */
	};

	int ret = cosem_parse_aare(aare, sizeof(aare));
	ASSERT_EQ(0, ret);
}

void test_aare_rejected(void)
{
	uint8_t aare[] = {
		0x61, 0x0A,
		0xA1, 0x03, 0x06, 0x01, 0x00,
		0xA2, 0x03, 0x02, 0x01, 0x01,  /* Association Result = 1 (rejected-permanent) */
	};

	int ret = cosem_parse_aare(aare, sizeof(aare));
	ASSERT_TRUE(ret < 0);  /* Should return -EACCES */
}

void test_aare_wrong_tag(void)
{
	uint8_t data[] = { 0x60, 0x05, 0x00, 0x00, 0x00, 0x00, 0x00 };
	int ret = cosem_parse_aare(data, sizeof(data));
	ASSERT_TRUE(ret < 0);  /* Wrong tag (0x60 is AARQ, not AARE) */
}

void test_aare_null(void)
{
	int ret = cosem_parse_aare(NULL, 10);
	ASSERT_TRUE(ret < 0);
}

void test_aare_too_short(void)
{
	uint8_t data[] = { 0x61, 0x00 };
	int ret = cosem_parse_aare(data, sizeof(data));
	ASSERT_TRUE(ret < 0);  /* Too short to find A2 */
}

/* ==== GET.request Build Tests ==== */

void test_get_request_normal(void)
{
	uint8_t buf[32];
	struct cosem_attr_desc attr = {
		.class_id = 3,  /* Register */
		.obis = obis(1, 1, 32, 7, 0, 255),  /* Voltage Phase A */
		.attribute_id = 2,  /* Value */
	};

	int len = cosem_build_get_request(buf, sizeof(buf), 0x01, &attr);

	ASSERT_GT(len, 0);
	ASSERT_EQ(COSEM_TAG_GET_REQUEST, buf[0]);  /* 0xC0 */
	ASSERT_EQ(GET_REQUEST_NORMAL, buf[1]);      /* 0x01 */
	ASSERT_EQ(0x01, buf[2]);                    /* invoke_id */

	/* Class ID */
	ASSERT_EQ(0x00, buf[3]);
	ASSERT_EQ(0x03, buf[4]);

	/* OBIS code: 1-1:32.7.0*255 */
	ASSERT_EQ(1,   buf[5]);
	ASSERT_EQ(1,   buf[6]);
	ASSERT_EQ(32,  buf[7]);
	ASSERT_EQ(7,   buf[8]);
	ASSERT_EQ(0,   buf[9]);
	ASSERT_EQ(255, buf[10]);

	/* Attribute ID */
	ASSERT_EQ(2, buf[11]);

	/* Access selection = 0 */
	ASSERT_EQ(0x00, buf[12]);

	ASSERT_EQ(13, len);
}

void test_get_request_different_obis(void)
{
	uint8_t buf[32];
	struct cosem_attr_desc attr = {
		.class_id = 3,
		.obis = obis(1, 1, 1, 8, 0, 255),  /* Active Energy Import */
		.attribute_id = 2,
	};

	int len = cosem_build_get_request(buf, sizeof(buf), 0x0A, &attr);

	ASSERT_GT(len, 0);
	ASSERT_EQ(0x0A, buf[2]);  /* invoke_id */
	ASSERT_EQ(1, buf[7]);     /* C = 1 (total active) */
	ASSERT_EQ(8, buf[8]);     /* D = 8 (energy) */
}

void test_get_request_null_args(void)
{
	uint8_t buf[32];
	struct cosem_attr_desc attr = { .class_id = 3, .obis = obis(1,1,32,7,0,255), .attribute_id = 2 };

	ASSERT_TRUE(cosem_build_get_request(NULL, 32, 0, &attr) < 0);
	ASSERT_TRUE(cosem_build_get_request(buf, 32, 0, NULL) < 0);
}

void test_get_request_buffer_too_small(void)
{
	uint8_t buf[5];
	struct cosem_attr_desc attr = { .class_id = 3, .obis = obis(1,1,32,7,0,255), .attribute_id = 2 };

	int len = cosem_build_get_request(buf, sizeof(buf), 0, &attr);
	ASSERT_TRUE(len < 0);
}

/* ==== COSEM Data Decode Tests ==== */

void test_decode_null_data(void)
{
	uint8_t data[] = { COSEM_TYPE_NULL_DATA };
	struct cosem_get_result result;

	int consumed = cosem_decode_data(data, sizeof(data), &result);
	ASSERT_EQ(1, consumed);
	ASSERT_EQ(COSEM_TYPE_NULL_DATA, result.data_type);
}

void test_decode_boolean(void)
{
	uint8_t data[] = { COSEM_TYPE_BOOLEAN, 0x01 };
	struct cosem_get_result result;

	int consumed = cosem_decode_data(data, sizeof(data), &result);
	ASSERT_EQ(2, consumed);
	ASSERT_EQ(1, (int)result.value.u64);
}

void test_decode_uint8(void)
{
	uint8_t data[] = { COSEM_TYPE_UINT8, 0xFF };
	struct cosem_get_result result;

	int consumed = cosem_decode_data(data, sizeof(data), &result);
	ASSERT_EQ(2, consumed);
	ASSERT_EQ(255, (int)result.value.u64);
}

void test_decode_int8(void)
{
	uint8_t data[] = { COSEM_TYPE_INT8, 0x80 };  /* -128 */
	struct cosem_get_result result;

	int consumed = cosem_decode_data(data, sizeof(data), &result);
	ASSERT_EQ(2, consumed);
	ASSERT_EQ(-128, (int)result.value.i64);
}

void test_decode_uint16(void)
{
	uint8_t data[] = { COSEM_TYPE_UINT16, 0x04, 0xD2 };  /* 1234 */
	struct cosem_get_result result;

	int consumed = cosem_decode_data(data, sizeof(data), &result);
	ASSERT_EQ(3, consumed);
	ASSERT_EQ(1234, (int)result.value.u64);
}

void test_decode_int16(void)
{
	uint8_t data[] = { COSEM_TYPE_INT16, 0xFF, 0x9C };  /* -100 */
	struct cosem_get_result result;

	int consumed = cosem_decode_data(data, sizeof(data), &result);
	ASSERT_EQ(3, consumed);
	ASSERT_EQ(-100, (int)result.value.i64);
}

void test_decode_uint32(void)
{
	uint8_t data[] = { COSEM_TYPE_UINT32, 0x00, 0x01, 0x51, 0x80 };  /* 86400 */
	struct cosem_get_result result;

	int consumed = cosem_decode_data(data, sizeof(data), &result);
	ASSERT_EQ(5, consumed);
	ASSERT_EQ(86400, (int)result.value.u64);
}

void test_decode_int32(void)
{
	uint8_t data[] = { COSEM_TYPE_INT32, 0xFF, 0xFF, 0xFF, 0x9C };  /* -100 */
	struct cosem_get_result result;

	int consumed = cosem_decode_data(data, sizeof(data), &result);
	ASSERT_EQ(5, consumed);
	ASSERT_EQ(-100, (int)result.value.i64);
}

void test_decode_uint64(void)
{
	uint8_t data[] = { COSEM_TYPE_UINT64,
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x2A };  /* 42 */
	struct cosem_get_result result;

	int consumed = cosem_decode_data(data, sizeof(data), &result);
	ASSERT_EQ(9, consumed);
	ASSERT_EQ(42, (int)result.value.u64);
}

void test_decode_int64(void)
{
	uint8_t data[] = { COSEM_TYPE_INT64,
		0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xD6 };  /* -42 */
	struct cosem_get_result result;

	int consumed = cosem_decode_data(data, sizeof(data), &result);
	ASSERT_EQ(9, consumed);
	ASSERT_EQ(-42, (int)result.value.i64);
}

void test_decode_float32(void)
{
	/* IEEE 754 float32: 3.14f = 0x4048F5C3 */
	uint8_t data[] = { COSEM_TYPE_FLOAT32, 0x40, 0x48, 0xF5, 0xC3 };
	struct cosem_get_result result;

	int consumed = cosem_decode_data(data, sizeof(data), &result);
	ASSERT_EQ(5, consumed);
	ASSERT_FLOAT_EQ(3.14, result.value.f64, 0.01);
}

void test_decode_float64(void)
{
	/* IEEE 754 float64: 2.718281828 = 0x4005BF0A8B145769 */
	uint8_t data[] = { COSEM_TYPE_FLOAT64,
		0x40, 0x05, 0xBF, 0x0A, 0x8B, 0x14, 0x57, 0x69 };
	struct cosem_get_result result;

	int consumed = cosem_decode_data(data, sizeof(data), &result);
	ASSERT_EQ(9, consumed);
	ASSERT_FLOAT_EQ(2.718281828, result.value.f64, 0.000001);
}

void test_decode_octet_string(void)
{
	uint8_t data[] = { COSEM_TYPE_OCTET_STRING, 0x04, 0xDE, 0xAD, 0xBE, 0xEF };
	struct cosem_get_result result;

	int consumed = cosem_decode_data(data, sizeof(data), &result);
	ASSERT_EQ(6, consumed);
	ASSERT_EQ(4, (int)result.value.raw.len);
	ASSERT_EQ(0xDE, result.value.raw.data[0]);
	ASSERT_EQ(0xEF, result.value.raw.data[3]);
}

void test_decode_visible_string(void)
{
	uint8_t data[] = { COSEM_TYPE_VISIBLE_STRING, 0x03, 'A', 'B', 'C' };
	struct cosem_get_result result;

	int consumed = cosem_decode_data(data, sizeof(data), &result);
	ASSERT_EQ(5, consumed);
	ASSERT_EQ(3, (int)result.value.raw.len);
	ASSERT_EQ('A', result.value.raw.data[0]);
	ASSERT_EQ('C', result.value.raw.data[2]);
}

void test_decode_enum(void)
{
	uint8_t data[] = { COSEM_TYPE_ENUM, 0x1B };  /* unit = 27 (W) */
	struct cosem_get_result result;

	int consumed = cosem_decode_data(data, sizeof(data), &result);
	ASSERT_EQ(2, consumed);
	ASSERT_EQ(0x1B, (int)result.value.u64);
}

void test_decode_structure(void)
{
	uint8_t data[] = { COSEM_TYPE_STRUCTURE, 0x02 };  /* 2 elements */
	struct cosem_get_result result;

	int consumed = cosem_decode_data(data, sizeof(data), &result);
	ASSERT_EQ(2, consumed);
	ASSERT_EQ(2, (int)result.value.u64);  /* Element count */
}

void test_decode_array(void)
{
	uint8_t data[] = { COSEM_TYPE_ARRAY, 0x05 };  /* 5 elements */
	struct cosem_get_result result;

	int consumed = cosem_decode_data(data, sizeof(data), &result);
	ASSERT_EQ(2, consumed);
	ASSERT_EQ(5, (int)result.value.u64);
}

void test_decode_unknown_type(void)
{
	uint8_t data[] = { 0xFE, 0x00 };  /* Unknown type */
	struct cosem_get_result result;

	int consumed = cosem_decode_data(data, sizeof(data), &result);
	ASSERT_TRUE(consumed < 0);  /* -ENOTSUP */
}

void test_decode_truncated_uint16(void)
{
	uint8_t data[] = { COSEM_TYPE_UINT16, 0x04 };  /* Missing second byte */
	struct cosem_get_result result;

	int consumed = cosem_decode_data(data, sizeof(data), &result);
	ASSERT_TRUE(consumed < 0);  /* -ENODATA */
}

void test_decode_truncated_uint32(void)
{
	uint8_t data[] = { COSEM_TYPE_UINT32, 0x00, 0x01 };
	struct cosem_get_result result;

	int consumed = cosem_decode_data(data, sizeof(data), &result);
	ASSERT_TRUE(consumed < 0);
}

void test_decode_null_args(void)
{
	struct cosem_get_result result;
	uint8_t data[] = { COSEM_TYPE_UINT8, 0x42 };

	ASSERT_TRUE(cosem_decode_data(NULL, 2, &result) < 0);
	ASSERT_TRUE(cosem_decode_data(data, 2, NULL) < 0);
	ASSERT_TRUE(cosem_decode_data(data, 0, &result) < 0);
}

/* ==== GET.response Parse Tests ==== */

void test_get_response_normal_uint32(void)
{
	/* C4 01 <invoke=01> <choice=00> <uint32: 06 00 01 51 80> */
	uint8_t data[] = {
		COSEM_TAG_GET_RESPONSE, GET_RESPONSE_NORMAL, 0x01,
		0x00,  /* Data choice (success) */
		COSEM_TYPE_UINT32, 0x00, 0x01, 0x51, 0x80,  /* 86400 */
	};

	struct cosem_get_result result;
	int ret = cosem_parse_get_response(data, sizeof(data), &result);

	ASSERT_EQ(0, ret);
	ASSERT_TRUE(result.success);
	ASSERT_EQ(86400, (int)result.value.u64);
}

void test_get_response_normal_int16(void)
{
	uint8_t data[] = {
		COSEM_TAG_GET_RESPONSE, GET_RESPONSE_NORMAL, 0x01,
		0x00,
		COSEM_TYPE_INT16, 0xFF, 0x9C,  /* -100 */
	};

	struct cosem_get_result result;
	int ret = cosem_parse_get_response(data, sizeof(data), &result);

	ASSERT_EQ(0, ret);
	ASSERT_TRUE(result.success);
	ASSERT_EQ(-100, (int)result.value.i64);
}

void test_get_response_access_error(void)
{
	/* C4 01 01 01 04 → Data-Access-Result error 4 (read-write-denied) */
	uint8_t data[] = {
		COSEM_TAG_GET_RESPONSE, GET_RESPONSE_NORMAL, 0x01,
		0x01,  /* Error choice */
		0x04,  /* Error code 4 */
	};

	struct cosem_get_result result;
	int ret = cosem_parse_get_response(data, sizeof(data), &result);

	ASSERT_TRUE(ret < 0);
	ASSERT_FALSE(result.success);
}

void test_get_response_wrong_tag(void)
{
	uint8_t data[] = { 0xC0, 0x01, 0x01, 0x00, COSEM_TYPE_UINT8, 0x42 };
	struct cosem_get_result result;

	int ret = cosem_parse_get_response(data, sizeof(data), &result);
	ASSERT_TRUE(ret < 0);  /* Wrong tag */
}

void test_get_response_null_args(void)
{
	uint8_t data[] = { COSEM_TAG_GET_RESPONSE, 0x01, 0x01, 0x00, COSEM_TYPE_UINT8, 0x42 };
	struct cosem_get_result result;

	ASSERT_TRUE(cosem_parse_get_response(NULL, 6, &result) < 0);
	ASSERT_TRUE(cosem_parse_get_response(data, 6, NULL) < 0);
}

/* ==== RLRQ Build Tests ==== */

void test_rlrq_build(void)
{
	uint8_t buf[16];
	int len = cosem_build_rlrq(buf, sizeof(buf));

	ASSERT_EQ(2, len);
	ASSERT_EQ(COSEM_TAG_RLRQ, buf[0]);  /* 0x62 */
	ASSERT_EQ(0x00, buf[1]);
}

void test_rlrq_buffer_too_small(void)
{
	uint8_t buf[1];
	int len = cosem_build_rlrq(buf, sizeof(buf));
	ASSERT_TRUE(len < 0);
}

void test_rlrq_null_buffer(void)
{
	int len = cosem_build_rlrq(NULL, 16);
	ASSERT_TRUE(len < 0);
}

/* ==== OBIS Helper ==== */

void test_obis_helper(void)
{
	struct obis_code o = obis(1, 1, 32, 7, 0, 255);
	ASSERT_EQ(1,   o.a);
	ASSERT_EQ(1,   o.b);
	ASSERT_EQ(32,  o.c);
	ASSERT_EQ(7,   o.d);
	ASSERT_EQ(0,   o.e);
	ASSERT_EQ(255, o.f);
}

/* ==== Test Suite Runner ==== */

void run_cosem_tests(void)
{
	TEST_SUITE_BEGIN("COSEM");

	/* AARQ */
	RUN_TEST(test_aarq_no_password);
	RUN_TEST(test_aarq_with_password);
	RUN_TEST(test_aarq_buffer_too_small);
	RUN_TEST(test_aarq_null_buffer);

	/* AARE parse */
	RUN_TEST(test_aare_accepted);
	RUN_TEST(test_aare_rejected);
	RUN_TEST(test_aare_wrong_tag);
	RUN_TEST(test_aare_null);
	RUN_TEST(test_aare_too_short);

	/* GET.request */
	RUN_TEST(test_get_request_normal);
	RUN_TEST(test_get_request_different_obis);
	RUN_TEST(test_get_request_null_args);
	RUN_TEST(test_get_request_buffer_too_small);

	/* Data decode — all types */
	RUN_TEST(test_decode_null_data);
	RUN_TEST(test_decode_boolean);
	RUN_TEST(test_decode_uint8);
	RUN_TEST(test_decode_int8);
	RUN_TEST(test_decode_uint16);
	RUN_TEST(test_decode_int16);
	RUN_TEST(test_decode_uint32);
	RUN_TEST(test_decode_int32);
	RUN_TEST(test_decode_uint64);
	RUN_TEST(test_decode_int64);
	RUN_TEST(test_decode_float32);
	RUN_TEST(test_decode_float64);
	RUN_TEST(test_decode_octet_string);
	RUN_TEST(test_decode_visible_string);
	RUN_TEST(test_decode_enum);
	RUN_TEST(test_decode_structure);
	RUN_TEST(test_decode_array);
	RUN_TEST(test_decode_unknown_type);
	RUN_TEST(test_decode_truncated_uint16);
	RUN_TEST(test_decode_truncated_uint32);
	RUN_TEST(test_decode_null_args);

	/* GET.response parse */
	RUN_TEST(test_get_response_normal_uint32);
	RUN_TEST(test_get_response_normal_int16);
	RUN_TEST(test_get_response_access_error);
	RUN_TEST(test_get_response_wrong_tag);
	RUN_TEST(test_get_response_null_args);

	/* RLRQ */
	RUN_TEST(test_rlrq_build);
	RUN_TEST(test_rlrq_buffer_too_small);
	RUN_TEST(test_rlrq_null_buffer);

	/* OBIS helper */
	RUN_TEST(test_obis_helper);

	TEST_SUITE_END("COSEM");
}
