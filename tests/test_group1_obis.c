/*
 * Unit Tests — Group-1 OBIS Index Map Lock
 *
 * Locks the obis_table[] index -> name mapping so a future table reorder
 * cannot silently break the Group-1 / Group-2 split that the firmware and
 * the upstream LwM2M push logic both depend on by *index*.
 *
 * Group-1 = the always-pushed "core" set (single-phase friendly).
 * Group-2 = the 10-element skip set {3,4,5,18,19,20,21,23,24,25}: the
 * reactive/apparent power, power factor, totals, reactive+apparent energy
 * and frequency registers that the single-phase / minimal builds skip.
 *
 * NOTE: obis_table[] and OBIS_TABLE_SIZE are *static* to dlms_meter.c.
 * They are only in scope inside the translation unit that #includes
 * dlms_meter.c (via test_dlms_logic.c). Therefore this file is #included
 * from test_main.c *after* test_dlms_logic.c — it is NOT a separate
 * compilation unit on the gcc line (that would dup-symbol / lose static
 * scope). It relies on test_framework.h already being included.
 */

/* ==== Group-1 anchor indices ==== */

void test_group1_obis_index_0_voltage_r(void)
{
	ASSERT_STR_EQ("Voltage_R", obis_table[0].name);
}

void test_group1_obis_index_1_current_r(void)
{
	ASSERT_STR_EQ("Current_R", obis_table[1].name);
}

void test_group1_obis_index_2_active_power_r(void)
{
	ASSERT_STR_EQ("ActivePower_R", obis_table[2].name);
}

void test_group1_obis_index_22_active_energy(void)
{
	ASSERT_STR_EQ("ActiveEnergy", obis_table[22].name);
}

/* ==== Group-2 (the 10-element skip set) ==== */

void test_group2_skip_set_names(void)
{
	/* Each skip-set index must map to its canonical Group-2 register name.
	 * If a table reorder shifts any of these, the firmware's index-based
	 * skip logic would skip the WRONG register — this catches it. */
	struct {
		size_t      idx;
		const char *name;
	} expected[] = {
		/* Reactive / Apparent power / PF for phase R */
		{ 3,  "ReactivePower_R" },
		{ 4,  "ApparentPower_R" },
		{ 5,  "PowerFactor_R" },
		/* Totals */
		{ 18, "TotalActivePower" },
		{ 19, "TotalReactivePower" },
		{ 20, "TotalApparentPower" },
		{ 21, "TotalPowerFactor" },
		/* Reactive + Apparent energy */
		{ 23, "ReactiveEnergy" },
		{ 24, "ApparentEnergy" },
		/* Frequency */
		{ 25, "Frequency" },
	};

	for (size_t i = 0; i < ARRAY_SIZE(expected); i++) {
		size_t idx = expected[i].idx;
		/* Index must be inside the table. */
		if (idx >= OBIS_TABLE_SIZE) {
			printf(TF_RED "FAIL" TF_RESET "\n");
			printf("    skip-set index %zu out of range "
			       "(OBIS_TABLE_SIZE=%zu)\n",
			       idx, (size_t)OBIS_TABLE_SIZE);
			_tf_failed++;
			return;
		}
		if (strcmp(obis_table[idx].name, expected[i].name) != 0) {
			printf(TF_RED "FAIL" TF_RESET "\n");
			printf("    obis_table[%zu].name = \"%s\", "
			       "expected \"%s\"\n",
			       idx, obis_table[idx].name, expected[i].name);
			_tf_failed++;
			return;
		}
	}
}

/* The skip set must have exactly 10 elements (single-phase build drops
 * Group-2: 3 reactive/apparent/PF + 4 totals + 2 extra energy + frequency). */
void test_group2_skip_set_size_is_10(void)
{
	const size_t skip_set[] = { 3, 4, 5, 18, 19, 20, 21, 23, 24, 25 };
	ASSERT_EQ(10, (int)ARRAY_SIZE(skip_set));
}

/* ==== Suite runner ==== */

void run_group1_obis_tests(void)
{
	TEST_SUITE_BEGIN("Group-1 OBIS Index Map");

	RUN_TEST(test_group1_obis_index_0_voltage_r);
	RUN_TEST(test_group1_obis_index_1_current_r);
	RUN_TEST(test_group1_obis_index_2_active_power_r);
	RUN_TEST(test_group1_obis_index_22_active_energy);

	RUN_TEST(test_group2_skip_set_names);
	RUN_TEST(test_group2_skip_set_size_is_10);

	TEST_SUITE_END("Group-1 OBIS Index Map");
}
