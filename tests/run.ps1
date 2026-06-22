# run.ps1 — one-command unit-test runner for the AMI LwM2M node.
#
# Compiles the native-gcc host harness (no ztest / no twister) and runs it.
#
# Usage:
#   pwsh ./run.ps1
#
# Translation-unit notes:
#   - test_main.c is the entry point. It #includes test_dlms_logic.c, which
#     in turn #includes ../src/dlms_meter.c directly (to reach its static
#     obis_table[], value_to_double(), etc). test_main.c then #includes
#     test_group1_obis.c, which also relies on that static scope.
#   - Therefore dlms_meter.c, test_dlms_logic.c and test_group1_obis.c are
#     NOT listed on the gcc line — adding them would duplicate symbols and
#     break static-scope access. Only the standalone units (test_hdlc.c,
#     test_cosem.c) and the libraries they link (dlms_hdlc.c, dlms_cosem.c)
#     are compiled separately.

$ErrorActionPreference = 'Stop'

# Always operate from the tests/ directory (this script's location).
$testsDir = $PSScriptRoot
Set-Location $testsDir

$exe = Join-Path $testsDir 'run_tests.exe'

$sources = @(
    'test_main.c',
    'test_hdlc.c',
    'test_cosem.c',
    '../src/dlms_hdlc.c',
    '../src/dlms_cosem.c'
)

$gccArgs = @(
    '-o', $exe
) + $sources + @(
    '-DUNIT_TEST',
    '-I../src',
    '-Istubs',
    '-lm',
    '-Wall'
)

Write-Host "[run.ps1] Compiling unit-test harness..."
& gcc @gccArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "[run.ps1] BUILD FAILED (gcc exit $LASTEXITCODE)" -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "[run.ps1] Running tests..."
& $exe
$rc = $LASTEXITCODE

if ($rc -eq 0) {
    Write-Host "[run.ps1] ALL TESTS PASSED" -ForegroundColor Green
} else {
    Write-Host "[run.ps1] TESTS FAILED (exit $rc)" -ForegroundColor Red
}

exit $rc
