$ErrorActionPreference = 'Stop'
$reportRepo = Split-Path -Parent $PSScriptRoot
$reportDb = Join-Path $reportRepo 'var\auto-promotion.db'
$reportPython = Join-Path $reportRepo '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $reportPython)) { throw 'Project Python is missing.' }
if (-not (Test-Path -LiteralPath $reportDb)) { throw 'Existing campaign database is missing.' }
$reportTaskName = 'AutoPromotion-Reports-8782'
$reportIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$reportArgs = '-m auto_promotion.service_host --db "' + $reportDb + '" --port 8782 --reports-only'
$reportAction = New-ScheduledTaskAction -Execute $reportPython -Argument $reportArgs -WorkingDirectory $reportRepo
$reportLogon = New-ScheduledTaskTrigger -AtLogOn -User $reportIdentity
$reportFallback = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes 5)
$reportSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$reportPrincipal = New-ScheduledTaskPrincipal -UserId $reportIdentity -LogonType Interactive -RunLevel Limited
$reportTask = New-ScheduledTask -Action $reportAction -Trigger @($reportLogon, $reportFallback) -Settings $reportSettings -Principal $reportPrincipal -Description 'Hosts only read-only statistics and saved reports at 8782. No sender, feeds, inbox, joins or external writes. Preserves existing cooldowns, stops and limits.'
Register-ScheduledTask -TaskName $reportTaskName -InputObject $reportTask -Force | Out-Null
Start-ScheduledTask -TaskName $reportTaskName
Get-ScheduledTask -TaskName $reportTaskName | Select-Object TaskName,State | ConvertTo-Json -Compress
