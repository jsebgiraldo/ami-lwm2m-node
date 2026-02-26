/*
 * DLMS/COSEM Application Layer
 *
 * Implements COSEM AARQ, GET.request, and response decoding.
 * Targeting DLMS Logical Name (LN) referencing with LLS authentication.
 */

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <string.h>
#include <math.h>

#include "dlms_cosem.h"

LOG_MODULE_REGISTER(dlms_cosem, LOG_LEVEL_DBG);

/*
 * Application Context Name for LN referencing (no ciphering):
 *   joint-iso-itu-t(2) country(16) country-name(756 = CH)
 *   identified-organization(5) DLMS-UA(8) application-context(1)
 *   context-id-LN(1) = 2.16.756.5.8.1.1
 * BER encoding: 60 36 A1 09 06 07 60 85 74 05 08 01 01
 */
static const uint8_t app_context_ln[] = {
	0x60, 0x85, 0x74, 0x05, 0x08, 0x01, 0x01
};

int cosem_build_aarq(uint8_t *buf, size_t buf_size,
		     const uint8_t *password, size_t pass_len)
{
	if (!buf || buf_size < 64) {
		return -EINVAL;
	}

	uint8_t *p = buf;
	uint8_t *len_pos;

	/* AARQ tag */
	*p++ = COSEM_TAG_AARQ;
	len_pos = p++;  /* Length placeholder */

	/*
	 * Application Context Name [1]
	 *   A1 09
	 *     06 07 60 85 74 05 08 01 01
	 */
	*p++ = 0xA1;  /* Context tag [1] */
	*p++ = 0x09;
	*p++ = 0x06;  /* OID type */
	*p++ = 0x07;  /* OID length */
	memcpy(p, app_context_ln, sizeof(app_context_ln));
	p += sizeof(app_context_ln);

	/*
	 * Sender ACSE Requirements [8A] (if password authentication)
	 *   8A 02 07 80  — bit string, authentication functional unit
	 */
	if (password && pass_len > 0) {
		*p++ = 0x8A;  /* Context tag [10] implicit */
		*p++ = 0x02;
		*p++ = 0x07;  /* Unused bits = 7 → only bit 0 used */
		*p++ = 0x80;  /* Authentication functional unit */

		/*
		 * Mechanism Name [8B]
		 *   8B 07 60 85 74 05 08 02 01
		 *   = 2.16.756.5.8.2.1 (Low Level Security)
		 */
		*p++ = 0x8B;  /* Context tag [11] implicit */
		*p++ = 0x07;
		*p++ = 0x60;
		*p++ = 0x85;
		*p++ = 0x74;
		*p++ = 0x05;
		*p++ = 0x08;
		*p++ = 0x02;
		*p++ = 0x01;  /* LLS mechanism */

		/*
		 * Calling Authentication Value [AC]
		 *   AC <len>
		 *     80 <pass_len> <password>
		 */
		*p++ = 0xAC;  /* Context tag [12] constructed */
		*p++ = (uint8_t)(pass_len + 2);
		*p++ = 0x80;  /* charstring tag (context-specific) */
		*p++ = (uint8_t)pass_len;
		memcpy(p, password, pass_len);
		p += pass_len;
	}

	/*
	 * User Information [BE]
	 *   BE <len>
	 *     04 <len>  (OCTET STRING — xDLMS InitiateRequest)
	 *       01 00 00 00  — proposed DLMS version, etc.
	 *       06 5F 1F     — proposed conformance (bits)
	 *       04 00        — proposed QoS
	 *       00 07        — proposed-dlms-version-number = 6
	 *       00 80        — max-receive-pdu-size = 128
	 *
	 * InitiateRequest:
	 *   01     — xDLMS InitiateRequest tag
	 *   00     — dedicated-key absent
	 *   00     — response-allowed = TRUE (default)
	 *   00     — proposed-quality-of-service = 0
	 *   06     — proposed-dlms-version-number = 6
	 *   5F 1F 04 00 00 1E 1D 00 80
	 *          — proposed conformance block (3 bytes tag + 4 bytes)
	 *   00 80  — client-max-receive-pdu-size = 128
	 */
	static const uint8_t initiate_request[] = {
		0x01,                   /* xDLMS InitiateRequest */
		0x00,                   /* dedicated-key absent */
		0x00,                   /* response-allowed = TRUE */
		0x00,                   /* proposed-quality-of-service */
		0x06,                   /* proposed-dlms-version-number = 6 */
		0x5F, 0x1F,             /* Conformance tag */
		0x04,                   /* Conformance length = 4 */
		0x00,                   /* Unused bits */
		/* Conformance block (24 bits):
		 * bit 0: general-protection
		 * bit 3: read, bit 4: write
		 * bit 8: unconfirmed-write
		 * bit 9: attribute0-supported-with-get
		 * bit 12: get, bit 15: set
		 * bit 19: selective-access, bit 20: event-notification
		 * bit 23: action
		 * We request: get(12) + selective-access(19) + block-transfer-with-get(14)
		 * = 0x00 1C 03 = get + set + selective_access + block_transfer
		 */
		0x00, 0x18, 0x1D,
		0x00, 0x80,             /* client-max-receive-pdu-size = 128 */
	};

	*p++ = 0xBE;  /* Context tag [14] constructed */
	*p++ = (uint8_t)(sizeof(initiate_request) + 2);
	*p++ = 0x04;  /* OCTET STRING tag */
	*p++ = (uint8_t)sizeof(initiate_request);
	memcpy(p, initiate_request, sizeof(initiate_request));
	p += sizeof(initiate_request);

	/* Fill in overall AARQ length */
	*len_pos = (uint8_t)(p - len_pos - 1);

	size_t total = p - buf;
	LOG_DBG("AARQ built: %u bytes", (unsigned)total);
	return (int)total;
}

