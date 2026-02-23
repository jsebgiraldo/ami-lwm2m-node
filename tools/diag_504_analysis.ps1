<#
.SYNOPSIS
  Diagnostic tool to analyze 504 (CoAP timeout) failures in LwM2M reads
  over Thread mesh and identify root causes.

.DESCRIPTION
  Runs a series of targeted tests:
    Test 1 - Ping latency to device via OTBR (Thread mesh reachability)
    Test 2 - Single-resource repeated reads (isolate per-resource issues)
    Test 3 - Delay sweep (find minimum safe inter-request delay)
    Test 4 - Burst test (rapid-fire reads to measure congestion onset)
    Test 5 - Registration check (verify client is registered on Leshan)
#>
param(
    [string]$BaseUri   = "http://192.168.1.111:18080/api",
    [string]$Endpoint  = "ami-esp32c6-25c0",
    [string]$OtbrHost  = "192.168.1.111",
    [string]$DeviceIPv6 = ""
)

$ErrorActionPreference = 'SilentlyContinue'

# -- Helpers ---------------------------------------------------------------
function Read-Resource {
    param([string]$Path, [int]$TimeoutSec = 8)
    $uri = "$BaseUri/clients/$Endpoint/$Path"
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $resp = Invoke-RestMethod -Uri $uri -TimeoutSec $TimeoutSec -ErrorAction Stop
        $sw.Stop()
        $status = "CONTENT"
        $val = if ($resp.content.value) { $resp.content.value } else { $resp.content }
    } catch {
        $sw.Stop()
        $status = "FAIL"
        $val = $_.Exception.Message
    }
    [PSCustomObject]@{
        Path      = $Path
        LatencyMs = $sw.ElapsedMilliseconds
        Status    = $status
        Value     = "$val"
    }
}

function Write-Header ($title) {
    Write-Host ""
    Write-Host ("=" * 70) -ForegroundColor Cyan
    Write-Host "  $title" -ForegroundColor Cyan
    Write-Host ("=" * 70) -ForegroundColor Cyan
}

function Write-Result ($label, $value, $color = "White") {
    Write-Host ("  {0,-35} {1}" -f $label, $value) -ForegroundColor $color
}

# -- Auto-detect device IPv6 from Leshan ----------------------------------
Write-Header "DIAGNOSTIC: 504 Failure Root-Cause Analysis"
Write-Host "  Endpoint : $Endpoint"
Write-Host "  Leshan   : $BaseUri"
Write-Host "  Time     : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"

try {
    $clientInfo = Invoke-RestMethod -Uri "$BaseUri/clients/$Endpoint" -TimeoutSec 5
    Write-Host ""
    Write-Result "Registration" "ACTIVE" "Green"
    Write-Result "Address" $clientInfo.address
    Write-Result "Lifetime" "$($clientInfo.lifetime)s"
    Write-Result "Binding" $clientInfo.bindingMode
    Write-Result "Last update" $clientInfo.lastUpdate
    if (-not $DeviceIPv6 -and $clientInfo.address) {
        if ($clientInfo.address -match '\[([^\]]+)\]') {
            $DeviceIPv6 = $Matches[1]
        }
    }
} catch {
    Write-Host ""
    Write-Result "Registration" "NOT FOUND -- device may be offline!" "Red"
}

# =========================================================================
# TEST 1: Thread Mesh Ping Latency
# =========================================================================
Write-Header "TEST 1: Thread Mesh Ping Latency (OTBR -> Device)"

if ($DeviceIPv6) {
    Write-Host "  Pinging $DeviceIPv6 via OTBR..."
    $pingResults = @()
    for ($i = 1; $i -le 10; $i++) {
        try {
            $raw = ssh root@$OtbrHost "ping6 -c 1 -W 5 $DeviceIPv6 2>&1" 2>$null
            if ($raw -match 'time[=<](\d+\.?\d*)') {
                $ms = [double]$Matches[1]
                $pingResults += $ms
                Write-Host "    Ping $i : ${ms}ms" -ForegroundColor Green
            } else {
                $pingResults += -1
                Write-Host "    Ping $i : TIMEOUT" -ForegroundColor Red
            }
        } catch {
            $pingResults += -1
            Write-Host "    Ping $i : ERROR" -ForegroundColor Red
        }
        Start-Sleep -Milliseconds 500
    }
    $okPings = $pingResults | Where-Object { $_ -ge 0 }
    if ($okPings.Count -gt 0) {
        $avgPing = ($okPings | Measure-Object -Average).Average
        $maxPing = ($okPings | Measure-Object -Maximum).Maximum
        $lostPct = [math]::Round(($pingResults.Count - $okPings.Count) / $pingResults.Count * 100)
        Write-Host ""
        Write-Result "Avg ping" ("{0:N1}ms" -f $avgPing)
        Write-Result "Max ping" ("{0:N1}ms" -f $maxPing)
        Write-Result "Packet loss" "$lostPct%"
    } else {
        Write-Host "  ALL PINGS FAILED -- Thread mesh may be down!" -ForegroundColor Red
    }
} else {
    Write-Host "  SKIP -- Device IPv6 not available" -ForegroundColor Yellow
}

