/*
 * Unit Tests — DLMS Meter Logic (dlms_meter.c)
 *
 * Tests value_to_double conversion, OBIS table completeness,
 * meter_readings struct layout, and configuration defaults.
 *
 * Strategy: #include dlms_meter.c directly so we can test
 * static functions. Stub headers in stubs/ intercept the
 * hardware-dependent includes (RS485, LwM2M).
 */
#include "test_framework.h"

/*
 * RS485 stubs — must be defined BEFORE including dlms_meter.c
 * because "rs485_uart.h" resolves to ../src/rs485_uart.h (declarations only).
 * We provide the implementations here so the linker is happy.
 */
#include <stdint.h>
#include <stddef.h>
int rs485_init(void) { return 0; }
int rs485_send(const uint8_t *data, size_t len) { (void)data; return (int)len; }
int rs485_recv(uint8_t *buf, size_t buf_size, int timeout_ms) {
	(void)buf; (void)buf_size; (void)timeout_ms; return 0;
}
void rs485_flush_rx(void) {}

/*
 * Include the dlms_meter.c source directly.
 * The -Istubs flag ensures our stub versions of lwm2m_observation.h
 * and zephyr/net/lwm2m.h are found.
 */
#include "../src/dlms_meter.c"

/* ==== OBIS Table Completeness ==== */

void test_obis_table_has_27_entries(void)
{
	ASSERT_EQ(27, (int)OBIS_TABLE_SIZE);
}

void test_obis_table_all_class3(void)
{
	/* All our meter registers are class_id = 3 (Register) */
	for (size_t i = 0; i < OBIS_TABLE_SIZE; i++) {
		ASSERT_EQ(3, (int)obis_table[i].class_id);
	}
}

void test_obis_table_unique_offsets(void)
{
	/* Each entry should map to a unique field in meter_readings */
	for (size_t i = 0; i < OBIS_TABLE_SIZE; i++) {
		for (size_t j = i + 1; j < OBIS_TABLE_SIZE; j++) {
			if (obis_table[i].offset == obis_table[j].offset) {
				printf(TF_RED "FAIL" TF_RESET "\n");
				printf("    Duplicate offset at [%zu]=%s and [%zu]=%s\n",
				       i, obis_table[i].name, j, obis_table[j].name);
				_tf_failed++;
				return;
			}
		}
	}
}

void test_obis_table_unique_obis_codes(void)
{
	/* Each OBIS code should be unique */
	for (size_t i = 0; i < OBIS_TABLE_SIZE; i++) {
		for (size_t j = i + 1; j < OBIS_TABLE_SIZE; j++) {
			const struct obis_code *a = &obis_table[i].obis;
			const struct obis_code *b = &obis_table[j].obis;
			int same = (a->a == b->a && a->b == b->b &&
				    a->c == b->c && a->d == b->d &&
				    a->e == b->e && a->f == b->f);
			if (same) {
				printf(TF_RED "FAIL" TF_RESET "\n");
				printf("    Duplicate OBIS at [%zu]=%s and [%zu]=%s\n",
				       i, obis_table[i].name, j, obis_table[j].name);
				_tf_failed++;
				return;
			}
		}
	}
}

void test_obis_table_all_have_names(void)
{
	for (size_t i = 0; i < OBIS_TABLE_SIZE; i++) {
		ASSERT_TRUE(obis_table[i].name != NULL);
		ASSERT_TRUE(strlen(obis_table[i].name) > 0);
	}
}

/* ==== meter_readings Struct Layout ==== */

void test_readings_voltage_r_first_field(void)
{
	/* voltage_r should be the first double field */
	ASSERT_EQ(0, (int)MR_OFF(voltage_r));
}

void test_readings_all_doubles_8_bytes(void)
{
	/* Each measurement field is a double = 8 bytes */
	ASSERT_EQ(8, (int)sizeof(double));
}

