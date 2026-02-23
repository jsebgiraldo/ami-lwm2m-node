<#
.SYNOPSIS
    LwM2M Read Latency Test Suite — Collects multi-round timing data for thesis analysis
.DESCRIPTION
    Runs N rounds of individual resource reads across all LwM2M objects,
    testing with configurable inter-request delays. All raw data is exported
    to CSV for post-processing with Python/matplotlib.

    Output CSV columns:
      Timestamp, Round, SeqNum, Object, ObjectName, Instance, Resource,
      ResourceLabel, DelayMs, LatencyMs, Status, Value, Error

.PARAMETER Rounds
    Number of complete read cycles to perform (default: 10)
.PARAMETER DelayMs
    Milliseconds between requests (default: 3000)
.PARAMETER OutputDir
    Directory for CSV output (default: ./results)
.EXAMPLE
    .\test_suite_latency.ps1 -Rounds 10 -DelayMs 3000
    .\test_suite_latency.ps1 -Rounds 5 -DelayMs 1000   # stress test
#>

param(
    [string]$Endpoint  = "ami-esp32c6-25c0",
    [string]$Server    = "http://192.168.1.111:18080",
    [int]$Rounds       = 10,
    [int]$DelayMs      = 3000,
    [int]$TimeoutSec   = 30,
    [string]$OutputDir = ".\results"
)

$base = "$Server/api/clients/$Endpoint"

# ── Resource definitions: Object, Instance, Resource, ObjectName, ResourceLabel ──
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

# ── Setup ──
if (-not (Test-Path $OutputDir)) { New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null }

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$csvFile   = Join-Path $OutputDir "latency_${timestamp}_delay${DelayMs}ms_${Rounds}rounds.csv"

Write-Host ""
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  LwM2M Read Latency Test Suite" -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  Endpoint  : $Endpoint"
Write-Host "  Server    : $Server"
Write-Host "  Rounds    : $Rounds"
Write-Host "  Delay     : ${DelayMs}ms"
Write-Host "  Resources : $($resources.Count) per round"
Write-Host "  Total Reqs: $($resources.Count * $Rounds)"
Write-Host "  Est. Time : $([math]::Round(($resources.Count * $Rounds * ($DelayMs + 100)) / 60000, 1)) min"
Write-Host "  Output    : $csvFile"
Write-Host "  Started   : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host ""

# ── Verify device registration ──
try {
    $clients = (Invoke-WebRequest -Uri "$Server/api/clients" -UseBasicParsing -TimeoutSec 10).Content | ConvertFrom-Json
    $found = $clients | Where-Object { $_.endpoint -eq $Endpoint }
    if (-not $found) {
        Write-Host "ERROR: Endpoint '$Endpoint' not registered!" -ForegroundColor Red
        exit 1
    }
    Write-Host "Device registered. Last update: $($found.lastUpdate)" -ForegroundColor Green
} catch {
    Write-Host "ERROR: Cannot reach Leshan at $Server" -ForegroundColor Red
    exit 1
}

# ── CSV header ──
$csvRows = @()
$csvRows += "Timestamp,Round,SeqNum,Object,ObjectName,Instance,Resource,ResourceLabel,DelayMs,LatencyMs,Status,Value,Error"

$globalSeq = 0
$totalSuccess = 0
$totalFail = 0

# ── Main loop ──
for ($round = 1; $round -le $Rounds; $round++) {
    $roundStart = Get-Date
    Write-Host ""
    Write-Host "── Round $round/$Rounds ──────────────────────────────────────" -ForegroundColor Yellow

    $resIdx = 0
    foreach ($res in $resources) {
        $obj = $res[0]; $inst = $res[1]; $rid = $res[2]
        $objName = $res[3]; $resLabel = $res[4]
        $uri = "$base/$obj/$inst/$rid"
        $globalSeq++
        $resIdx++

        Start-Sleep -Milliseconds $DelayMs

        $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss.fff"
        $sw = [System.Diagnostics.Stopwatch]::StartNew()

        try {
            $r = Invoke-WebRequest -Uri $uri -UseBasicParsing -TimeoutSec $TimeoutSec
            $sw.Stop()
            $elapsed = $sw.ElapsedMilliseconds

            $json = $r.Content | ConvertFrom-Json
            $status = $json.status
            $val = ""
            if ($json.content.value -ne $null) {
                $val = "$($json.content.value)" -replace ',', ';' -replace '"', "'"
                if ($val.Length -gt 60) { $val = $val.Substring(0, 57) + "..." }
            } elseif ($json.content.values -ne $null) {
                $val = "(multi)"
            }

            $color = if ($elapsed -lt 500) { "Green" }
                     elseif ($elapsed -lt 3000) { "Yellow" }
                     else { "Red" }

            Write-Host ("  [{0,3}/{1}] {2,8}ms {3,-20} {4}" -f $resIdx, $resources.Count, $elapsed, "$objName/$resLabel", $status) -ForegroundColor $color
            $totalSuccess++

            $csvRows += "$ts,$round,$globalSeq,$obj,$objName,$inst,$rid,$resLabel,$DelayMs,$elapsed,$status,`"$val`","
        } catch {
            $sw.Stop()
            $elapsed = $sw.ElapsedMilliseconds
            $errMsg = ($_.Exception.Message -replace ',', ';' -replace '"', "'").Substring(0, [math]::Min($_.Exception.Message.Length, 100))
            $httpCode = ""
            if ($errMsg -match "\((\d+)\)") { $httpCode = $Matches[1] }

            Write-Host ("  [{0,3}/{1}] {2,8}ms {3,-20} FAIL({4})" -f $resIdx, $resources.Count, $elapsed, "$objName/$resLabel", $httpCode) -ForegroundColor Red
            $totalFail++

            $csvRows += "$ts,$round,$globalSeq,$obj,$objName,$inst,$rid,$resLabel,$DelayMs,$elapsed,FAIL($httpCode),`"`",`"$errMsg`""
        }
    }

    $roundElapsed = ((Get-Date) - $roundStart).TotalSeconds
    Write-Host ("  Round $round complete: {0:N1}s" -f $roundElapsed) -ForegroundColor DarkGray
}

# ── Write CSV ──
$csvRows | Out-File -FilePath $csvFile -Encoding UTF8
Write-Host ""
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  TEST SUITE COMPLETE" -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  Total requests : $($totalSuccess + $totalFail)"
Write-Host "  Successful     : $totalSuccess" -ForegroundColor Green
Write-Host "  Failed         : $totalFail" -ForegroundColor $(if ($totalFail -gt 0) { "Red" } else { "Green" })
Write-Host "  CSV saved to   : $csvFile" -ForegroundColor White
Write-Host "  Finished       : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next step: Generate graphs with:" -ForegroundColor Yellow
Write-Host "  python tools\graph_latency.py `"$csvFile`"" -ForegroundColor Yellow
