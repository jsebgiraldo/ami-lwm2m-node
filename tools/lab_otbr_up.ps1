<#
.SYNOPSIS
  Windows-side helper for the LAB OpenThread Border Router bring-up.
  Thin, non-destructive wrapper around usbipd-win + WSL2 docker so the
  runbook (docs/LAB_OTBR_BRINGUP.md) stays copy-pasteable. It does NOT flash
  the dongle and does NOT form the Thread network destructively — it only
  binds/attaches the USB RCP into WSL and reports health.

.DESCRIPTION
  Actions (pass as -Action):
    list     usbipd list  — find the ZBDongle-E busid (VID:PID 10c4:ea60).
    attach   bind (if needed) + attach COM58's USB device into WSL2.
             Bind needs an elevated shell (one-time, persists). Attach must
             be re-run after every replug or `wsl --shutdown`.
    health   Is it up? -> docker ps, ot-ctl state (expect 'leader'),
             ot-ctl br omrprefix, ot-ctl netdata show.
    detach   usbipd detach (hand COM58 back to Windows).

.PARAMETER Action
  list | attach | health | detach   (default: health)

.PARAMETER Distro
  WSL distro name (default: Ubuntu-24.04).

.PARAMETER Container
  OTBR docker container name (default: otbr).

.PARAMETER VidPid
  USB VID:PID of the RCP dongle (default: 10c4:ea60 = Silabs CP210x).

.EXAMPLE
  # one-time, from an ELEVATED PowerShell:
  ./tools/lab_otbr_up.ps1 -Action attach

.EXAMPLE
  ./tools/lab_otbr_up.ps1 -Action health

.NOTES
  This script authors no hardware changes on its own beyond usbipd attach/detach.
  Flashing the dongle and forming the mesh are manual steps in the runbook.
#>
[CmdletBinding()]
param(
    [ValidateSet('list', 'attach', 'health', 'detach')]
    [string]$Action = 'health',
    [string]$Distro = 'Ubuntu-24.04',
    [string]$Container = 'otbr',
    [string]$VidPid = '10c4:ea60'
)

$ErrorActionPreference = 'Stop'

function Require-Cmd([string]$name) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        throw "'$name' not found on PATH. See docs/LAB_OTBR_BRINGUP.md prerequisites."
    }
}

function Get-DongleBusId {
    # Parse `usbipd list` for the line matching $VidPid; return its BUSID.
    $lines = usbipd list 2>$null
    foreach ($ln in $lines) {
        if ($ln -match $VidPid) {
            if ($ln -match '^\s*(\d+-\d+)\s') { return $Matches[1] }
        }
    }
    return $null
}

function Invoke-Wsl([string]$cmd) {
    # Run a command string inside the WSL distro (bash -lc).
    wsl -d $Distro -- bash -lc $cmd
}

switch ($Action) {

    'list' {
        Require-Cmd usbipd
        Write-Host "== usbipd list (look for $VidPid = ZBDongle-E) ==" -ForegroundColor Cyan
        usbipd list
        $bus = Get-DongleBusId
        if ($bus) { Write-Host "`nZBDongle-E busid = $bus" -ForegroundColor Green }
        else { Write-Warning "No device matching $VidPid found. Is it plugged in? Flashed to ot-rcp yet?" }
    }

    'attach' {
        Require-Cmd usbipd
        $bus = Get-DongleBusId
        if (-not $bus) { throw "No device matching $VidPid. Run: ./tools/lab_otbr_up.ps1 -Action list" }
        Write-Host "ZBDongle-E busid = $bus" -ForegroundColor Green

        # bind (idempotent; needs admin the first time — persists across reboots)
        Write-Host "== usbipd bind --busid $bus ==" -ForegroundColor Cyan
        try { usbipd bind --busid $bus }
        catch { Write-Warning "bind failed (already bound, or run this once from an ELEVATED shell): $_" }

        Write-Host "== usbipd attach --wsl --busid $bus ==" -ForegroundColor Cyan
        usbipd attach --wsl --busid $bus --distribution $Distro
        Write-Host "Attached. Inside WSL the RCP should be /dev/ttyUSB0:" -ForegroundColor Green
        Invoke-Wsl 'ls -l /dev/ttyUSB* 2>/dev/null || echo "  (no /dev/ttyUSB* yet — check dmesg | tail)"'
        Write-Host "`nNOTE: re-run 'attach' after any replug or 'wsl --shutdown'." -ForegroundColor Yellow
    }

    'detach' {
        Require-Cmd usbipd
        $bus = Get-DongleBusId
        if (-not $bus) { Write-Warning "No matching device to detach."; break }
        Write-Host "== usbipd detach --busid $bus ==" -ForegroundColor Cyan
        usbipd detach --busid $bus
        Write-Host "Detached — COM58 is back under Windows." -ForegroundColor Green
    }

    'health' {
        Write-Host "== Is the LAB OTBR up? ==" -ForegroundColor Cyan

        Write-Host "`n[1/4] docker ps:" -ForegroundColor Cyan
        Invoke-Wsl "docker ps --filter name=$Container --format 'table {{.Names}}\t{{.Status}}'"

        Write-Host "`n[2/4] ot-ctl state (expect 'leader'):" -ForegroundColor Cyan
        Invoke-Wsl "docker exec $Container ot-ctl state"

        Write-Host "`n[3/4] ot-ctl br omrprefix (the routable OMR /64):" -ForegroundColor Cyan
        Invoke-Wsl "docker exec $Container ot-ctl br omrprefix"

        Write-Host "`n[4/4] ot-ctl netdata show (OMR prefix must appear under Prefixes):" -ForegroundColor Cyan
        Invoke-Wsl "docker exec $Container ot-ctl netdata show"

        Write-Host "`nIf state=leader and an OMR prefix is present, the BR is up." -ForegroundColor Green
        Write-Host "Extract creds for the node:  python tools\lab_thread_creds.py --exec `"wsl -d $Distro docker exec $Container ot-ctl`"" -ForegroundColor Yellow
    }
}