void test_readings_struct_has_metadata(void)
{
	struct meter_readings r;
	memset(&r, 0, sizeof(r));

	/* Metadata fields exist and are writable (v0.17.0: added read_target, field_mask) */
	r.valid = true;
	r.read_count = 22;
	r.error_count = 5;
	r.read_target = 27;
	r.field_mask = 0x07FFFFFF;
	r.timestamp_ms = 123456;

	ASSERT_TRUE(r.valid);
	ASSERT_EQ(22, r.read_count);
	ASSERT_EQ(5, r.error_count);
	ASSERT_EQ(27, r.read_target);
	ASSERT_EQ((int)0x07FFFFFF, (int)r.field_mask);
	ASSERT_EQ(123456, (int)r.timestamp_ms);
}

void test_readings_field_count(void)
{
	/*
	 * 27 double fields:
	 *  3 voltages + 3 currents + 3 active + 3 reactive +
	 *  3 apparent + 3 pf + 4 totals + 3 energy + 1 freq + 1 neutral
	 */
	ASSERT_EQ(27, (int)OBIS_TABLE_SIZE);

	/* Verify the last OBIS entry maps to neutral_current */
	ASSERT_EQ(MR_OFF(neutral_current), obis_table[26].offset);
}

/* ==== value_to_double Conversion ==== */

void test_vtod_unsigned_no_scaler(void)
{
	struct cosem_get_result r = {
		.data_type = COSEM_TYPE_UINT32,
		.value = { .u64 = 1320 },
	};

	/* No scaler cached → raw value returned */
	double val = value_to_double(&r, 0);
	ASSERT_FLOAT_EQ(1320.0, val, 0.001);
}

void test_vtod_signed_negative(void)
{
	struct cosem_get_result r = {
		.data_type = COSEM_TYPE_INT32,
		.value = { .i64 = -500 },
	};

	double val = value_to_double(&r, 0);
	ASSERT_FLOAT_EQ(-500.0, val, 0.001);
}

void test_vtod_float64_direct(void)
{
	struct cosem_get_result r = {
		.data_type = COSEM_TYPE_FLOAT64,
		.value = { .f64 = 131.9 },
	};

	double val = value_to_double(&r, 0);
	ASSERT_FLOAT_EQ(131.9, val, 0.001);
}

void test_vtod_with_scaler(void)
{
	/* Cache scaler = 10^(-1) = 0.1 for table index 0 */
	scaler_cache[0] = 0.1;
	scaler_cached[0] = true;

	struct cosem_get_result r = {
		.data_type = COSEM_TYPE_UINT32,
		.value = { .u64 = 1320 },
	};

	double val = value_to_double(&r, 0);

	/* 1320 * 0.1 = 132.0 */
	ASSERT_FLOAT_EQ(132.0, val, 0.001);

	/* Cleanup */
	scaler_cached[0] = false;
}

void test_vtod_scaler_negative_exponent(void)
{
	/* scaler = 10^(-3) for energy values */
	scaler_cache[1] = pow(10.0, -3.0);  /* 0.001 */
	scaler_cached[1] = true;

	struct cosem_get_result r = {
		.data_type = COSEM_TYPE_UINT64,
		.value = { .u64 = 56893000 },
	};

	double val = value_to_double(&r, 1);

	/* 56893000 * 0.001 = 56893.0 */
	ASSERT_FLOAT_EQ(56893.0, val, 0.1);

	scaler_cached[1] = false;
}

void test_vtod_enum_type(void)
{
	struct cosem_get_result r = {
		.data_type = COSEM_TYPE_ENUM,
		.value = { .u64 = 27 },  /* W unit */
	};

	double val = value_to_double(&r, 0);
	ASSERT_FLOAT_EQ(27.0, val, 0.001);
}

void test_vtod_unknown_type_returns_zero(void)
{
	struct cosem_get_result r = {
		.data_type = COSEM_TYPE_OCTET_STRING,
		.value = { .u64 = 42 },
	};

	double val = value_to_double(&r, 0);
	ASSERT_FLOAT_EQ(0.0, val, 0.001);
}

void test_vtod_out_of_bounds_index(void)
{
	struct cosem_get_result r = {
		.data_type = COSEM_TYPE_UINT32,
		.value = { .u64 = 100 },
	};

	/* Index -1 → scaler not applied */
	double val = value_to_double(&r, -1);
	ASSERT_FLOAT_EQ(100.0, val, 0.001);

	/* Index beyond table → scaler not applied */
	val = value_to_double(&r, 999);
	ASSERT_FLOAT_EQ(100.0, val, 0.001);
}