# =========================================================================
# TEST 2: Single-Resource Reliability (10 reads of each critical resource)
# =========================================================================
Write-Header "TEST 2: Single-Resource Reliability (10 reads, 5s delay)"

$testResources = @(
    @{ Path = "3/0/0";       Label = "Device/Manufacturer (fast)" },
    @{ Path = "10242/0/0";   Label = "Power Meter/voltage_a (medium)" },
    @{ Path = "10483/0/5";   Label = "Thread Network/IPv6Addrs (worst)" },
    @{ Path = "10484/0/0";   Label = "Thread Commission/JoinerEUI" },
    @{ Path = "10486/0/0";   Label = "Thread Diag/Role" }
)

$test2Results = @()
foreach ($res in $testResources) {
    Write-Host ""
    Write-Host "  >>> $($res.Label) [$($res.Path)]" -ForegroundColor Yellow
    $successes = 0
    $latencies = @()
    for ($i = 1; $i -le 10; $i++) {
        $r = Read-Resource -Path $res.Path -TimeoutSec 8
        if ($r.Status -eq "CONTENT") {
            $successes++
            $latencies += $r.LatencyMs
            $c = if ($r.LatencyMs -lt 100) { "Green" } elseif ($r.LatencyMs -lt 3000) { "Yellow" } else { "Red" }
            Write-Host ("    [{0,2}/10] {1,6}ms CONTENT" -f $i, $r.LatencyMs) -ForegroundColor $c
        } else {
            Write-Host ("    [{0,2}/10] {1,6}ms FAIL" -f $i, $r.LatencyMs) -ForegroundColor Red
        }
        Start-Sleep -Seconds 5
    }
    $rate = [math]::Round($successes / 10 * 100)
    $avgLat = if ($latencies.Count -gt 0) { [math]::Round(($latencies | Measure-Object -Average).Average) } else { "N/A" }
    Write-Host "    Success: $rate% ($successes/10), Avg latency: ${avgLat}ms"
    $test2Results += [PSCustomObject]@{
        Resource    = $res.Label
        Path        = $res.Path
        SuccessRate = $rate
        AvgLatency  = $avgLat
        Successes   = $successes
    }
}

Write-Host ""
Write-Host "  -- Test 2 Summary --" -ForegroundColor Cyan
foreach ($r in $test2Results) {
    $c = if ($r.SuccessRate -ge 90) { "Green" } elseif ($r.SuccessRate -ge 60) { "Yellow" } else { "Red" }
    Write-Host ("  {0,-45} {1}% success, avg {2}ms" -f $r.Resource, $r.SuccessRate, $r.AvgLatency) -ForegroundColor $c
}

# =========================================================================
# TEST 3: Delay Sweep -- Find minimum safe inter-request delay
# =========================================================================
Write-Header "TEST 3: Delay Sweep (5 reads x 6 delays)"

$sweepPath = "3/0/0"
$delays = @(1000, 2000, 3000, 5000, 8000, 10000)

Write-Host "  Resource: Device/Manufacturer [$sweepPath]"
Write-Host ""

$sweepResults = @()
foreach ($delayMs in $delays) {
    $successes = 0
    $latencies = @()
    for ($i = 1; $i -le 5; $i++) {
        $r = Read-Resource -Path $sweepPath -TimeoutSec 8
        if ($r.Status -eq "CONTENT") { $successes++; $latencies += $r.LatencyMs }
        Start-Sleep -Milliseconds $delayMs
    }
    $rate = [math]::Round($successes / 5 * 100)
    $avgLat = if ($latencies.Count -gt 0) { [math]::Round(($latencies | Measure-Object -Average).Average) } else { "N/A" }
    $c = if ($rate -ge 100) { "Green" } elseif ($rate -ge 60) { "Yellow" } else { "Red" }
    Write-Host ("  Delay {0,5}ms -> {1}% success ({2}/5), avg {3}ms" -f $delayMs, $rate, $successes, $avgLat) -ForegroundColor $c
    $sweepResults += [PSCustomObject]@{ DelayMs = $delayMs; SuccessRate = $rate; AvgLatency = $avgLat }
}

# =========================================================================
# TEST 4: Burst Test -- Rapid-fire reads to find congestion threshold
# =========================================================================
Write-Header "TEST 4: Burst Test (20 rapid reads, 500ms delay)"

$burstPath = "3/0/0"
Write-Host "  Resource: Device/Manufacturer [$burstPath]"
Write-Host "  Delay: 500ms between requests"
Write-Host ""

