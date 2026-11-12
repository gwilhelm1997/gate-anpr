<#
  Gate ANPR: give the Home Assistant VirtualBox VM the host CPU's AVX2 instructions.
  Run on the PC that runs VirtualBox, in an Administrator PowerShell:
      powershell -ExecutionPolicy Bypass -File enable-avx2.ps1
  It shuts Home Assistant down cleanly, changes one VM setting, and starts it again.
#>
$ErrorActionPreference = "Stop"

# 1. Find VBoxManage (not on PATH by default).
$vbox = @("$env:ProgramFiles\Oracle\VirtualBox\VBoxManage.exe",
          "${env:ProgramFiles(x86)}\Oracle\VirtualBox\VBoxManage.exe",
          "$env:VBOX_MSI_INSTALL_PATH\VBoxManage.exe") | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $vbox) { throw "VBoxManage.exe not found. Is VirtualBox installed on this PC?" }
Write-Host "VirtualBox: $vbox"

# 2. Find the Home Assistant VM.
$vms = & $vbox list vms | ForEach-Object { if ($_ -match '^"(.+)"\s+\{') { $Matches[1] } }
if (-not $vms) { throw "No VirtualBox VMs found for this Windows user. Run as the user that owns the VM." }
$vm = $vms | Where-Object { $_ -match 'home ?assistant|haos|hass' } | Select-Object -First 1
if (-not $vm) {
    Write-Host "VMs found:"; $vms | ForEach-Object { Write-Host "  $_" }
    $vm = Read-Host "Type the Home Assistant VM name exactly"
}
Write-Host "Home Assistant VM: $vm"

# 3. Current CPU profile.
$info = & $vbox showvminfo $vm --machinereadable
$profile = ($info | Where-Object { $_ -like 'cpu-profile=*' }) -replace '^cpu-profile="?|"$', ''
Write-Host "Current CPU profile: $(if ($profile) { $profile } else { 'default' })"

# 4. Shut down cleanly if running (ACPI power button = graceful HA shutdown).
$running = (& $vbox list runningvms) -match [regex]::Escape("`"$vm`"")
if ($running) {
    Write-Host "Shutting down Home Assistant cleanly (up to 3 minutes)..."
    & $vbox controlvm $vm acpipowerbutton
    $deadline = (Get-Date).AddMinutes(3)
    while (((& $vbox list runningvms) -match [regex]::Escape("`"$vm`"")) -and (Get-Date) -lt $deadline) { Start-Sleep 5 }
    if ((& $vbox list runningvms) -match [regex]::Escape("`"$vm`"")) {
        throw "Home Assistant did not shut down within 3 minutes. Shut it down from HA (Settings > System > power), then run this again."
    }
    Start-Sleep 3
}

# 5. The fix: expose the host CPU (including AVX2) to the VM.
& $vbox modifyvm $vm --cpu-profile host
Write-Host "CPU profile set to: host"

# 6. Start Home Assistant again (headless = no window; it runs as before).
& $vbox startvm $vm --type headless
Write-Host "Home Assistant is starting. Give it 2-3 minutes."

# 7. Can VirtualBox actually pass AVX2 through? Not if it runs on top of Hyper-V.
$hv = (Get-CimInstance Win32_ComputerSystem).HypervisorPresent
$hvci = $null
try { $hvci = (Get-CimInstance -Namespace root\Microsoft\Windows\DeviceGuard -ClassName Win32_DeviceGuard).SecurityServicesRunning -contains 2 } catch {}
Write-Host ""
if ($hv) {
    Write-Warning "Windows is running a hypervisor (Hyper-V$(if ($hvci) { ' / Memory integrity' })). VirtualBox then runs on top of it and usually CANNOT pass AVX2 through."
    Write-Host "If Gate ANPR still shows avx2: false after this, you can turn these off (reboot needed). It lowers Windows' protection:"
    Write-Host "  - Windows Security > Device security > Core isolation > Memory integrity: Off"
    Write-Host "  - Turn Windows features on or off: untick Hyper-V, Virtual Machine Platform, Windows Hypervisor Platform"
} else {
    Write-Host "No Hyper-V layer detected: AVX2 should now reach Home Assistant."
}