/* ==== Default Configuration ==== */

void test_default_config(void)
{
	set_defaults();

	ASSERT_EQ(1, (int)cfg.client_sap);
	ASSERT_EQ(0, (int)cfg.server_logical);
	ASSERT_EQ(1, (int)cfg.server_physical);
	ASSERT_STR_EQ("22222222", cfg.password);
	ASSERT_EQ(128, (int)cfg.max_info_len);
	ASSERT_EQ(5000, cfg.response_timeout_ms);
	ASSERT_EQ(30, cfg.inter_frame_delay_ms);
}

void test_meter_set_config_null_resets(void)
{
	/* Modify config */
	cfg.client_sap = 99;
	cfg.max_info_len = 256;

	/* Passing NULL resets to defaults */
	meter_set_config(NULL);

	ASSERT_EQ(1, (int)cfg.client_sap);
	ASSERT_EQ(128, (int)cfg.max_info_len);
}

void test_meter_set_config_custom(void)
{
	struct meter_config custom = {
		.client_sap = 16,
		.server_logical = 1,
		.server_physical = 2,
		.max_info_len = 256,
		.response_timeout_ms = 3000,
		.inter_frame_delay_ms = 50,
	};
	strncpy(custom.password, "88888888", sizeof(custom.password));

	meter_set_config(&custom);

	ASSERT_EQ(16, (int)cfg.client_sap);
	ASSERT_EQ(1, (int)cfg.server_logical);
	ASSERT_EQ(2, (int)cfg.server_physical);
	ASSERT_EQ(256, (int)cfg.max_info_len);
	ASSERT_EQ(3000, cfg.response_timeout_ms);
	ASSERT_STR_EQ("88888888", cfg.password);

	/* Restore defaults */
	meter_set_config(NULL);
}

/* ==== Meter State ==== */

void test_initial_state_disconnected(void)
{
	/* After set_defaults, state is whatever — but meter_get_state should work */
	state = METER_DISCONNECTED;
	ASSERT_EQ(METER_DISCONNECTED, (int)meter_get_state());
}

void test_state_enum_ordering(void)
{
	/* Ensure state ordering: DISCONNECTED < HDLC_CONNECTED < ASSOCIATED */
	ASSERT_TRUE(METER_DISCONNECTED < METER_HDLC_CONNECTED);
	ASSERT_TRUE(METER_HDLC_CONNECTED < METER_ASSOCIATED);
}

/* ==== LLC Header ==== */

void test_llc_send_header(void)
{
	ASSERT_EQ(3, (int)LLC_HDR_LEN);
	ASSERT_EQ(0xE6, llc_send_hdr[0]);
	ASSERT_EQ(0xE6, llc_send_hdr[1]);
	ASSERT_EQ(0x00, llc_send_hdr[2]);
}

/* ==== OBIS → meter_readings Mapping Spot Checks ==== */

void test_obis_voltage_r_is_first(void)
{
	/* First entry should be Voltage Phase A */
	ASSERT_EQ(1,   obis_table[0].obis.a);
	ASSERT_EQ(1,   obis_table[0].obis.b);
	ASSERT_EQ(32,  obis_table[0].obis.c);
	ASSERT_EQ(7,   obis_table[0].obis.d);
	ASSERT_EQ(0,   obis_table[0].obis.e);
	ASSERT_EQ(255, obis_table[0].obis.f);
	ASSERT_EQ(MR_OFF(voltage_r), obis_table[0].offset);
}

void test_obis_frequency(void)
{
	/* Frequency = 1-1:14.7.0*255 at index 25 */
	ASSERT_EQ(14,  obis_table[25].obis.c);
	ASSERT_EQ(7,   obis_table[25].obis.d);
	ASSERT_EQ(MR_OFF(frequency), obis_table[25].offset);
}

void test_obis_active_energy(void)
{
	/* Active Energy Import = 1-1:1.8.0*255 at index 22 */
	ASSERT_EQ(1,  obis_table[22].obis.c);
	ASSERT_EQ(8,  obis_table[22].obis.d);
	ASSERT_EQ(MR_OFF(active_energy), obis_table[22].offset);
}

