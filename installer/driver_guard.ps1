# 404ever: keeps the "TS404 Panel" MIDI port registered.
# The port is ts404_midi.dll, listed in one of Windows' MIDI driver slots (Drivers32: midi1 ... midi9).
# Windows rewrites those slots when it reinstalls audio/MIDI devices (after a crash, a driver update, a new
# USB device...), which can drop our entry; FL Studio then has no "TS404 Panel" port and the TS404 window
# stays empty. This script puts the entry back. A scheduled task runs it at startup, at logon and every 15 min.
#   driver_guard.ps1              check, repair if needed
#   driver_guard.ps1 -Install     same + create the scheduled task
#   driver_guard.ps1 -Uninstall   remove the task and the entry
param([switch]$Install, [switch]$Uninstall)

$TaskName = '404ever MIDI port guard'
$Dll = 'ts404_midi.dll'
$Path = 'SOFTWARE\Microsoft\Windows NT\CurrentVersion\Drivers32'
$hklm = [Microsoft.Win32.RegistryKey]::OpenBaseKey('LocalMachine', 'Registry64')
$key = $hklm.OpenSubKey($Path, $true)
$slots = 9..1 | ForEach-Object { "midi$_" }   # highest first: Windows fills its own from the low end

function Find-Ours { $slots | Where-Object { $v = $key.GetValue($_); $v -and ((Split-Path $v -Leaf) -ieq $Dll) } }

if ($Uninstall) {
    schtasks.exe /Delete /TN $TaskName /F 2>$null | Out-Null
    Find-Ours | ForEach-Object { $key.DeleteValue($_) }
    exit 0
}

$ours = @(Find-Ours)
if ($ours.Count -gt 1) { $ours | Select-Object -Skip 1 | ForEach-Object { $key.DeleteValue($_) } }
if ($ours.Count -eq 0 -and (Test-Path "$env:SystemRoot\System32\$Dll")) {
    $slot = $slots | Where-Object { $null -eq $key.GetValue($_) } | Select-Object -First 1
    if (-not $slot) {
        # all taken: one wdmaud.drv entry serves every Windows MIDI device, the repeats are spare
        $dupes = @($slots | Where-Object { "$($key.GetValue($_))" -ieq 'wdmaud.drv' })
        if ("$($key.GetValue('midi'))" -ieq 'wdmaud.drv' -or $dupes.Count -gt 1) { $slot = $dupes | Select-Object -First 1 }
    }
    if ($slot) {
        $key.SetValue($slot, $Dll, 'String')
        $log = Join-Path $env:ProgramData '404ever'
        New-Item -ItemType Directory -Force $log | Out-Null
        Add-Content (Join-Path $log 'driver_guard.log') ("{0} MIDI port entry was missing, restored in {1}" -f (Get-Date -Format s), $slot)
    }
}

if ($Install) {
    $ps = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $arg = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $PSCommandPath
    $action = New-ScheduledTaskAction -Execute $ps -Argument $arg
    $boot = New-ScheduledTaskTrigger -AtStartup
    $logon = New-ScheduledTaskTrigger -AtLogOn
    $every = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 15)
    $who = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $boot, $logon, $every -Principal $who -Settings $set `
        -Description 'Keeps the "TS404 Panel" MIDI port of 404ever registered (Windows can drop it when it reinstalls audio devices).' -Force | Out-Null
}
exit 0
