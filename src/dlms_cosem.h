/*
 * DLMS/COSEM Application Layer
 *
 * Implements COSEM AARQ (Association Request), GET.request PDU encoding,
 * and response decoding for reading OBIS code values from a DLMS meter.
 *
 * Supports Lowest Level Security (LLS) authentication.
 */

#ifndef DLMS_COSEM_H_
#define DLMS_COSEM_H_

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

/* COSEM APDU tags */
#define COSEM_TAG_AARQ              0x60
#define COSEM_TAG_AARE              0x61
#define COSEM_TAG_GET_REQUEST       0xC0
#define COSEM_TAG_GET_RESPONSE      0xC4
#define COSEM_TAG_RLRQ              0x62  /* Release Request */
#define COSEM_TAG_RLRE              0x63  /* Release Response */

/* GET.request types */
#define GET_REQUEST_NORMAL          0x01
#define GET_REQUEST_NEXT            0x02
#define GET_REQUEST_WITH_LIST       0x03

/* GET.response types */
#define GET_RESPONSE_NORMAL         0x01
#define GET_RESPONSE_WITH_DATABLOCK 0x02
#define GET_RESPONSE_WITH_LIST      0x03

/* COSEM data types */
#define COSEM_TYPE_NULL_DATA        0x00
#define COSEM_TYPE_BOOLEAN          0x03
#define COSEM_TYPE_INT8             0x0F
#define COSEM_TYPE_UINT8            0x11
#define COSEM_TYPE_INT16            0x10
#define COSEM_TYPE_UINT16           0x12
#define COSEM_TYPE_INT32            0x05
#define COSEM_TYPE_UINT32           0x06
#define COSEM_TYPE_INT64            0x14
#define COSEM_TYPE_UINT64           0x15
#define COSEM_TYPE_FLOAT32          0x17
#define COSEM_TYPE_FLOAT64          0x18
#define COSEM_TYPE_OCTET_STRING     0x09
#define COSEM_TYPE_VISIBLE_STRING   0x0A
#define COSEM_TYPE_ENUM             0x16
#define COSEM_TYPE_STRUCTURE        0x02
#define COSEM_TYPE_ARRAY            0x01

/* OBIS code structure (6 bytes: A-B:C.D.E*F) */
struct obis_code {
	uint8_t a;
	uint8_t b;
	uint8_t c;
	uint8_t d;
	uint8_t e;
	uint8_t f;
};

/* COSEM attribute descriptor */
struct cosem_attr_desc {
	uint16_t         class_id;       /* Interface class (e.g. 3 = Register) */
	struct obis_code obis;           /* OBIS logical name */
	int8_t           attribute_id;   /* Attribute index (2 = value for Register) */
};

/* Parsed COSEM GET response */
struct cosem_get_result {
	bool   success;
	uint8_t data_type;       /* COSEM data type tag */
	union {
		int64_t   i64;
		uint64_t  u64;
		double    f64;
		float     f32;
		struct {
			uint8_t data[128];
			size_t  len;
		} raw;
	} value;
	/* For scaler-unit (Register class): scaler and unit */
	int8_t  scaler;          /* 10^scaler multiplier */
	uint8_t unit;            /* DLMS unit enum */
	bool    has_scaler_unit;
};

/**
 * @brief Build AARQ (Association Request) PDU
 *
 * Creates an AARQ for Logical Name referencing with optional
 * Lowest Level Security (LLS) password authentication.
 *
 * @param buf       Output buffer
 * @param buf_size  Size of output buffer
 * @param password  LLS password (NULL for no authentication)
 * @param pass_len  Password length
 * @return PDU length, or negative errno
 */
int cosem_build_aarq(uint8_t *buf, size_t buf_size,
		     const uint8_t *password, size_t pass_len);

/**
 * @brief Parse AARE (Association Response) PDU
 *
 * @param data  AARE PDU data
 * @param len   PDU length
 * @return 0 if association accepted, negative errno on failure/rejection
 */
int cosem_parse_aare(const uint8_t *data, size_t len);

/**
 * @brief Build GET.request-normal PDU
 *
 * @param buf       Output buffer
 * @param buf_size  Size of output buffer
 * @param invoke_id Invoke ID (caller tracks this)
 * @param attr      COSEM attribute descriptor (class + OBIS + attribute)
 * @return PDU length, or negative errno
 */
int cosem_build_get_request(uint8_t *buf, size_t buf_size,
			    uint8_t invoke_id,
			    const struct cosem_attr_desc *attr);

/**
 * @brief Parse GET.response PDU and extract value
 *
 * @param data    Response PDU data
 * @param len     PDU length
 * @param result  Output parsed result
 * @return 0 on success, negative errno on failure
 */
int cosem_parse_get_response(const uint8_t *data, size_t len,
			     struct cosem_get_result *result);

/**
 * @brief Build RLRQ (Release Request) PDU
 *
 * @param buf       Output buffer
 * @param buf_size  Size of output buffer
 * @return PDU length, or negative errno
 */
int cosem_build_rlrq(uint8_t *buf, size_t buf_size);

/**
 * @brief Decode a COSEM data value from raw bytes
 *
 * @param data   Raw data starting at the type tag
 * @param len    Available data length
 * @param result Output result with decoded value
 * @return Number of bytes consumed, or negative errno
 */
int cosem_decode_data(const uint8_t *data, size_t len,
		      struct cosem_get_result *result);

/**
 * @brief Convenience: create OBIS code from A-B:C.D.E*F notation
 */
static inline struct obis_code obis(uint8_t a, uint8_t b,
				    uint8_t c, uint8_t d,
				    uint8_t e, uint8_t f)
{
	return (struct obis_code){ a, b, c, d, e, f };
}

#endif /* DLMS_COSEM_H_ */
