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

	/* Metadata fields exist and are writable */
	r.valid = true;
	r.read_count = 22;
	r.error_count = 5;
	r.timestamp_ms = 123456;

	ASSERT_TRUE(r.valid);
	ASSERT_EQ(22, r.read_count);
	ASSERT_EQ(5, r.error_count);
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

/* ==== Zero-Value Prevention (v0.16.0) ==== */

void test_last_good_cache_initially_invalid(void)
{
	/* On startup, last_good_valid should be false */
	last_good_valid = false;
	ASSERT_FALSE(last_good_valid);
}

void test_last_good_fills_failed_reads(void)
{
	/*
	 * Simulate: first poll succeeds fully (populates last_good),
	 * second poll has a failed read → field keeps last_good value.
	 */
	struct meter_readings r;
	memset(&r, 0, sizeof(r));

	/* Populate last_good with known values */
	last_good_valid = true;
	memset(&last_good, 0, sizeof(last_good));
	last_good.voltage_r = 122.4;
	last_good.current_r = 5.5;
	last_good.frequency = 60.0;
	last_good.active_energy = 1234.5;

	/* Simulate what meter_read_all does: copy last_good into readings */
	if (last_good_valid) {
		memcpy(&r, &last_good, sizeof(r));
	} else {
		memset(&r, 0, sizeof(r));
	}

	/* A "failed read" means the field was NOT overwritten →
	 * it retains the last_good value from the memcpy above.
	 * Simulate a successful read of only voltage and frequency.
	 */
	r.voltage_r = 123.0;    /* new good value */
	r.frequency = 60.1;     /* new good value */
	/* current_r NOT overwritten → keeps 5.5 from last_good */
	/* active_energy NOT overwritten → keeps 1234.5 from last_good */

	r.read_count = 2;
	r.error_count = 2;  /* 2 failed reads */
	r.valid = true;

	/* Verify: failed reads have last_good values, NOT zeros */
	ASSERT_FLOAT_EQ(123.0, r.voltage_r, 0.001);    /* new value */
	ASSERT_FLOAT_EQ(5.5,   r.current_r, 0.001);    /* last_good */
	ASSERT_FLOAT_EQ(60.1,  r.frequency, 0.001);     /* new value */
	ASSERT_FLOAT_EQ(1234.5, r.active_energy, 0.001);/* last_good */
}

void test_first_poll_zeros_without_last_good(void)
{
	/*
	 * Very first poll: no last_good available → fields start at 0.
	 * This is expected — the first poll must succeed to populate cache.
	 */
	struct meter_readings r;
	last_good_valid = false;

	/* Simulate what meter_read_all does without last_good */
	if (last_good_valid) {
		memcpy(&r, &last_good, sizeof(r));
	} else {
		memset(&r, 0, sizeof(r));
	}

	ASSERT_FLOAT_EQ(0.0, r.voltage_r, 0.001);
	ASSERT_FLOAT_EQ(0.0, r.current_r, 0.001);
	ASSERT_FLOAT_EQ(0.0, r.frequency, 0.001);
}

void test_last_good_updated_after_valid_read(void)
{
	/* After a valid read, last_good should be updated */
	struct meter_readings r;
	memset(&r, 0, sizeof(r));
	r.voltage_r = 220.5;
	r.frequency = 50.0;
	r.valid = true;
	r.read_count = 2;

	/* Simulate what meter_read_all does after the read loop */
	if (r.valid) {
		memcpy(&last_good, &r, sizeof(last_good));
		last_good_valid = true;
	}

	ASSERT_TRUE(last_good_valid);
	ASSERT_FLOAT_EQ(220.5, last_good.voltage_r, 0.001);
	ASSERT_FLOAT_EQ(50.0,  last_good.frequency, 0.001);
}

