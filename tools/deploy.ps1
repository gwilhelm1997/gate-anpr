<#
.SYNOPSIS
  Copies the Gate ANPR app to Home Assistant's local apps folder over Samba.
.DESCRIPTION
  Needs the Samba share app running in Home Assistant. You are asked for the Samba user's
  password if the share is not connected yet (nothing is stored by this script).
  Afterwards: Settings > Apps > Install app > menu > Check for updates, then Install / Update / Rebuild.
#>
param(
    [string]$HomeAssistant = "homeassistant",
    [string]$User = "homeassistant"
)
$ErrorActionPreference = "Stop"
$share = "\\$HomeAssistant\addons"
$source = Join-Path $PSScriptRoot "..\gate_anpr"

if (-not (Test-Path $share)) {
    # Prompts for the Samba password (Samba share app configuration); Windows keeps the session only.
    net use $share /user:$User
    if ($LASTEXITCODE -ne 0) { throw "Could not connect to $share" }
}

$target = Join-Path $share "gate_anpr"
robocopy $source $target /MIR /XD __pycache__ /XF *.pyc /NFL /NDL /NJH /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed with exit code $LASTEXITCODE" }

$version = (Select-String -Path (Join-Path $target "config.yaml") -Pattern '^version:\s*"?([^"]+)"?').Matches[0].Groups[1].Value
Write-Host "Copied Gate ANPR $version to $target"
Write-Host "Now: Settings > Apps > Install app > (menu) Check for updates, then Install, Update or Rebuild."
