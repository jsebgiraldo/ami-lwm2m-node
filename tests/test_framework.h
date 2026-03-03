/*
 * Minimal Unit Test Framework — No Dependencies
 *
 * Provides ASSERT macros, test registration, and a simple runner.
 * Designed for embedded C projects that need to test on the host.
 */
#ifndef TEST_FRAMEWORK_H_
#define TEST_FRAMEWORK_H_

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

/* ---- Counters ---- */
/* Defined in test_main.c, shared across all translation units */
#ifdef TEST_MAIN_FILE
int _tf_total   = 0;
int _tf_passed  = 0;
int _tf_failed  = 0;
#else
extern int _tf_total;
extern int _tf_passed;
extern int _tf_failed;
#endif
static int _tf_suite_total  = 0;
static int _tf_suite_passed = 0;
static int _tf_suite_failed = 0;

/* ---- Colors (ANSI) ---- */
#define TF_GREEN  "\033[32m"
#define TF_RED    "\033[31m"
#define TF_YELLOW "\033[33m"
#define TF_CYAN   "\033[36m"
#define TF_RESET  "\033[0m"

/* ---- Suite management ---- */
#define TEST_SUITE_BEGIN(name) \
	do { \
		printf("\n" TF_CYAN "=== Suite: %s ===" TF_RESET "\n", name); \
		_tf_suite_total = 0; \
		_tf_suite_passed = 0; \
		_tf_suite_failed = 0; \
	} while (0)

#define TEST_SUITE_END(name) \
	do { \
		if (_tf_suite_failed == 0) { \
			printf(TF_GREEN "  Suite %s: %d/%d PASSED" TF_RESET "\n", \
			       name, _tf_suite_passed, _tf_suite_total); \
		} else { \
			printf(TF_RED "  Suite %s: %d/%d FAILED (%d errors)" TF_RESET "\n", \
			       name, _tf_suite_passed, _tf_suite_total, _tf_suite_failed); \
		} \
	} while (0)

/* ---- Run a test function ---- */
#define RUN_TEST(fn) \
	do { \
		_tf_total++; \
		_tf_suite_total++; \
		int _before = _tf_failed; \
		printf("  [TEST] %-50s ", #fn); \
		fn(); \
		if (_tf_failed == _before) { \
			_tf_passed++; \
			_tf_suite_passed++; \
			printf(TF_GREEN "PASS" TF_RESET "\n"); \
		} else { \
			_tf_suite_failed += (_tf_failed - _before); \
		} \
	} while (0)

/* ---- Assertions ---- */
#define ASSERT_TRUE(cond) \
	do { \
		if (!(cond)) { \
			printf(TF_RED "FAIL" TF_RESET "\n"); \
			printf("    %s:%d: ASSERT_TRUE(%s) failed\n", \
			       __FILE__, __LINE__, #cond); \
			_tf_failed++; \
			return; \
		} \
	} while (0)

#define ASSERT_FALSE(cond) ASSERT_TRUE(!(cond))

#define ASSERT_EQ(expected, actual) \
	do { \
		long long _e = (long long)(expected); \
		long long _a = (long long)(actual); \
		if (_e != _a) { \
			printf(TF_RED "FAIL" TF_RESET "\n"); \
			printf("    %s:%d: ASSERT_EQ: expected %lld, got %lld\n", \
			       __FILE__, __LINE__, _e, _a); \
			_tf_failed++; \
			return; \
		} \
	} while (0)

#define ASSERT_NE(expected, actual) \
	do { \
		long long _e = (long long)(expected); \
		long long _a = (long long)(actual); \
		if (_e == _a) { \
			printf(TF_RED "FAIL" TF_RESET "\n"); \
			printf("    %s:%d: ASSERT_NE: %lld == %lld (should differ)\n", \
			       __FILE__, __LINE__, _e, _a); \
			_tf_failed++; \
			return; \
		} \
	} while (0)

#define ASSERT_GT(a, b) \
	do { \
		long long _a = (long long)(a); \
		long long _b = (long long)(b); \
		if (!(_a > _b)) { \
			printf(TF_RED "FAIL" TF_RESET "\n"); \
			printf("    %s:%d: ASSERT_GT: %lld not > %lld\n", \
			       __FILE__, __LINE__, _a, _b); \
			_tf_failed++; \
			return; \
		} \
	} while (0)

#define ASSERT_GE(a, b) \
	do { \
		long long _a = (long long)(a); \
		long long _b = (long long)(b); \
		if (!(_a >= _b)) { \
			printf(TF_RED "FAIL" TF_RESET "\n"); \
			printf("    %s:%d: ASSERT_GE: %lld not >= %lld\n", \
			       __FILE__, __LINE__, _a, _b); \
			_tf_failed++; \
			return; \
		} \
	} while (0)

#define ASSERT_LT(a, b) ASSERT_GT(b, a)

#define ASSERT_FLOAT_EQ(expected, actual, epsilon) \
	do { \
		double _e = (double)(expected); \
		double _a = (double)(actual); \
		if (fabs(_e - _a) > (double)(epsilon)) { \
			printf(TF_RED "FAIL" TF_RESET "\n"); \
			printf("    %s:%d: ASSERT_FLOAT_EQ: expected %.6f, got %.6f (eps=%.6f)\n", \
			       __FILE__, __LINE__, _e, _a, (double)(epsilon)); \
			_tf_failed++; \
			return; \
		} \
	} while (0)

#define ASSERT_MEM_EQ(expected, actual, len) \
	do { \
		if (memcmp(expected, actual, len) != 0) { \
			printf(TF_RED "FAIL" TF_RESET "\n"); \
			printf("    %s:%d: ASSERT_MEM_EQ: memory differs (%zu bytes)\n", \
			       __FILE__, __LINE__, (size_t)(len)); \
			_tf_failed++; \
			return; \
		} \
	} while (0)

#define ASSERT_STR_EQ(expected, actual) \
	do { \
		if (strcmp(expected, actual) != 0) { \
			printf(TF_RED "FAIL" TF_RESET "\n"); \
			printf("    %s:%d: ASSERT_STR_EQ: \"%s\" != \"%s\"\n", \
			       __FILE__, __LINE__, expected, actual); \
			_tf_failed++; \
			return; \
		} \
	} while (0)

/* ---- Summary ---- */
#define TEST_SUMMARY() \
	do { \
		printf("\n" TF_CYAN "==============================" TF_RESET "\n"); \
		if (_tf_failed == 0) { \
			printf(TF_GREEN "ALL %d TESTS PASSED" TF_RESET "\n", _tf_total); \
		} else { \
			printf(TF_RED "%d PASSED, %d FAILED (of %d total)" TF_RESET "\n", \
			       _tf_passed, _tf_failed, _tf_total); \
		} \
		printf(TF_CYAN "==============================" TF_RESET "\n"); \
	} while (0)

#define TEST_EXIT_CODE() (_tf_failed > 0 ? 1 : 0)

#endif /* TEST_FRAMEWORK_H_ */
