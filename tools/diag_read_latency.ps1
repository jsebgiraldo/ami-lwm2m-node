<#
.SYNOPSIS
    LwM2M Read Latency Diagnostic — measures round-trip time for each resource
.DESCRIPTION
    Reads key resources from all 10 LwM2M objects via Leshan REST API,
    measuring the elapsed time per request. Helps identify:
    - CoAP congestion over Thread (504 timeouts)
    - Slow resources (large payloads, block transfer)
    - Optimal inter-request delay
.NOTES
    Server-side: time from Leshan REST API request to response
    The CoAP RTT over Thread is included in this measurement.
    Typical path: PowerShell → HTTP → Leshan → CoAP → Thread → ESP32 → back
#>

param(
    [string]$Endpoint = "ami-esp32c6-25c0",
    [string]$Server   = "http://192.168.1.111:18080",
    [int]$DelayMs      = 3000,      # ms between requests (avoid CoAP congestion)
    [int]$TimeoutSec   = 30
)

$base = "$Server/api/clients/$Endpoint"

# Define resources to test: [Object, Instance, Resource, Label]
$resources = @(
    @(1, 0, 1, "Server/Lifetime"),
    @(3, 0, 0, "Device/Manufacturer"),
    @(3, 0, 3, "Device/FWVersion"),
    @(4, 0, 0, "ConnMon/Bearer"),
    @(4, 0, 2, "ConnMon/RSSI"),
    @(4, 0, 3, "ConnMon/LinkQuality"),
    @(4, 0, 4, "ConnMon/IPAddresses"),
    @(10242, 0, 0, "PowerMeter/voltage_a"),
    @(10483, 0, 0, "ThreadNet/NetworkName"),
    @(10483, 0, 2, "ThreadNet/ExtPanId"),
    @(10483, 0, 5, "ThreadNet/RLOC16"),
    @(10483, 0, 11, "ThreadNet/IPv6Addrs"),
    @(10484, 0, 0, "Commission/JoinerEUI"),
    @(10485, 0, 0, "Neighbor/Role"),
    @(10485, 0, 1, "Neighbor/RLOC16"),
    @(10485, 0, 3, "Neighbor/AvgRSSI"),
    @(10485, 0, 8, "Neighbor/ExtMAC"),
    @(10486, 0, 0, "CLI/Version"),
    @(10486, 0, 3, "CLI/Result"),
    @(33000, 0, 0, "ThreadDiag/Role"),
    @(33000, 0, 1, "ThreadDiag/PartitionID"),
    @(33000, 0, 2, "ThreadDiag/TxTotal")
)

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  LwM2M Read Latency Diagnostic" -ForegroundColor Cyan
Write-Host "  Endpoint : $Endpoint" -ForegroundColor Cyan
Write-Host "  Server   : $Server" -ForegroundColor Cyan
Write-Host "  Delay    : ${DelayMs}ms between requests" -ForegroundColor Cyan
Write-Host "  Timeout  : ${TimeoutSec}s per request" -ForegroundColor Cyan
Write-Host "  Resources: $($resources.Count)" -ForegroundColor Cyan
Write-Host "  Date     : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# Check registration first
try {
    $clients = (Invoke-WebRequest -Uri "$Server/api/clients" -UseBasicParsing -TimeoutSec 10).Content | ConvertFrom-Json
    $found = $clients | Where-Object { $_.endpoint -eq $Endpoint }
    if (-not $found) {
        Write-Host "ERROR: Endpoint '$Endpoint' not registered!" -ForegroundColor Red
        exit 1
    }
    Write-Host "Device registered. Last update: $($found.lastUpdate)" -ForegroundColor Green
    Write-Host ""
} catch {
    Write-Host "ERROR: Cannot reach Leshan at $Server" -ForegroundColor Red
    exit 1
}

# Results collection
$results = @()
$totalSuccess = 0
$totalFail = 0
$totalTime = 0

Write-Host ("{0,-30} {1,8} {2,8} {3}" -f "Resource", "Time(ms)", "Status", "Value") -ForegroundColor Yellow
Write-Host ("{0,-30} {1,8} {2,8} {3}" -f "--------", "--------", "------", "-----") -ForegroundColor Yellow