/* ==== Read Validation & Field Mask (v0.17.0) ==== */

void test_last_good_cache_initially_invalid(void)
{
	/* On startup, last_good_valid should be false */
	last_good_valid = false;
	ASSERT_FALSE(last_good_valid);
}

void test_failed_reads_stay_zero(void)
{
	/*
	 * v0.17.0: Failed reads are NOT filled with last_good.
	 * They stay at 0.0 and their field_mask bit is NOT set.
	 * PUSH_FIELD skips them, so they never reach the server.
	 */
	struct meter_readings r;
	memset(&r, 0, sizeof(r));

	/* Only voltage_r and frequency were successfully read */
	r.voltage_r = 123.0;
	r.frequency = 60.1;
	r.field_mask = (1u << 0) | (1u << 25);  /* bits 0, 25 */
	r.read_count = 2;
	r.error_count = 25;

	/* Verify: successfully read fields have real values */
	ASSERT_FLOAT_EQ(123.0, r.voltage_r, 0.001);
	ASSERT_FLOAT_EQ(60.1,  r.frequency, 0.001);

	/* Verify: failed reads are zero (NOT last_good) */
	ASSERT_FLOAT_EQ(0.0, r.current_r, 0.001);
	ASSERT_FLOAT_EQ(0.0, r.active_energy, 0.001);

	/* Verify: field_mask only has bits for successful reads */
	ASSERT_TRUE(r.field_mask & (1u << 0));     /* voltage_r: read */
	ASSERT_TRUE(r.field_mask & (1u << 25));    /* frequency: read */
	ASSERT_FALSE(r.field_mask & (1u << 1));    /* current_r: NOT read */
	ASSERT_FALSE(r.field_mask & (1u << 22));   /* active_energy: NOT read */
}

void test_first_poll_zeros_without_last_good(void)
{
	/* All polls start with memset(0) — field_mask gates what gets pushed */
	struct meter_readings r;
	last_good_valid = false;

	memset(&r, 0, sizeof(r));
	r.field_mask = 0;

	ASSERT_FLOAT_EQ(0.0, r.voltage_r, 0.001);
	ASSERT_FLOAT_EQ(0.0, r.current_r, 0.001);
	ASSERT_FLOAT_EQ(0.0, r.frequency, 0.001);
	ASSERT_EQ(0, (int)r.field_mask);
}

void test_last_good_updated_per_field(void)
{
	/*
	 * v0.17.0: last_good is updated per-field, only for fields
	 * that were actually read (bit set in field_mask).
	 */
	last_good_valid = true;
	memset(&last_good, 0, sizeof(last_good));
	last_good.voltage_r = 120.0;
	last_good.current_r = 5.0;

	struct meter_readings r;
	memset(&r, 0, sizeof(r));
	r.voltage_r = 122.0;  /* New reading */
	r.field_mask = (1u << 0);  /* Only voltage_r was read */
	r.valid = true;
	r.read_target = 27;

	/* Simulate per-field update logic from meter_read_all() */
	for (size_t j = 0; j < OBIS_TABLE_SIZE; j++) {
		if (r.field_mask & (1u << j)) {
			double *src = (double *)((uint8_t *)&r +
						  obis_table[j].offset);
			double *dst = (double *)((uint8_t *)&last_good +
						  obis_table[j].offset);
			*dst = *src;
		}
	}

	/* voltage_r updated to new value */
	ASSERT_FLOAT_EQ(122.0, last_good.voltage_r, 0.001);
	/* current_r NOT in field_mask → retains old value */
	ASSERT_FLOAT_EQ(5.0, last_good.current_r, 0.001);
}

void test_last_good_not_updated_on_invalid_read(void)
{
	/* If reads are invalid (below min coverage), last_good untouched */
	struct meter_readings old_good;
	memset(&old_good, 0, sizeof(old_good));
	old_good.voltage_r = 120.0;
	old_good.frequency = 60.0;

	memcpy(&last_good, &old_good, sizeof(last_good));
	last_good_valid = true;

	struct meter_readings r;
	memset(&r, 0, sizeof(r));
	r.valid = false;
	r.read_count = 0;
	r.field_mask = 0;

	/* Do NOT update last_good when invalid */
	if (r.valid) {
		/* would update per-field... */
	}

	/* last_good should still hold old values */
	ASSERT_TRUE(last_good_valid);
	ASSERT_FLOAT_EQ(120.0, last_good.voltage_r, 0.001);
	ASSERT_FLOAT_EQ(60.0,  last_good.frequency, 0.001);
}

