# Run once to start Jarvis + the widget at every login:   powershell -ExecutionPolicy Bypass -File autostart.ps1
# Undo:                                                    powershell -ExecutionPolicy Bypass -File autostart.ps1 -Remove
param([switch]$Remove)
$J = $PSScriptRoot
if ($Remove) { "Jarvis", "Jarvis Widget", "JARVIS OS" | ForEach-Object { Unregister-ScheduledTask -TaskName $_ -Confirm:$false }; "Removed."; return }
$set = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
$trig = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$who = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$brain = New-ScheduledTaskAction -Execute "conhost.exe" -Argument "--headless `"$J\.venv\Scripts\python.exe`" -X utf8 `"$J\jarvis.py`"" -WorkingDirectory $J
$widget = New-ScheduledTaskAction -Execute "$J\.venv\Scripts\pythonw.exe" -Argument "-X utf8 `"$J\widget.py`"" -WorkingDirectory $J
Register-ScheduledTask -TaskName "Jarvis" -Action $brain -Trigger $trig -Settings $set -Principal $who -Force | Out-Null
Register-ScheduledTask -TaskName "Jarvis Widget" -Action $widget -Trigger $trig -Settings $set -Principal $who -Force | Out-Null
$os = New-ScheduledTaskAction -Execute "$J\.venv\Scripts\pythonw.exe" -Argument "-X utf8 `"$J\jarvis-os\jarvis_os.py`"" -WorkingDirectory "$J\jarvis-os"
Register-ScheduledTask -TaskName "JARVIS OS" -Action $os -Trigger $trig -Settings $set -Principal $who -Force | Out-Null
"Jarvis will start at login."
