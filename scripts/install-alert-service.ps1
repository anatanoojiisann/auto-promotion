param(
    [Parameter(Mandatory = $true)][string]$KeyFile
)
$ErrorActionPreference = 'Stop'
$alertRepo = Split-Path -Parent $PSScriptRoot
$alertDb = Join-Path $alertRepo 'var\auto-promotion.db'
$alertPython = Join-Path $alertRepo '.venv\Scripts\pythonw.exe'
$alertKeyPath = (Resolve-Path -LiteralPath $KeyFile).Path
if (-not (Test-Path -LiteralPath $alertPython)) { throw 'Project Python is missing.' }
if (-not (Test-Path -LiteralPath $alertDb)) { throw 'Existing campaign database is missing.' }
# The task stores only a file path. The private key never enters process arguments.
$alertArgs = '-m auto_promotion.alert_service --db "' + $alertDb + '" --env-file "' + $alertKeyPath + '" --enable serve'
$alertName = 'AutoPromotion-Alerts'
$alertIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$alertAction = New-ScheduledTaskAction -Execute $alertPython -Argument $alertArgs -WorkingDirectory $alertRepo
$alertLogon = New-ScheduledTaskTrigger -AtLogOn -User $alertIdentity
$alertFallback = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes 5)
$alertSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$alertPrincipal = New-ScheduledTaskPrincipal -UserId $alertIdentity -LogonType Interactive -RunLevel Limited
$alertTask = New-ScheduledTask -Action $alertAction -Trigger @($alertLogon, $alertFallback) -Settings $alertSettings -Principal $alertPrincipal -Description 'Sends one compact platform-count summary at each UTC+8 three-hour boundary. No automatic task, recovery, startup, daily or historical-revision pushes. Never starts social tasks.'
if (Get-ScheduledTask -TaskName $alertName -ErrorAction SilentlyContinue) {
    throw 'Alert task already exists. Inspect it before replacing a running installation.'
}
Register-ScheduledTask -TaskName $alertName -InputObject $alertTask | Out-Null
Start-ScheduledTask -TaskName $alertName
& (Join-Path $PSScriptRoot 'install-alert-boundary-wake.ps1')
Get-ScheduledTask -TaskName $alertName | Select-Object TaskName,State | ConvertTo-Json -Compress