int cosem_parse_aare(const uint8_t *data, size_t len)
{
	if (!data || len < 3) {
		return -EINVAL;
	}

	if (data[0] != COSEM_TAG_AARE) {
		LOG_ERR("AARE: Wrong tag: 0x%02X (expected 0x61)", data[0]);
		return -EPROTO;
	}

	/*
	 * Scan for Association Result [A2]:
	 *   A2 03 02 01 <result>
	 * result: 0 = accepted, 1 = rejected-permanent, 2 = rejected-transient
	 */
	for (size_t i = 2; i < len - 4; i++) {
		if (data[i] == 0xA2 && data[i + 1] == 0x03 &&
		    data[i + 2] == 0x02 && data[i + 3] == 0x01) {
			uint8_t result = data[i + 4];
			if (result == 0) {
				LOG_INF("AARE: Association ACCEPTED");
				return 0;
			}
			LOG_ERR("AARE: Association REJECTED (result=%u)", result);
			return -EACCES;
		}
	}

	LOG_WRN("AARE: Could not find association-result");
	return -EPROTO;
}

int cosem_build_get_request(uint8_t *buf, size_t buf_size,
			    uint8_t invoke_id,
			    const struct cosem_attr_desc *attr)
{
	if (!buf || !attr || buf_size < 16) {
		return -EINVAL;
	}

	uint8_t *p = buf;

	/* GET.request tag */
	*p++ = COSEM_TAG_GET_REQUEST;
	/* GET.request-normal */
	*p++ = GET_REQUEST_NORMAL;
	/* Invoke ID and priority */
	*p++ = invoke_id;

	/* COSEM attribute descriptor */
	/* Class ID (2 bytes) */
	*p++ = (attr->class_id >> 8) & 0xFF;
	*p++ = attr->class_id & 0xFF;

	/* OBIS code (6 bytes) */
	*p++ = attr->obis.a;
	*p++ = attr->obis.b;
	*p++ = attr->obis.c;
	*p++ = attr->obis.d;
	*p++ = attr->obis.e;
	*p++ = attr->obis.f;

	/* Attribute ID */
	*p++ = attr->attribute_id;

	/* Access selection = 0 (no selective access) */
	*p++ = 0x00;

	size_t total = p - buf;
	LOG_DBG("GET.request built: %u bytes, class=%u, OBIS=%u-%u:%u.%u.%u*%u, attr=%d",
		(unsigned)total, attr->class_id,
		attr->obis.a, attr->obis.b, attr->obis.c,
		attr->obis.d, attr->obis.e, attr->obis.f,
		attr->attribute_id);

	return (int)total;
}

int cosem_decode_data(const uint8_t *data, size_t len,
		      struct cosem_get_result *result)
{
	if (!data || !result || len < 1) {
		return -EINVAL;
	}

	result->data_type = data[0];
	size_t consumed = 1;