foreach ($res in $resources) {
    $obj = $res[0]; $inst = $res[1]; $rid = $res[2]; $label = $res[3]
    $uri = "$base/$obj/$inst/$rid"
    $path = "/$obj/$inst/$rid"

    Start-Sleep -Milliseconds $DelayMs

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $r = Invoke-WebRequest -Uri $uri -UseBasicParsing -TimeoutSec $TimeoutSec
        $sw.Stop()
        $elapsed = $sw.ElapsedMilliseconds

        $json = $r.Content | ConvertFrom-Json
        $status = $json.status
        $val = ""

        if ($json.content.value -ne $null) {
            $val = "$($json.content.value)"
            if ($val.Length -gt 40) { $val = $val.Substring(0, 37) + "..." }
        } elseif ($json.content.values -ne $null) {
            $val = "(multi-instance)"
        }

        $color = if ($elapsed -lt 3000) { "Green" }
                 elseif ($elapsed -lt 8000) { "Yellow" }
                 else { "Red" }

        Write-Host ("{0,-30} {1,8} {2,8} {3}" -f $label, $elapsed, $status, $val) -ForegroundColor $color
        $totalSuccess++
        $totalTime += $elapsed

        $results += [PSCustomObject]@{
            Path    = $path
            Label   = $label
            TimeMs  = $elapsed
            Status  = $status
            Value   = $val
            Error   = $null
        }
    } catch {
        $sw.Stop()
        $elapsed = $sw.ElapsedMilliseconds
        $errMsg = $_.Exception.Message
        $httpCode = ""
        if ($errMsg -match "\((\d+)\)") { $httpCode = $Matches[1] }

        Write-Host ("{0,-30} {1,8} {2,8} {3}" -f $label, $elapsed, "FAIL", $httpCode) -ForegroundColor Red
        $totalFail++
        $totalTime += $elapsed

        $results += [PSCustomObject]@{
            Path    = $path
            Label   = $label
            TimeMs  = $elapsed
            Status  = "FAIL($httpCode)"
            Value   = $null
            Error   = $errMsg
        }
    }
}

# Summary
Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  SUMMARY" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Total requests : $($results.Count)"
Write-Host "  Successful     : $totalSuccess" -ForegroundColor Green
Write-Host "  Failed (504/err): $totalFail" -ForegroundColor $(if ($totalFail -gt 0) { "Red" } else { "Green" })
Write-Host ""

$successResults = $results | Where-Object { $_.Status -ne $null -and $_.Status -notlike "FAIL*" }
if ($successResults.Count -gt 0) {
    $times = $successResults | ForEach-Object { $_.TimeMs }
    $avg = [math]::Round(($times | Measure-Object -Average).Average, 0)
    $min = ($times | Measure-Object -Minimum).Minimum
    $max = ($times | Measure-Object -Maximum).Maximum
    $median = ($times | Sort-Object)[([math]::Floor($times.Count / 2))]
    $p90 = ($times | Sort-Object)[([math]::Floor($times.Count * 0.9))]

    Write-Host "  Latency Statistics (successful reads):" -ForegroundColor Yellow
    Write-Host "    Min     : ${min} ms"
    Write-Host "    Max     : ${max} ms"
    Write-Host "    Average : ${avg} ms"
    Write-Host "    Median  : ${median} ms"
    Write-Host "    P90     : ${p90} ms"
    Write-Host "    Total   : ${totalTime} ms ($([math]::Round($totalTime/1000, 1))s)"
    Write-Host ""

    # Slowest resources
    Write-Host "  Slowest Resources:" -ForegroundColor Yellow
    $successResults | Sort-Object TimeMs -Descending | Select-Object -First 5 | ForEach-Object {
        $color = if ($_.TimeMs -gt 8000) { "Red" } elseif ($_.TimeMs -gt 3000) { "Yellow" } else { "White" }
        Write-Host "    $($_.TimeMs)ms  $($_.Label) ($($_.Path))" -ForegroundColor $color
    }
    Write-Host ""

    # Fastest resources
    Write-Host "  Fastest Resources:" -ForegroundColor Yellow
    $successResults | Sort-Object TimeMs | Select-Object -First 5 | ForEach-Object {
        Write-Host "    $($_.TimeMs)ms  $($_.Label) ($($_.Path))" -ForegroundColor Green
    }
}

if ($totalFail -gt 0) {
    Write-Host ""
    Write-Host "  Failed Resources:" -ForegroundColor Red
    $results | Where-Object { $_.Status -like "FAIL*" } | ForEach-Object {
        Write-Host "    $($_.TimeMs)ms  $($_.Label) ($($_.Path)) - $($_.Status)" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  Diagnosis complete." -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
