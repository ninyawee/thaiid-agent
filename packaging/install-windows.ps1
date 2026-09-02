# Windows — install thaiid-agent and start it at logon.
#
#   powershell -ExecutionPolicy Bypass -File packaging\install-windows.ps1 .\thaiid-agent.exe
#
# A per-user scheduled task rather than a Windows service: the agent serves the
# logged-in person and a service running as SYSTEM would not see their session's
# reader. No admin rights needed either.
#
# PC/SC ships with Windows — the "Smart Card" service (SCardSvr) is start-on-
# demand, so plugging a reader in is enough. Nothing to install.
#
# Uninstall:  Unregister-ScheduledTask -TaskName thaiid-agent -Confirm:$false

param(
    [Parameter(Mandatory = $true)][string]$Exe,
    # Only needed if you point a page you host yourself at the agent. The
    # built-in page on http://127.0.0.1:8765 is same-origin and always allowed.
    [string]$Origins = ''
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $Exe)) { throw "not found: $Exe" }

$dest = Join-Path $env:LOCALAPPDATA 'thaiid-agent'
New-Item -ItemType Directory -Force -Path $dest | Out-Null
$target = Join-Path $dest 'thaiid-agent.exe'
Copy-Item -Path $Exe -Destination $target -Force
Write-Host "installed to $target"

if ($Origins) {
    [Environment]::SetEnvironmentVariable('THAIID_ORIGINS', $Origins, 'User')
    Write-Host "THAIID_ORIGINS set for this user: $Origins"
}

# Prove it runs before wiring it to logon — a task that fails at logon is
# invisible, and this turns that into an error you see now.
& $target --selftest
if ($LASTEXITCODE -ne 0) { throw "selftest failed; not registering the task" }

$action  = New-ScheduledTaskAction -Execute $target
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName 'thaiid-agent' -Action $action -Trigger $trigger `
    -Settings $settings -Description 'Thai ID card reader agent' -Force | Out-Null

Start-ScheduledTask -TaskName 'thaiid-agent'
Write-Host 'registered and started. Open http://127.0.0.1:8765'