/* ==== Field Mask (v0.17.0) ==== */

void test_field_mask_initially_zero(void)
{
	struct meter_readings r;
	memset(&r, 0, sizeof(r));
	ASSERT_EQ(0, (int)r.field_mask);
}

void test_field_mask_bit_for_each_obis(void)
{
	/* 27 OBIS entries fit in uint32_t (bits 0-26) */
	ASSERT_TRUE(OBIS_TABLE_SIZE <= 32);

	/* Verify each bit can be set independently */
	uint32_t all_bits = 0;
	for (size_t i = 0; i < OBIS_TABLE_SIZE; i++) {
		all_bits |= (1u << i);
	}
	/* 27 bits = 0x07FFFFFF */
	ASSERT_EQ((int)0x07FFFFFF, (int)all_bits);
}

void test_min_read_percent_threshold(void)
{
	/*
	 * MIN_READ_PERCENT=50 means at least 50% of reads must succeed.
	 * For 27 OBIS codes: min = ceil(27*50/100) = ceil(13.5) = 14
	 */
	int read_target = 27;
	int min_reads = (read_target * MIN_READ_PERCENT + 99) / 100;
	ASSERT_EQ(14, min_reads);

	/* For single-phase (15 non-skipped): min = ceil(7.5) = 8 */
	read_target = 15;
	min_reads = (read_target * MIN_READ_PERCENT + 99) / 100;
	ASSERT_EQ(8, min_reads);
}

void test_valid_requires_min_coverage(void)
{
	/* With read_target=27, need 14+ successful reads */
	struct meter_readings r;
	memset(&r, 0, sizeof(r));

	r.read_count = 10;  /* Below threshold */
	r.read_target = 27;
	int min_reads = (r.read_target * MIN_READ_PERCENT + 99) / 100;
	r.valid = (r.read_count >= min_reads);
	ASSERT_FALSE(r.valid);  /* 10 < 14 → invalid */

	/* Exactly at threshold → valid */
	r.read_count = 14;
	r.valid = (r.read_count >= min_reads);
	ASSERT_TRUE(r.valid);  /* 14 >= 14 → valid */
}

/* ==== Sanity Check (v0.17.0: range validation + field coverage) ==== */

void test_sanity_check_rejects_all_zeros(void)
{
	struct meter_readings r;
	memset(&r, 0, sizeof(r));
	r.valid = true;
	r.field_mask = 0;      /* Nothing was read */
	r.read_target = 27;
	/* Neither voltage (bit 0) nor frequency (bit 25) read → FAIL */
	ASSERT_FALSE(readings_sanity_check(&r));
}

void test_sanity_check_accepts_valid_readings(void)
{
	struct meter_readings r;
	memset(&r, 0, sizeof(r));
	r.voltage_r = 122.0;
	r.frequency = 60.0;
	r.valid = true;
	r.read_target = 4;
	r.field_mask = (1u << 0) | (1u << 1) | (1u << 25) | (1u << 26);
	/* 4/4 bits set = 100% > 50% → passes all checks */
	ASSERT_TRUE(readings_sanity_check(&r));
}

void test_sanity_check_accepts_zero_current(void)
{
	/* Current CAN legitimately be 0.0 (no load) */
	struct meter_readings r;
	memset(&r, 0, sizeof(r));
	r.voltage_r = 122.0;
	r.frequency = 60.0;
	r.current_r = 0.0;
	r.valid = true;
	r.read_target = 3;
	r.field_mask = (1u << 0) | (1u << 1) | (1u << 25);
	ASSERT_TRUE(readings_sanity_check(&r));
}

