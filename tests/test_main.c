/*
 * AMI LwM2M Node — Unit Test Runner
 *
 * Compile (Windows, GCC/MinGW):
 *   cd tests
 *   gcc -o run_tests.exe test_main.c test_hdlc.c test_cosem.c \
 *       ../src/dlms_hdlc.c ../src/dlms_cosem.c \
 *       -I../src -Istubs -DUNIT_TEST -lm -Wall -Wextra
 *
 * Run:
 *   .\run_tests.exe
 *
 * Note: test_dlms_logic.c is NOT listed as a separate compilation unit
 * because it #includes dlms_meter.c directly to access static functions.
 * Instead, test_dlms_logic.c is #included from this file.
 */
#define TEST_MAIN_FILE  /* Define global counters in this translation unit */
#include "test_framework.h"

/* Test suite runners — defined in each test file */
extern void run_hdlc_tests(void);
extern void run_cosem_tests(void);

/*
 * DLMS logic tests include dlms_meter.c directly (static function access).
 * Include the test file here so it becomes part of this translation unit,
 * along with the full dlms_meter.c static scope.
 */
#include "test_dlms_logic.c"

int main(void)
{
	printf("\n");
	printf("==============================================\n");
	printf("  AMI LwM2M Node — Unit Test Suite\n");
	printf("  DLMS/COSEM • HDLC • Meter Logic\n");
	printf("==============================================\n");

	run_hdlc_tests();
	run_cosem_tests();
	run_dlms_logic_tests();

	TEST_SUMMARY();
	return TEST_EXIT_CODE();
}
