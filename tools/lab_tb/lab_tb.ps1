#Requires -Version 7.0
<#
.SYNOPSIS
  Windows-side driver for the BENCH ThingsBoard CE LwM2M server that runs inside
  WSL2 next to the native otbr-agent. Thin wrapper around tools/lab_tb/lab_tb_up.sh.

.NOTES
  RUN WITH pwsh 7, NOT Windows PowerShell 5.1. This file is UTF-8 without BOM and
  contains non-ASCII characters (em dashes, arrows); PS 5.1 decodes BOM-less files
  as ANSI, which corrupts those bytes mid-string and produces a bogus
  "The string is missing the terminator" ParserError. pwsh 7 defaults to UTF-8.
  Equivalent fallback that bypasses PowerShell entirely:
    wsl -d Ubuntu-24.04 -- bash tools/lab_tb/lab_tb_up.sh <action>

.DESCRIPTION
  WHY: the lab node discovers its LwM2M server only via DNS-SD over Thread and
  then REGISTERs. With no server, overlays/lab.conf must disable the
  boot-register deadline and stretch the HW watchdog — so the bench validates a
  configuration we never ship. This stack gives the bench a real ThingsBoard
  (same product as the fleet's TB Edge), bound on WSL's wpan0 OMR address.

  Everything actually happens inside WSL as root (docker.sock and `ss -p` both
  require it). This script only translates paths, strips CRLF from the shell
  script, and adds the Windows-side reachability check that WSL cannot do.

  Actions (-Action):
    bootstrap  FIRST RUN: preflight -> pull -> install -> up -> verify
    preflight  docker/RAM/free-ports/wpan0 checks (safe, changes nothing)
    pull       docker compose pull
    install    ONE-TIME DB schema + demo data (creates tenant@thingsboard.org)
    up         start the stack, block until it answers
    verify     acceptance gate (UDP bind family, Leshan log line, REST login)
    status     containers + sockets + wpan0 addresses
    logs       tail TB's log            (-Follow to stream)
    restart    restart TB only — REQUIRED after every LwM2M model upload
    down       stop, keep the database
    reset      stop and DELETE the database  (-Yes required)
    srpinfo    print the SRP host/service records the node needs
    url        print/verify the Windows-visible URLs

.EXAMPLE
  ./tools/lab_tb/lab_tb.ps1 -Action bootstrap

.EXAMPLE
  ./tools/lab_tb/lab_tb.ps1 -Action verify

.NOTES
  Nothing here touches a COM port, the RCP dongle, or the node.
  Runbook + troubleshooting: tools/lab_tb/README.md
#>
[CmdletBinding()]
param(
    [ValidateSet('bootstrap','preflight','pull','install','up','verify','status','logs','restart','down','reset','srpinfo','url')]
    [string]$Action = 'status',
    [string]$Distro = 'Ubuntu-24.04',
    [int]$HttpPort  = 8080,
    [switch]$Follow,
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'

function Require-Wsl {
    if (-not (Get-Command wsl -ErrorAction SilentlyContinue)) {
        throw "'wsl' not found on PATH."
    }
    $distros = (wsl -l -q) -replace "`0", '' | ForEach-Object { $_.Trim() } | Where-Object { $_ }
    if ($distros -notcontains $Distro) {
        throw "WSL distro '$Distro' not found. Available: $($distros -join ', ')"
    }
}

# Convert this script's directory to its /mnt/c/... form inside WSL.
function Get-WslDir {
    $win = ($PSScriptRoot -replace '\\', '/')
    $p = (wsl -d $Distro -- wslpath -a "$win") 2>$null
    if (-not $p) { throw "wslpath failed for '$win'." }
    return ($p -replace "`0", '').Trim()
}

# Run lab_tb_up.sh inside WSL as root.
#   - piped through sed to strip CRLF, so a Windows checkout never breaks bash
#   - LAB_TB_DIR tells the script where the compose file lives
function Invoke-LabTb([string]$act, [hashtable]$vars = @{}) {
    $dir = Get-WslDir
    $prefix = ''
    foreach ($k in $vars.Keys) { $prefix += "$k='$($vars[$k])' " }
    $inner = "cd '$dir' && sed 's/\r`$//' lab_tb_up.sh > /tmp/lab_tb_up.sh && ${prefix}LAB_TB_DIR='$dir' bash /tmp/lab_tb_up.sh $act"
    # Out-Host keeps the script's console output on screen; only the exit code
    # travels back through the pipeline.
    wsl -d $Distro -u root -- bash -lc $inner | Out-Host
    return $LASTEXITCODE
}

function Show-Urls {
    Write-Host "== Windows-visible endpoints ==" -ForegroundColor Cyan
    $wslIp = ((wsl -d $Distro -- hostname -I) -replace "`0", '').Trim().Split(' ')[0]
    Write-Host "  UI / REST : http://localhost:$HttpPort        (WSL2 localhostForwarding)"
    if ($wslIp) { Write-Host "  fallback  : http://${wslIp}:$HttpPort   (use this if localhost is refused)" }
    Write-Host "  login     : tenant@thingsboard.org / tenant"
    Write-Host ""
    foreach ($u in @("http://localhost:$HttpPort/login", $(if ($wslIp) { "http://${wslIp}:$HttpPort/login" }))) {
        if (-not $u) { continue }
        try {
            $r = Invoke-WebRequest -Uri $u -TimeoutSec 8 -UseBasicParsing -ErrorAction Stop
            Write-Host "  [ OK ] $u -> HTTP $($r.StatusCode)" -ForegroundColor Green
        } catch {
            Write-Host "  [FAIL] $u -> $($_.Exception.Message)" -ForegroundColor Yellow
        }
    }
    Write-Host ""
    Write-Host "  If localhost is refused but the WSL IP works, localhostForwarding is off in" -ForegroundColor Yellow
    Write-Host "  %USERPROFILE%\.wslconfig. Point the repo tooling at the WSL IP instead." -ForegroundColor Yellow
}

Require-Wsl

switch ($Action) {

    'url' { Show-Urls }

    'logs' {
        $vars = @{}
        if ($Follow) { $vars['LAB_TB_FOLLOW'] = '1' }
        $null = Invoke-LabTb 'logs' $vars
    }

    'reset' {
        if (-not $Yes) {
            throw "reset DELETES the bench TB database. Re-run with -Yes to confirm."
        }
        $null = Invoke-LabTb 'reset' @{ 'LAB_TB_YES' = '1' }
    }

    'bootstrap' {
        Write-Host "== FIRST-RUN bootstrap: preflight -> pull -> install -> up -> verify ==" -ForegroundColor Cyan
        Write-Host "   Budget ~5-10 min (postgres pull + schema install + first JVM boot)." -ForegroundColor DarkGray
        $rc = Invoke-LabTb 'bootstrap'
        Write-Host ""
        Show-Urls
        if ($rc -ne 0) {
            Write-Warning "bootstrap returned exit code $rc — re-run './tools/lab_tb/lab_tb.ps1 -Action verify' after fixing."
        } else {
            Write-Host "Next: upload the LwM2M models, then -Action restart (Leshan loads models at startup only)." -ForegroundColor Green
        }
    }

    default {
        $rc = Invoke-LabTb $Action
        if ($Action -in @('up','verify')) { Write-Host ""; Show-Urls }
        if ($rc -ne 0) { Write-Warning "'$Action' returned exit code $rc." }
    }
}