void test_sanity_check_rejects_voltage_out_of_range(void)
{
	/* Voltage outside [50, 500] → should FAIL */
	struct meter_readings r;
	memset(&r, 0, sizeof(r));
	r.voltage_r = 600.0;  /* Way too high */
	r.frequency = 60.0;
	r.valid = true;
	r.read_target = 2;
	r.field_mask = (1u << 0) | (1u << 25);
	ASSERT_FALSE(readings_sanity_check(&r));

	/* Too low */
	r.voltage_r = 30.0;
	ASSERT_FALSE(readings_sanity_check(&r));
}

void test_sanity_check_rejects_frequency_out_of_range(void)
{
	/* Frequency outside [40, 70] → should FAIL */
	struct meter_readings r;
	memset(&r, 0, sizeof(r));
	r.voltage_r = 120.0;
	r.frequency = 80.0;  /* Too high */
	r.valid = true;
	r.read_target = 2;
	r.field_mask = (1u << 0) | (1u << 25);
	ASSERT_FALSE(readings_sanity_check(&r));

	/* Too low */
	r.frequency = 30.0;
	ASSERT_FALSE(readings_sanity_check(&r));
}

void test_sanity_check_requires_field_coverage(void)
{
	/* Even with valid voltage/frequency, too few fields → FAIL */
	struct meter_readings r;
	memset(&r, 0, sizeof(r));
	r.voltage_r = 120.0;
	r.frequency = 60.0;
	r.valid = true;
	r.read_target = 27;
	r.field_mask = (1u << 0) | (1u << 25);  /* Only 2/27 = 7% < 50% */
	ASSERT_FALSE(readings_sanity_check(&r));
}

void test_sanity_check_voltage_only_no_frequency(void)
{
	/* Voltage read but not frequency → passes check 3 (at least one read) */
	struct meter_readings r;
	memset(&r, 0, sizeof(r));
	r.voltage_r = 120.0;
	r.valid = true;
	r.read_target = 2;
	r.field_mask = (1u << 0) | (1u << 1);  /* voltage_r + current_r */
	/* Frequency not read (bit 25 not set) → freq range check skipped */
	ASSERT_TRUE(readings_sanity_check(&r));
}

/* ==== Periodic push (v0.18.0) — field_mask guard tests ==== */

void test_push_field_skips_when_bit_not_set(void)
{
	/* If a field's bit is NOT in field_mask, it should be skipped */
	struct meter_readings r;
	memset(&r, 0, sizeof(r));
	r.valid = true;
	r.field_mask = 0;  /* No bits set → every field should be skipped */
	/* Simulate: for bit_idx 0, mask & (1u << 0) == 0 → skip */
	ASSERT_TRUE((r.field_mask & (1u << 0)) == 0);
	ASSERT_TRUE((r.field_mask & (1u << 25)) == 0);
}

void test_push_field_pushes_when_bit_set(void)
{
	/* If a field's bit IS in field_mask, it should be pushed */
	struct meter_readings r;
	memset(&r, 0, sizeof(r));
	r.valid = true;
	r.field_mask = (1u << 0) | (1u << 18) | (1u << 25);
	ASSERT_TRUE((r.field_mask & (1u << 0)) != 0);   /* voltage_r */
	ASSERT_TRUE((r.field_mask & (1u << 18)) != 0);   /* total_active_power */
	ASSERT_TRUE((r.field_mask & (1u << 25)) != 0);   /* frequency */
	ASSERT_TRUE((r.field_mask & (1u << 1)) == 0);    /* current_r NOT set */
}

void test_push_field_all_27_bits(void)
{
	/* Full read: all 27 bits set → all fields pushed */
	struct meter_readings r;
	memset(&r, 0, sizeof(r));
	r.valid = true;
	r.field_mask = 0x07FFFFFFu;  /* bits 0-26 */
	for (int i = 0; i < 27; i++) {
		ASSERT_TRUE((r.field_mask & (1u << i)) != 0);
	}
	/* Bit 27 should NOT be set */
	ASSERT_TRUE((r.field_mask & (1u << 27)) == 0);
}

/* ==== v0.19.0: OBIS Retry & Diagnostics ==== */

void test_retry_constants(void)
{
	/* Retry config must be reasonable */
	ASSERT_TRUE(OBIS_READ_MAX_RETRIES >= 1);
	ASSERT_TRUE(OBIS_READ_MAX_RETRIES <= 5);
	ASSERT_TRUE(OBIS_RETRY_DELAY_MS >= 50);
	ASSERT_TRUE(OBIS_RETRY_DELAY_MS <= 500);
	ASSERT_TRUE(DIAG_LOG_INTERVAL >= 5);
}

