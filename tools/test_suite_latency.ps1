<#
.SYNOPSIS
    LwM2M Read Latency Test Suite v2 — with warmup + retry
.DESCRIPTION
    Runs N rounds of individual resource reads across all LwM2M objects,
    testing with configurable inter-request delays. Includes warmup phase
    to prime CoAP path and retry logic for transient failures.

    Output CSV columns:
      Timestamp, Round, SeqNum, Object, ObjectName, Instance, Resource,
      ResourceLabel, DelayMs, LatencyMs, Status, Value, Error
#>

param(
    [string]$Endpoint  = "ami-esp32c6-25c0",
    [string]$Server    = "http://192.168.1.111:18080",
    [int]$Rounds       = 10,
    [int]$DelayMs      = 3000,
    [int]$TimeoutSec   = 30,
    [string]$OutputDir = ".\results",
    [switch]$NoWarmup,
    [int]$MaxRetries   = 0,
    [int]$RetryDelayMs = 3000,
    [int]$CoapTimeoutSec = 10
)

$base = "$Server/api/clients/$Endpoint"

# Resource definitions: Object, Instance, Resource, ObjectName, ResourceLabel
$resources = @(
    @(1,  0, 1,  "LwM2M Server",        "Lifetime"),
    @(3,  0, 0,  "Device",              "Manufacturer"),
    @(3,  0, 3,  "Device",              "FWVersion"),
    @(4,  0, 0,  "Conn Monitor",        "Bearer"),
    @(4,  0, 2,  "Conn Monitor",        "RSSI"),
    @(4,  0, 3,  "Conn Monitor",        "LinkQuality"),
    @(4,  0, 4,  "Conn Monitor",        "IPAddresses"),
    @(10242, 0, 0, "Power Meter",       "voltage_a"),
    @(10483, 0, 0, "Thread Network",    "NetworkName"),
    @(10483, 0, 2, "Thread Network",    "ExtPanId"),
    @(10483, 0, 5, "Thread Network",    "RLOC16"),
    @(10483, 0, 11,"Thread Network",    "IPv6Addrs"),
    @(10484, 0, 0, "Thread Commission", "JoinerEUI"),
    @(10485, 0, 0, "Thread Neighbor",   "Role"),
    @(10485, 0, 1, "Thread Neighbor",   "RLOC16"),
    @(10485, 0, 3, "Thread Neighbor",   "AvgRSSI"),
    @(10485, 0, 8, "Thread Neighbor",   "ExtMAC"),
    @(10486, 0, 0, "Thread CLI",        "Version"),
    @(10486, 0, 3, "Thread CLI",        "Result"),
    @(33000, 0, 0, "Thread Diag",       "Role"),
    @(33000, 0, 1, "Thread Diag",       "PartitionID"),
    @(33000, 0, 2, "Thread Diag",       "TxTotal")
)

# Setup
if (-not (Test-Path $OutputDir)) { New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null }

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$csvFile   = Join-Path $OutputDir "latency_${timestamp}_delay${DelayMs}ms_${Rounds}rounds.csv"

$warmupLabel = "ON"
if ($NoWarmup) { $warmupLabel = "OFF" }

Write-Host ""
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  LwM2M Read Latency Test Suite v2" -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  Endpoint  : $Endpoint"
Write-Host "  Server    : $Server"
Write-Host "  Rounds    : $Rounds"
Write-Host "  Delay     : ${DelayMs}ms"
Write-Host "  CoAP Tout : ${CoapTimeoutSec}s (Leshan API ?timeout=)"
Write-Host "  Retries   : $MaxRetries (backoff ${RetryDelayMs}ms)"
Write-Host "  Warmup    : $warmupLabel"
Write-Host "  Resources : $($resources.Count) per round"
Write-Host "  Total Reqs: $($resources.Count * $Rounds)"
Write-Host "  Est. Time : $([math]::Round(($resources.Count * $Rounds * ($DelayMs + 100)) / 60000, 1)) min"
Write-Host "  Output    : $csvFile"
Write-Host "  Started   : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host ""

# Verify device registration
try {
    $clients = (Invoke-WebRequest -Uri "$Server/api/clients" -UseBasicParsing -TimeoutSec 10).Content | ConvertFrom-Json
    $found = $clients | Where-Object { $_.endpoint -eq $Endpoint }
    if (-not $found) {
        Write-Host "ERROR: Endpoint '$Endpoint' not registered!" -ForegroundColor Red
        exit 1
    }
    Write-Host "Device registered. Last update: $($found.lastUpdate)" -ForegroundColor Green
}
catch {
    Write-Host "ERROR: Cannot reach Leshan at $Server" -ForegroundColor Red
    exit 1
}

# Warmup phase
if (-not $NoWarmup) {
    Write-Host ""
    Write-Host "-- Warmup Phase (not counted) --" -ForegroundColor Magenta
    $warmupPaths = @("1/0/1", "3/0/0", "3/0/3", "4/0/0", "10483/0/0", "10485/0/0", "10486/0/0", "33000/0/0")
    foreach ($wPath in $warmupPaths) {
        Start-Sleep -Milliseconds 2000
        try {
            $null = Invoke-WebRequest -Uri "$base/${wPath}?timeout=$CoapTimeoutSec" -UseBasicParsing -TimeoutSec 15
            Write-Host "  Warmup OK: $wPath" -ForegroundColor DarkGray
        }
        catch {
            Write-Host "  Warmup skip: $wPath" -ForegroundColor DarkGray
        }
    }
    Write-Host "  Warmup complete" -ForegroundColor Magenta
    Start-Sleep -Milliseconds 3000
}

