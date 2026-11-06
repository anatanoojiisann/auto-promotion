param([string]$DatabasePath, [string]$ConfigPath)
$ErrorActionPreference = 'Stop'
$effectsRepo = Split-Path -Parent $PSScriptRoot
if (-not $DatabasePath) { $DatabasePath = Join-Path $effectsRepo 'var\auto-promotion.db' }
if (-not $ConfigPath) { $ConfigPath = Join-Path $effectsRepo 'var\effects-cloud-sync.json' }
$effectsPython = Join-Path $effectsRepo '.venv\Scripts\pythonw.exe'
foreach ($effectsFile in @($effectsPython, $DatabasePath, $ConfigPath)) {
    if (-not (Test-Path -LiteralPath $effectsFile -PathType Leaf)) { throw 'Required sync file is missing.' }
}
$effectsTaskName = 'AutoPromotion-Effects-Sync'
$effectsIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$effectsArgs = '-m auto_promotion.effects_sync --db "' + $DatabasePath + '" --config "' + $ConfigPath + '"'
$effectsAction = New-ScheduledTaskAction -Execute $effectsPython -Argument $effectsArgs -WorkingDirectory $effectsRepo
$effectsLogon = New-ScheduledTaskTrigger -AtLogOn -User $effectsIdentity
$effectsFallback = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes 5)
$effectsSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$effectsPrincipal = New-ScheduledTaskPrincipal -UserId $effectsIdentity -LogonType Interactive -RunLevel Limited
$effectsTask = New-ScheduledTask -Action $effectsAction -Trigger @($effectsLogon,$effectsFallback) -Settings $effectsSettings -Principal $effectsPrincipal -Description 'Copies public publication counters and metric history to the read-only Cloudflare effects service. Does not collect from platforms or execute tasks.'
Register-ScheduledTask -TaskName $effectsTaskName -InputObject $effectsTask -Force | Out-Null
Start-ScheduledTask -TaskName $effectsTaskName
Get-ScheduledTask -TaskName $effectsTaskName | Select-Object TaskName,State | ConvertTo-Json -Compress