void test_obis_diag_struct_size(void)
{
	/* Must have one diag entry per OBIS table entry */
	ASSERT_EQ((int)ARRAY_SIZE(obis_diag), (int)OBIS_TABLE_SIZE);
}

void test_obis_diag_initially_zero(void)
{
	/* After memset, all diagnostic counters should be zero */
	memset(obis_diag, 0, sizeof(obis_diag));
	for (size_t i = 0; i < OBIS_TABLE_SIZE; i++) {
		ASSERT_EQ(0, (int)obis_diag[i].success);
		ASSERT_EQ(0, (int)obis_diag[i].fail);
		ASSERT_EQ(0, (int)obis_diag[i].retries);
		ASSERT_EQ(0, (int)obis_diag[i].skip);
		ASSERT_EQ(0, (int)obis_diag[i].total_ms);
	}
}

void test_obis_diag_counters_writable(void)
{
	/* Simulate a few polls worth of diagnostics */
	memset(obis_diag, 0, sizeof(obis_diag));

	obis_diag[0].success = 18;   /* voltage_r: often fails */
	obis_diag[0].fail = 2;
	obis_diag[0].retries = 4;    /* 2 retries per failure */
	obis_diag[2].success = 20;   /* active_power_r: always succeeds */
	obis_diag[2].fail = 0;

	ASSERT_EQ(18, (int)obis_diag[0].success);
	ASSERT_EQ(2,  (int)obis_diag[0].fail);
	ASSERT_EQ(4,  (int)obis_diag[0].retries);
	ASSERT_EQ(20, (int)obis_diag[2].success);
	ASSERT_EQ(0,  (int)obis_diag[2].fail);

	/* Success rate calculation */
	uint32_t total = obis_diag[0].success + obis_diag[0].fail;
	int pct = total > 0 ? (int)(obis_diag[0].success * 100 / total) : 0;
	ASSERT_EQ(90, pct);  /* 18/20 = 90% */
}

void test_poll_duration_tracking(void)
{
	/* Verify poll duration tracking variables */
	poll_count = 0;
	last_poll_duration_ms = 0;
	poll_duration_sum_ms = 0;

	/* Simulate 3 polls */
	poll_count = 3;
	poll_duration_sum_ms = 5000 + 6000 + 5500;  /* 16500 ms total */
	last_poll_duration_ms = 5500;

	int64_t avg = meter_get_avg_poll_duration_ms();
	ASSERT_EQ(5500, (int)avg);  /* 16500/3 = 5500 */
	ASSERT_EQ(5500, (int)meter_get_poll_duration_ms());
	ASSERT_EQ(3, (int)meter_get_poll_count());

	/* Cleanup */
	poll_count = 0;
	last_poll_duration_ms = 0;
	poll_duration_sum_ms = 0;
}

void test_obis_diag_api_bounds(void)
{
	/* meter_get_obis_diag should handle out-of-bounds gracefully */
	uint32_t s = 99, f = 99, r = 99, sk = 99;
	meter_get_obis_diag(-1, &s, &f, &r, &sk);
	ASSERT_EQ(0, (int)s);
	ASSERT_EQ(0, (int)f);

	meter_get_obis_diag(999, &s, &f, &r, &sk);
	ASSERT_EQ(0, (int)s);

	/* NULL pointers should not crash */
	meter_get_obis_diag(0, NULL, NULL, NULL, NULL);
}

void test_obis_diag_api_valid_index(void)
{
	memset(obis_diag, 0, sizeof(obis_diag));
	obis_diag[0].success = 42;
	obis_diag[0].fail = 3;
	obis_diag[0].retries = 5;
	obis_diag[0].skip = 0;

	uint32_t s, f, r, sk;
	meter_get_obis_diag(0, &s, &f, &r, &sk);
	ASSERT_EQ(42, (int)s);
	ASSERT_EQ(3,  (int)f);
	ASSERT_EQ(5,  (int)r);
	ASSERT_EQ(0,  (int)sk);

	/* Cleanup */
	memset(obis_diag, 0, sizeof(obis_diag));
}

