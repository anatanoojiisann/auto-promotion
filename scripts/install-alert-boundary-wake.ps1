$ErrorActionPreference = 'Stop'
$alertRepo = Split-Path -Parent $PSScriptRoot
$alertPrimary = Get-ScheduledTask -TaskName 'AutoPromotion-Alerts'
$alertParentAction = @($alertPrimary.Actions)
if ($alertParentAction.Count -ne 1) { throw 'Expected one existing alert action.' }
$alertExpectedPython = Join-Path $alertRepo '.venv\Scripts\pythonw.exe'
if (-not [string]::Equals($alertParentAction[0].Execute, $alertExpectedPython, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The existing alert service belongs to a different installation.'
}
$alertParentArguments = $alertParentAction[0].Arguments
if ($alertParentArguments -notmatch '^-m auto_promotion\.alert_service\s' -or $alertParentArguments -notmatch '(?<!\S)serve\s*$') {
    throw 'The existing alert action is not the expected notification service.'
}
# Reuse only the existing private-file reference; never read or copy its key.
$alertOnceArguments = $alertParentArguments -replace '(?<!\S)serve\s*$', 'once'
$alertName = 'AutoPromotion-Alerts-Boundary'
$alertExisting = Get-ScheduledTask -TaskName $alertName -ErrorAction SilentlyContinue
if ($alertExisting) {
    $alertOldAction = @($alertExisting.Actions)
    if ($alertOldAction.Count -ne 1 -or -not [string]::Equals($alertOldAction[0].Execute, $alertExpectedPython, [StringComparison]::OrdinalIgnoreCase) -or $alertOldAction[0].Arguments -notmatch '^-m auto_promotion\.alert_service\s') {
        throw 'A different task already uses the boundary task name.'
    }
}
$alertIdentity = $alertPrimary.Principal.UserId
$alertAction = New-ScheduledTaskAction -Execute $alertExpectedPython -Argument $alertOnceArguments -WorkingDirectory $alertRepo
# Fixed local UTC+8 boundaries. The repeating task wakes only every three hours,
# not on the existing host's five-minute health-check trigger.
if ((Get-TimeZone).Id -ne 'China Standard Time') { throw 'Configure explicit UTC+8 trigger times on this host first.' }
$alertTriggers = @(0,3,6,9,12,15,18,21 | ForEach-Object {
    New-ScheduledTaskTrigger -Daily -At ([datetime]::Today.AddHours($_))
})
$alertSettings = New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -MultipleInstances IgnoreNew
$alertPrincipal = New-ScheduledTaskPrincipal -UserId $alertIdentity -LogonType Interactive -RunLevel Limited
$alertTask = New-ScheduledTask -Action $alertAction -Trigger $alertTriggers -Settings $alertSettings -Principal $alertPrincipal -Description 'Wake at UTC+8 three-hour boundaries and run one notification cycle using the existing durable queue. Resume may recover only the latest missed window once. No batch catch-up or social task execution.'
Register-ScheduledTask -TaskName $alertName -InputObject $alertTask -Force | Out-Null
# Do not start now: this task exists only to service the scheduled boundary.
$alertSaved = Get-ScheduledTask -TaskName $alertName
$alertInfo = Get-ScheduledTaskInfo -TaskName $alertName
[pscustomobject]@{TaskName=$alertName;WakeToRun=$alertSaved.Settings.WakeToRun;TriggerCount=@($alertSaved.Triggers).Count;NextRunTime=$alertInfo.NextRunTime.ToString('o')} | ConvertTo-Json -Compress