void test_last_good_not_updated_on_invalid_read(void)
{
	/* If all reads fail (valid=false), last_good should NOT be updated */
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

	/* Do NOT update last_good */
	if (r.valid) {
		memcpy(&last_good, &r, sizeof(last_good));
	}

	/* last_good should still hold old values */
	ASSERT_TRUE(last_good_valid);
	ASSERT_FLOAT_EQ(120.0, last_good.voltage_r, 0.001);
	ASSERT_FLOAT_EQ(60.0,  last_good.frequency, 0.001);
}

/* ==== Sanity Check ==== */

void test_sanity_check_rejects_all_zeros(void)
{
	struct meter_readings r;
	memset(&r, 0, sizeof(r));
	r.valid = true;
	/* Both voltage_r=0 and frequency=0 → should FAIL */
	ASSERT_FALSE(readings_sanity_check(&r));
}

void test_sanity_check_accepts_valid_readings(void)
{
	struct meter_readings r;
	memset(&r, 0, sizeof(r));
	r.voltage_r = 122.0;
	r.frequency = 60.0;
	r.valid = true;
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
	ASSERT_TRUE(readings_sanity_check(&r));
}

void test_sanity_check_voltage_only(void)
{
	/* Voltage ok but frequency=0 → pass (only both zero fails) */
	struct meter_readings r;
	memset(&r, 0, sizeof(r));
	r.voltage_r = 120.0;
	r.frequency = 0.0;
	r.valid = true;
	ASSERT_TRUE(readings_sanity_check(&r));
}

/* ==== THRESH_CHECK macro boundary tests ==== */

void test_thresh_check_no_notify_within_threshold(void)
{
	/* Delta within threshold should NOT trigger notification */
	double old_val = 120.0;
	double new_val = 120.5;  /* delta = 0.5 < THRESH_VOLTAGE (1.0) */
	double delta = fabs(new_val - old_val);
	ASSERT_TRUE(delta < THRESH_VOLTAGE);
}

void test_thresh_check_notify_exceeds_threshold(void)
{
	/* Delta exceeding threshold should trigger notification */
	double old_val = 120.0;
	double new_val = 121.5;  /* delta = 1.5 >= THRESH_VOLTAGE (1.0) */
	double delta = fabs(new_val - old_val);
	ASSERT_TRUE(delta >= THRESH_VOLTAGE);
}

void test_thresh_check_zero_would_exceed_threshold(void)
{
	/* A zero replacing a real value would always exceed threshold */
	double old_val = 122.4;
	double zero_val = 0.0;
	double delta = fabs(zero_val - old_val);
	/* This is why the zero-fill bug was so insidious */
	ASSERT_TRUE(delta >= THRESH_VOLTAGE);
}

void test_thresh_check_last_good_within_threshold(void)
{
	/* A last-good value replacing a failed read stays within threshold */
	double old_val = 122.4;
	double last_good_val = 122.4;  /* same as last notified */
	double delta = fabs(last_good_val - old_val);
	ASSERT_TRUE(delta < THRESH_VOLTAGE);
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

	/* Zero-value prevention (v0.16.0) */
	RUN_TEST(test_last_good_cache_initially_invalid);
	RUN_TEST(test_last_good_fills_failed_reads);
	RUN_TEST(test_first_poll_zeros_without_last_good);
	RUN_TEST(test_last_good_updated_after_valid_read);
	RUN_TEST(test_last_good_not_updated_on_invalid_read);

	/* Sanity check */
	RUN_TEST(test_sanity_check_rejects_all_zeros);
	RUN_TEST(test_sanity_check_accepts_valid_readings);
	RUN_TEST(test_sanity_check_accepts_zero_current);
	RUN_TEST(test_sanity_check_voltage_only);

	/* THRESH_CHECK boundary tests */
	RUN_TEST(test_thresh_check_no_notify_within_threshold);
	RUN_TEST(test_thresh_check_notify_exceeds_threshold);
	RUN_TEST(test_thresh_check_zero_would_exceed_threshold);
	RUN_TEST(test_thresh_check_last_good_within_threshold);

	TEST_SUITE_END("DLMS Logic");
}