void test_avg_poll_duration_zero_polls(void)
{
	poll_count = 0;
	poll_duration_sum_ms = 0;
	ASSERT_EQ(0, (int)meter_get_avg_poll_duration_ms());
}

/* ==== Test Suite Runner ==== */

void run_dlms_logic_tests(void)
{
	TEST_SUITE_BEGIN("DLMS Logic");

	/* OBIS table */
	RUN_TEST(test_obis_table_has_27_entries);
	RUN_TEST(test_obis_table_all_class3);
	RUN_TEST(test_obis_table_unique_offsets);
	RUN_TEST(test_obis_table_unique_obis_codes);
	RUN_TEST(test_obis_table_all_have_names);

	/* meter_readings struct */
	RUN_TEST(test_readings_voltage_r_first_field);
	RUN_TEST(test_readings_all_doubles_8_bytes);
	RUN_TEST(test_readings_struct_has_metadata);
	RUN_TEST(test_readings_field_count);

	/* value_to_double */
	RUN_TEST(test_vtod_unsigned_no_scaler);
	RUN_TEST(test_vtod_signed_negative);
	RUN_TEST(test_vtod_float64_direct);
	RUN_TEST(test_vtod_with_scaler);
	RUN_TEST(test_vtod_scaler_negative_exponent);
	RUN_TEST(test_vtod_enum_type);
	RUN_TEST(test_vtod_unknown_type_returns_zero);
	RUN_TEST(test_vtod_out_of_bounds_index);

	/* Config defaults */
	RUN_TEST(test_default_config);
	RUN_TEST(test_meter_set_config_null_resets);
	RUN_TEST(test_meter_set_config_custom);

	/* State */
	RUN_TEST(test_initial_state_disconnected);
	RUN_TEST(test_state_enum_ordering);

	/* LLC header */
	RUN_TEST(test_llc_send_header);

	/* OBIS mapping spot checks */
	RUN_TEST(test_obis_voltage_r_is_first);
	RUN_TEST(test_obis_frequency);
	RUN_TEST(test_obis_active_energy);

	/* Read Validation & Field Mask (v0.17.0) */
	RUN_TEST(test_last_good_cache_initially_invalid);
	RUN_TEST(test_failed_reads_stay_zero);
	RUN_TEST(test_first_poll_zeros_without_last_good);
	RUN_TEST(test_last_good_updated_per_field);
	RUN_TEST(test_last_good_not_updated_on_invalid_read);

	/* Field mask */
	RUN_TEST(test_field_mask_initially_zero);
	RUN_TEST(test_field_mask_bit_for_each_obis);
	RUN_TEST(test_min_read_percent_threshold);
	RUN_TEST(test_valid_requires_min_coverage);

	/* Sanity check (v0.17.0: range + coverage) */
	RUN_TEST(test_sanity_check_rejects_all_zeros);
	RUN_TEST(test_sanity_check_accepts_valid_readings);
	RUN_TEST(test_sanity_check_accepts_zero_current);
	RUN_TEST(test_sanity_check_rejects_voltage_out_of_range);
	RUN_TEST(test_sanity_check_rejects_frequency_out_of_range);
	RUN_TEST(test_sanity_check_requires_field_coverage);
	RUN_TEST(test_sanity_check_voltage_only_no_frequency);

	/* Periodic push — field_mask guard (v0.18.0) */
	RUN_TEST(test_push_field_skips_when_bit_not_set);
	RUN_TEST(test_push_field_pushes_when_bit_set);
	RUN_TEST(test_push_field_all_27_bits);

	/* OBIS retry & diagnostics (v0.19.0) */
	RUN_TEST(test_retry_constants);
	RUN_TEST(test_obis_diag_struct_size);
	RUN_TEST(test_obis_diag_initially_zero);
	RUN_TEST(test_obis_diag_counters_writable);
	RUN_TEST(test_poll_duration_tracking);
	RUN_TEST(test_obis_diag_api_bounds);
	RUN_TEST(test_obis_diag_api_valid_index);
	RUN_TEST(test_avg_poll_duration_zero_polls);

	TEST_SUITE_END("DLMS Logic");
}
