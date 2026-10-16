$ErrorActionPreference = 'Stop'
$senderRepo = Split-Path -Parent $PSScriptRoot
$senderDb = Join-Path $senderRepo 'var\auto-promotion.db'
$senderPython = Join-Path $senderRepo '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $senderPython)) { throw 'Project Python is missing.' }
if (-not (Test-Path -LiteralPath $senderDb)) { throw 'Existing campaign database is missing.' }
$senderTaskName = 'AutoPromotion-Sender-8780'
$senderIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$senderArgs = '-m auto_promotion.service_host --db "' + $senderDb + '" --port 8780'
$senderAction = New-ScheduledTaskAction -Execute $senderPython -Argument $senderArgs -WorkingDirectory $senderRepo
$senderLogon = New-ScheduledTaskTrigger -AtLogOn -User $senderIdentity
# IgnoreNew makes the periodic fallback a liveness check, not a second sender.
$senderFallback = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes 5)
$senderSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$senderPrincipal = New-ScheduledTaskPrincipal -UserId $senderIdentity -LogonType Interactive -RunLevel Limited
$senderTask = New-ScheduledTask -Action $senderAction -Trigger @($senderLogon, $senderFallback) -Settings $senderSettings -Principal $senderPrincipal -Description 'Keeps the existing Auto Promotion sender online in the signed-in desktop session. Never starts draft tasks, releases stops, changes quotas or retries uncertain items.'
Register-ScheduledTask -TaskName $senderTaskName -InputObject $senderTask -Force | Out-Null
Start-ScheduledTask -TaskName $senderTaskName
Get-ScheduledTask -TaskName $senderTaskName | Select-Object TaskName,State,@{Name='LogonType';Expression={$_.Principal.LogonType}},@{Name='RunLevel';Expression={$_.Principal.RunLevel}},@{Name='RestartCount';Expression={$_.Settings.RestartCount}},@{Name='ExecutionTimeLimit';Expression={$_.Settings.ExecutionTimeLimit}} | ConvertTo-Json -Compress
