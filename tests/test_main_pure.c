#define TEST_MAIN_FILE
#include "test_framework.h"
#include <stdio.h>
extern void run_hdlc_tests(void);
extern void run_cosem_tests(void);
int main(void){
    printf("=== Pure logic tests (HDLC + COSEM) v0.6.69.1 ===\n");
    run_hdlc_tests();
    run_cosem_tests();
    printf("\n=== TOTAL=%d PASSED=%d FAILED=%d ===\n", _tf_total, _tf_passed, _tf_failed);
    return _tf_failed == 0 ? 0 : 1;
}