	switch (data[0]) {
	case COSEM_TYPE_NULL_DATA:
		result->value.u64 = 0;
		break;

	case COSEM_TYPE_BOOLEAN:
		if (len < 2) return -ENODATA;
		result->value.u64 = data[1];
		consumed = 2;
		break;

	case COSEM_TYPE_UINT8:
	case COSEM_TYPE_ENUM:
		if (len < 2) return -ENODATA;
		result->value.u64 = data[1];
		consumed = 2;
		break;

	case COSEM_TYPE_INT8:
		if (len < 2) return -ENODATA;
		result->value.i64 = (int8_t)data[1];
		consumed = 2;
		break;

	case COSEM_TYPE_UINT16:
		if (len < 3) return -ENODATA;
		result->value.u64 = (data[1] << 8) | data[2];
		consumed = 3;
		break;

	case COSEM_TYPE_INT16:
		if (len < 3) return -ENODATA;
		result->value.i64 = (int16_t)((data[1] << 8) | data[2]);
		consumed = 3;
		break;

	case COSEM_TYPE_UINT32:
		if (len < 5) return -ENODATA;
		result->value.u64 = ((uint32_t)data[1] << 24) |
				    ((uint32_t)data[2] << 16) |
				    ((uint32_t)data[3] << 8) |
				    data[4];
		consumed = 5;
		break;

	case COSEM_TYPE_INT32:
		if (len < 5) return -ENODATA;
		result->value.i64 = (int32_t)(((uint32_t)data[1] << 24) |
					      ((uint32_t)data[2] << 16) |
					      ((uint32_t)data[3] << 8) |
					      data[4]);
		consumed = 5;
		break;

	case COSEM_TYPE_UINT64:
		if (len < 9) return -ENODATA;
		result->value.u64 = 0;
		for (int i = 0; i < 8; i++) {
			result->value.u64 = (result->value.u64 << 8) | data[1 + i];
		}
		consumed = 9;
		break;

	case COSEM_TYPE_INT64:
		if (len < 9) return -ENODATA;
		result->value.i64 = 0;
		for (int i = 0; i < 8; i++) {
			result->value.i64 = (result->value.i64 << 8) | data[1 + i];
		}
		consumed = 9;
		break;

	case COSEM_TYPE_FLOAT32: {
		if (len < 5) return -ENODATA;
		uint32_t fbits = ((uint32_t)data[1] << 24) |
				 ((uint32_t)data[2] << 16) |
				 ((uint32_t)data[3] << 8) |
				 data[4];
		float f;
		memcpy(&f, &fbits, sizeof(f));
		result->value.f64 = (double)f;
		result->data_type = COSEM_TYPE_FLOAT32;
		consumed = 5;
		break;
	}

	case COSEM_TYPE_FLOAT64: {
		if (len < 9) return -ENODATA;
		uint64_t dbits = 0;
		for (int i = 0; i < 8; i++) {
			dbits = (dbits << 8) | data[1 + i];
		}
		memcpy(&result->value.f64, &dbits, sizeof(double));
		consumed = 9;
		break;
	}

	case COSEM_TYPE_OCTET_STRING:
	case COSEM_TYPE_VISIBLE_STRING:
		if (len < 2) return -ENODATA;
		{
			uint8_t slen = data[1];
			if (len < (size_t)(2 + slen)) return -ENODATA;
			size_t copy_len = slen;
			if (copy_len > sizeof(result->value.raw.data)) {
				copy_len = sizeof(result->value.raw.data);
			}
			memcpy(result->value.raw.data, &data[2], copy_len);
			result->value.raw.len = copy_len;
			consumed = 2 + slen;
		}
		break;

	case COSEM_TYPE_STRUCTURE: {
		/* Structure: just record element count, caller handles elements */
		if (len < 2) return -ENODATA;
		result->value.u64 = data[1];  /* Element count */
		consumed = 2;
		break;
	}

	case COSEM_TYPE_ARRAY: {
		if (len < 2) return -ENODATA;
		result->value.u64 = data[1];  /* Element count */
		consumed = 2;
		break;
	}

	default:
		LOG_WRN("Unknown COSEM data type: 0x%02X", data[0]);
		return -ENOTSUP;
	}

	return (int)consumed;
}

int cosem_parse_get_response(const uint8_t *data, size_t len,
			     struct cosem_get_result *result)
{
	if (!data || !result || len < 4) {
		return -EINVAL;
	}

	memset(result, 0, sizeof(*result));

	/* Verify GET.response tag */
	if (data[0] != COSEM_TAG_GET_RESPONSE) {
		LOG_ERR("GET.response: Wrong tag: 0x%02X", data[0]);
		return -EPROTO;
	}

	uint8_t response_type = data[1];
	/* uint8_t invoke_id = data[2]; */

	if (response_type == GET_RESPONSE_NORMAL) {
		/*
		 * GET.response-normal:
		 *   C4 01 <invoke_id> <data-or-error>
		 *
		 * data: 00 <type> <value>  (Data choice)
		 * error: 01 <error-code>   (Data-Access-Result)
		 */
		if (len < 4) return -ENODATA;

		uint8_t choice = data[3];
		if (choice == 0x00) {
			/* Data present */
			int ret = cosem_decode_data(&data[4], len - 4, result);
			if (ret < 0) {
				return ret;
			}
			result->success = true;
			return 0;
		} else if (choice == 0x01) {
			/* Data-Access-Result error */
			uint8_t error = (len > 4) ? data[4] : 0xFF;
			LOG_ERR("GET.response: Data access error: %u", error);
			result->success = false;
			return -EACCES;
		}
	} else if (response_type == GET_RESPONSE_WITH_DATABLOCK) {
		LOG_WRN("GET.response with datablock — not yet supported");
		return -ENOTSUP;
	}

	return -EPROTO;
}

int cosem_build_rlrq(uint8_t *buf, size_t buf_size)
{
	if (!buf || buf_size < 3) {
		return -EINVAL;
	}

	/* RLRQ: 62 00 (Release Request, length 0 = normal release) */
	buf[0] = COSEM_TAG_RLRQ;
	buf[1] = 0x00;

	return 2;
}