# CSV header
$csvRows = @()
$csvRows += "Timestamp,Round,SeqNum,Object,ObjectName,Instance,Resource,ResourceLabel,DelayMs,LatencyMs,Status,Value,Error"

$globalSeq = 0
$totalSuccess = 0
$totalFail = 0
$totalRetried = 0

# Helper function for a single read attempt
function DoRead($uri, $timeoutSec) {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $result = @{ Ok = $false; Elapsed = 0; Status = ""; Val = ""; ErrMsg = ""; HttpCode = "" }
    try {
        $r = Invoke-WebRequest -Uri $uri -UseBasicParsing -TimeoutSec $timeoutSec
        $sw.Stop()
        $result.Elapsed = $sw.ElapsedMilliseconds
        $json = $r.Content | ConvertFrom-Json
        $result.Status = $json.status
        $result.Ok = $true
        $v = ""
        if ($json.content.value -ne $null) {
            $v = "$($json.content.value)" -replace ',', ';' -replace '"', "'"
            if ($v.Length -gt 60) { $v = $v.Substring(0, 57) + "..." }
        }
        elseif ($json.content.values -ne $null) {
            $v = "(multi)"
        }
        $result.Val = $v
    }
    catch {
        $sw.Stop()
        $result.Elapsed = $sw.ElapsedMilliseconds
        $msg = ($_.Exception.Message -replace ',', ';' -replace '"', "'")
        if ($msg.Length -gt 100) { $msg = $msg.Substring(0, 100) }
        $result.ErrMsg = $msg
        $code = ""
        if ($msg -match "\((\d+)\)") { $code = $Matches[1] }
        $result.HttpCode = $code
    }
    return $result
}

# Main loop
for ($round = 1; $round -le $Rounds; $round++) {
    $roundStart = Get-Date
    Write-Host ""
    Write-Host "-- Round $round/$Rounds --" -ForegroundColor Yellow

    $resIdx = 0
    foreach ($res in $resources) {
        $obj = $res[0]; $inst = $res[1]; $rid = $res[2]
        $objName = $res[3]; $resLabel = $res[4]
        $uri = "$base/$obj/$inst/${rid}?timeout=$CoapTimeoutSec"
        $globalSeq++
        $resIdx++

        $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss.fff"
        Start-Sleep -Milliseconds $DelayMs

        $readResult = DoRead $uri $TimeoutSec

        # Retry logic
        if ((-not $readResult.Ok) -and ($MaxRetries -gt 0)) {
            for ($retry = 1; $retry -le $MaxRetries; $retry++) {
                Write-Host ("  [{0,3}/{1}] RETRY #{2} after {3}ms..." -f $resIdx, $resources.Count, $retry, $RetryDelayMs) -ForegroundColor DarkYellow
                Start-Sleep -Milliseconds $RetryDelayMs
                $readResult = DoRead $uri $TimeoutSec
                if ($readResult.Ok) { break }
            }
        }

        $elapsed = $readResult.Elapsed

        if ($readResult.Ok) {
            $color = "Green"
            if ($elapsed -ge 500)  { $color = "Yellow" }
            if ($elapsed -ge 3000) { $color = "Red" }

            $tag = ""
            if ($totalRetried -ne $totalRetried) { $tag = "" }  # placeholder
            Write-Host ("  [{0,3}/{1}] {2,8}ms {3,-20} {4}" -f $resIdx, $resources.Count, $elapsed, "$objName/$resLabel", $readResult.Status) -ForegroundColor $color
            $totalSuccess++

            $csvRows += "$ts,$round,$globalSeq,$obj,$objName,$inst,$rid,$resLabel,$DelayMs,$elapsed,$($readResult.Status),`"$($readResult.Val)`","
        }
        else {
            Write-Host ("  [{0,3}/{1}] {2,8}ms {3,-20} FAIL({4})" -f $resIdx, $resources.Count, $elapsed, "$objName/$resLabel", $readResult.HttpCode) -ForegroundColor Red
            $totalFail++

            $csvRows += "$ts,$round,$globalSeq,$obj,$objName,$inst,$rid,$resLabel,$DelayMs,$elapsed,FAIL($($readResult.HttpCode)),`"`",`"$($readResult.ErrMsg)`""
        }
    }

    $roundElapsed = ((Get-Date) - $roundStart).TotalSeconds
    Write-Host ("  Round $round complete: {0:N1}s" -f $roundElapsed) -ForegroundColor DarkGray
}

# Write CSV
$csvRows | Out-File -FilePath $csvFile -Encoding UTF8
Write-Host ""
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  TEST SUITE COMPLETE" -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  Total requests : $($totalSuccess + $totalFail)"
Write-Host "  Successful     : $totalSuccess" -ForegroundColor Green
$failColor = "Green"
if ($totalFail -gt 0) { $failColor = "Red" }
Write-Host "  Failed         : $totalFail" -ForegroundColor $failColor
Write-Host "  CSV saved to   : $csvFile" -ForegroundColor White
Write-Host "  Finished       : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next step: Generate graphs with:" -ForegroundColor Yellow
Write-Host "  python tools\graph_latency.py `"$csvFile`"" -ForegroundColor Yellow