$burstOk = 0
$burstFail = 0
$firstFailAt = -1
for ($i = 1; $i -le 20; $i++) {
    $r = Read-Resource -Path $burstPath -TimeoutSec 8
    if ($r.Status -eq "CONTENT") {
        $burstOk++
        $c = if ($r.LatencyMs -lt 100) { "Green" } else { "Yellow" }
        Write-Host ("  [{0,2}/20] {1,6}ms CONTENT" -f $i, $r.LatencyMs) -ForegroundColor $c
    } else {
        $burstFail++
        if ($firstFailAt -eq -1) { $firstFailAt = $i }
        Write-Host ("  [{0,2}/20] {1,6}ms FAIL" -f $i, $r.LatencyMs) -ForegroundColor Red
    }
    Start-Sleep -Milliseconds 500
}

Write-Host ""
Write-Result "Success" "$burstOk/20 ($([math]::Round($burstOk/20*100))%)"
Write-Result "Failed" "$burstFail/20"
if ($firstFailAt -gt 0) {
    Write-Result "First failure at" "request #$firstFailAt" "Red"
    Write-Host "  >> Congestion starts after ~$firstFailAt requests at 500ms intervals" -ForegroundColor Yellow
} else {
    Write-Host "  >> No congestion detected at 500ms intervals" -ForegroundColor Green
}

# =========================================================================
# TEST 5: Registration Update Interference
# =========================================================================
Write-Header "TEST 5: Registration Lifetime and Update Window"

try {
    $client = Invoke-RestMethod -Uri "$BaseUri/clients/$Endpoint" -TimeoutSec 5
    $lifetime = $client.lifetime
    Write-Result "Server lifetime" "${lifetime}s"
    Write-Result "Update period (est.)" "$($lifetime - 10)s  (lifetime - 10s early)"

    $srvLifetime = Read-Resource -Path "1/0/1" -TimeoutSec 8
    Write-Result "Device-reported lifetime" "$($srvLifetime.Value)s"

    if ($lifetime -le 60) {
        Write-Host ""
        Write-Host "  WARNING: Short lifetime ($lifetime s) means frequent registration" -ForegroundColor Red
        Write-Host "    updates. Each update uses the CoAP channel, potentially blocking" -ForegroundColor Red
        Write-Host "    read requests and contributing to 504 timeouts." -ForegroundColor Red
        Write-Host ""
        Write-Host "  RECOMMENDATION: Increase CONFIG_LWM2M_ENGINE_DEFAULT_LIFETIME to 300" -ForegroundColor Yellow
    }
} catch {
    Write-Host "  Could not check registration info" -ForegroundColor Red
}

# =========================================================================
# DIAGNOSIS SUMMARY
# =========================================================================
Write-Header "DIAGNOSIS SUMMARY and RECOMMENDATIONS"

Write-Host ""
Write-Host "  ROOT CAUSE ANALYSIS:"
Write-Host "  ---------------------"
Write-Host "  The 504 failures are caused by CoAP timeouts at the Leshan server."
Write-Host "  When Leshan sends a Read request to the ESP32 over the Thread mesh,"
Write-Host "  the Californium CoAP library waits ~5s for a response. If the device"
Write-Host "  cannot respond in time (due to mesh latency, packet loss, or being"
Write-Host "  busy processing a previous request), Leshan returns HTTP 504."
Write-Host ""
Write-Host "  CONTRIBUTING FACTORS:"
Write-Host "  ----------------------"
Write-Host "  1. Thread mesh latency: Real CoAP reads take 2-3s round-trip"
Write-Host "  2. Short registration lifetime (30s): Updates every ~20s occupy"
Write-Host "     the CoAP channel, blocking read responses"
Write-Host "  3. Leshan CoAP timeout (~5s): Too tight for Thread mesh + device"
Write-Host "     processing time"
Write-Host "  4. Sequential request queuing: When reads arrive faster than the"
Write-Host "     device can process, responses are delayed beyond timeout"
Write-Host ""
Write-Host "  RECOMMENDED FIXES (in priority order):" -ForegroundColor Yellow
Write-Host "  ----------------------------------------"
Write-Host "  [FW-1] Increase LwM2M lifetime: 30s -> 300s"
Write-Host "         CONFIG_LWM2M_ENGINE_DEFAULT_LIFETIME=300"
Write-Host "         Reduces registration update traffic by 10x"
Write-Host ""
Write-Host "  [FW-2] Increase CoAP initial ACK timeout: 2s -> 5s"
Write-Host "         CONFIG_COAP_INIT_ACK_TIMEOUT_MS=5000"
Write-Host "         Gives more time for Thread mesh round-trips"
Write-Host ""
Write-Host "  [FW-3] Increase network buffers"
Write-Host "         CONFIG_NET_PKT_RX_COUNT=32"
Write-Host "         CONFIG_NET_PKT_TX_COUNT=32"
Write-Host "         Reduces packet drops under load"
Write-Host ""
Write-Host "  [SRV-1] Increase Leshan CoAP response timeout"
Write-Host "          Add Californium configuration for longer Thread mesh waits"
Write-Host ""
Write-Host "  [TEST-1] Use 5-8s inter-request delay instead of 3s"
Write-Host ""
Write-Host "  Diagnostic completed: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Cyan
Write-Host ""
