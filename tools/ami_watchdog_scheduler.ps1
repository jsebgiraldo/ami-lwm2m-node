# AMI Watchdog Scheduler - Windows Task Scheduler wrapper
# Manages automated health checks for the AMI ESP32-C6 node and TB Edge
[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$RunOnce,
    [switch]$Daemon,
    [switch]$Status,
    [switch]$DryRun,
    [int]$IntervalMinutes = 5,
    [int]$DurationDays = 1
)

$ErrorActionPreference = "Stop"

$TaskName  = "AMI_Watchdog"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WatchdogScript = Join-Path $ScriptDir "ami_watchdog.py"
$PythonPath = "python"
$LogDir = Join-Path (Join-Path (Split-Path -Parent $ScriptDir) "results") "watchdog"

# ---------------------------------------------------------------
# Helper: Check prerequisites
# ---------------------------------------------------------------
function Test-Prerequisites {
    try {
        $ver = & $PythonPath --version 2>&1
        Write-Host "  [OK] Python: $ver" -ForegroundColor Green
    }
    catch {
        Write-Host "  [FAIL] Python not found" -ForegroundColor Red
        return $false
    }

    $reqCheck = & $PythonPath -c "import requests; print(requests.__version__)" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [WARN] requests not installed. Installing..." -ForegroundColor Yellow
        & $PythonPath -m pip install requests --quiet
    }
    else {
        Write-Host "  [OK] requests: $reqCheck" -ForegroundColor Green
    }

    $serCheck = & $PythonPath -c "import serial; print(serial.VERSION)" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [WARN] pyserial not installed (optional for HW reset)" -ForegroundColor Yellow
    }
    else {
        Write-Host "  [OK] pyserial: $serCheck" -ForegroundColor Green
    }

    if (Test-Path $WatchdogScript) {
        Write-Host "  [OK] Watchdog: $WatchdogScript" -ForegroundColor Green
    }
    else {
        Write-Host "  [FAIL] Watchdog not found: $WatchdogScript" -ForegroundColor Red
        return $false
    }

    $sshTest = Get-Command ssh -ErrorAction SilentlyContinue
    if ($sshTest) {
        Write-Host "  [OK] SSH available" -ForegroundColor Green
    }
    else {
        Write-Host "  [WARN] SSH not available" -ForegroundColor Yellow
    }

    return $true
}

# ---------------------------------------------------------------
# Action: Install as Windows Scheduled Task
# ---------------------------------------------------------------
function Install-WatchdogTask {
    Write-Host ""
    Write-Host "=== Installing AMI Watchdog Scheduled Task ===" -ForegroundColor Cyan
    Write-Host "Prerequisites:" -ForegroundColor Cyan

    if (-not (Test-Prerequisites)) {
        Write-Host "Fix issues above and retry." -ForegroundColor Red
        return
    }

    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "Removing existing task..." -ForegroundColor Yellow
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }

    $psArgs = "-NoProfile -ExecutionPolicy Bypass -File ""$($ScriptDir)\ami_watchdog_scheduler.ps1"" -RunOnce"

    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $psArgs -WorkingDirectory $ScriptDir

    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) -RepetitionDuration (New-TimeSpan -Days $DurationDays)

    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "AMI Watchdog - Monitors ESP32 node and TB Edge" -Force

    Write-Host ""
    Write-Host "=== Installed ===" -ForegroundColor Green
    Write-Host "  Task: $TaskName" -ForegroundColor Green
    Write-Host "  Interval: every $IntervalMinutes min" -ForegroundColor Green
    Write-Host "  Duration: $DurationDays day(s)" -ForegroundColor Green
    Write-Host "  Expires: $((Get-Date).AddDays($DurationDays).ToString('yyyy-MM-dd HH:mm'))" -ForegroundColor Green
    Write-Host "  Logs: $LogDir" -ForegroundColor Green
}

# ---------------------------------------------------------------
# Action: Uninstall scheduled task
# ---------------------------------------------------------------
function Uninstall-WatchdogTask {
    Write-Host ""
    Write-Host "=== Removing AMI Watchdog Scheduled Task ===" -ForegroundColor Cyan
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "  Task removed." -ForegroundColor Green
    }
    else {
        Write-Host "  Task not found." -ForegroundColor Yellow
    }
}

# ---------------------------------------------------------------
# Action: Show status
# ---------------------------------------------------------------
function Show-WatchdogStatus {
    Write-Host ""
    Write-Host "=== AMI Watchdog Status ===" -ForegroundColor Cyan

    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        $info = $task | Get-ScheduledTaskInfo
        $sc = if ($task.State -eq "Ready") { "Green" } else { "Yellow" }
        Write-Host "  Scheduled Task: $($task.State)" -ForegroundColor $sc
        Write-Host "  Last Run: $($info.LastRunTime)"
        Write-Host "  Next Run: $($info.NextRunTime)"
        Write-Host "  Last Result: $($info.LastTaskResult)"
    }
    else {
        Write-Host "  Scheduled Task: NOT INSTALLED" -ForegroundColor Yellow
    }

    $latestReport = Join-Path (Join-Path $LogDir "reports") "latest.json"
    if (Test-Path $latestReport) {
        $report = Get-Content $latestReport | ConvertFrom-Json
        Write-Host ""
        Write-Host "  Latest Health Check:" -ForegroundColor Cyan
        Write-Host "    Time:    $($report.timestamp)"

        $oc = switch ($report.overall) {
            "OK"       { "Green" }
            "WARNING"  { "Yellow" }
            "CRITICAL" { "Red" }
            default    { "White" }
        }
        Write-Host "    Overall: $($report.overall)" -ForegroundColor $oc

        foreach ($chk in $report.checks) {
            $cc = switch ($chk.status) {
                "OK"       { "Green" }
                "WARNING"  { "Yellow" }
                "CRITICAL" { "Red" }
                default    { "White" }
            }
            $icon = switch ($chk.status) {
                "OK"       { "[OK]" }
                "WARNING"  { "[!!]" }
                "CRITICAL" { "[XX]" }
                default    { "[??]" }
            }
            Write-Host "    $icon $($chk.name): $($chk.message)" -ForegroundColor $cc
        }

        if ($report.recovery_needed) {
            Write-Host ""
            Write-Host "    *** Recovery Actions ***" -ForegroundColor Red
            foreach ($act in $report.recovery_actions) {
                Write-Host "      -> $act" -ForegroundColor Red
            }
        }
    }
    else {
        Write-Host "  No reports yet. Run -RunOnce first." -ForegroundColor Yellow
    }

    $stateFile = Join-Path $LogDir "watchdog_recovery_state.json"
    if (Test-Path $stateFile) {
        $state = Get-Content $stateFile | ConvertFrom-Json
        Write-Host ""
        Write-Host "  Recovery State:" -ForegroundColor Cyan
        Write-Host "    Escalation Level: $($state.escalation_level)"
        Write-Host "    Recovery Count: $($state.recovery_count)"
        if ($state.last_recovery_action) {
            Write-Host "    Last Action: $($state.last_recovery_action.time) (Level $($state.last_recovery_action.level))"
        }
    }

    $csvFile = Join-Path $LogDir ("health_" + (Get-Date -Format "yyyyMMdd") + ".csv")
    if (Test-Path $csvFile) {
        $lines = (Get-Content $csvFile | Measure-Object -Line).Lines - 1
        Write-Host ""
        Write-Host "  Checks today: $lines" -ForegroundColor Cyan
    }

    Write-Host ""
    Write-Host "  Log Dir: $LogDir"
}

# ---------------------------------------------------------------
# Action: Single health check
# ---------------------------------------------------------------
function Invoke-SingleCheck {
    $extraArgs = @()
    if ($DryRun) { $extraArgs += "--dry-run" }
    Write-Host "Running AMI health check..." -ForegroundColor Cyan
    & $PythonPath $WatchdogScript @extraArgs
}

# ---------------------------------------------------------------
# Action: Daemon mode
# ---------------------------------------------------------------
function Start-DaemonMode {
    $extraArgs = @("--daemon", "--interval", ($IntervalMinutes * 60))
    if ($DryRun) { $extraArgs += "--dry-run" }
    Write-Host "Starting AMI Watchdog daemon (Ctrl+C to stop)..." -ForegroundColor Cyan
    & $PythonPath $WatchdogScript @extraArgs
}

# ---------------------------------------------------------------
# Main dispatch
# ---------------------------------------------------------------
if ($Install)       { Install-WatchdogTask }
elseif ($Uninstall) { Uninstall-WatchdogTask }
elseif ($Status)    { Show-WatchdogStatus }
elseif ($Daemon)    { Start-DaemonMode }
elseif ($RunOnce)   { Invoke-SingleCheck }
else {
    Write-Host ""
    Write-Host "AMI Watchdog Scheduler" -ForegroundColor Cyan
    Write-Host "======================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Usage:"
    Write-Host "  .\ami_watchdog_scheduler.ps1 -Install              Install as Windows task (every 5min)"
    Write-Host "  .\ami_watchdog_scheduler.ps1 -Install -IntervalMinutes 3  Custom interval"
    Write-Host "  .\ami_watchdog_scheduler.ps1 -RunOnce              Single health check"
    Write-Host "  .\ami_watchdog_scheduler.ps1 -RunOnce -DryRun      Check only, no recovery"
    Write-Host "  .\ami_watchdog_scheduler.ps1 -Daemon               Foreground daemon mode"
    Write-Host "  .\ami_watchdog_scheduler.ps1 -Status               Show current status"
    Write-Host "  .\ami_watchdog_scheduler.ps1 -Uninstall            Remove scheduled task"
    Write-Host ""
}
