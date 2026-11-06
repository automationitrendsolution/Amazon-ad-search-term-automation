# Registers (or replaces) the hourly Windows Task Scheduler task -- the Windows
# twin of install_cron.sh.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_task_windows.ps1            # every hour at :45
#   powershell -ExecutionPolicy Bypass -File scripts\install_task_windows.ps1 -Minute 15
#   powershell -ExecutionPolicy Bypass -File scripts\install_task_windows.ps1 -Remove
#
# The task runs as the current user while they are signed in to Windows (the
# Amazon session in data\session belongs to this user). It only replaces its
# own task; other scheduled tasks are not touched.

param(
    [int]$Minute = 45,
    [string]$TaskName = 'Amazon Search Term Automation',
    [string]$RunArgs = '--load',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Removed task '$TaskName'"
    exit 0
}
if ($Minute -lt 0 -or $Minute -gt 59) { throw "-Minute must be 0-59" }

$wrapper = (Resolve-Path (Join-Path $PSScriptRoot 'run_search_term_automation.ps1')).Path
$projectDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

# First run: the next hh:$Minute from now; then every hour, indefinitely.
$now = Get-Date
$start = $now.Date.AddHours($now.Hour).AddMinutes($Minute)
if ($start -le $now.AddMinutes(1)) { $start = $start.AddHours(1) }

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -WorkingDirectory $projectDir `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$wrapper`" $RunArgs"
$trigger = New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Hours 1)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 55)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'search_term_Automation (Amazon Ads) -> gen_ai_db.searchtermreport, hourly' `
    -Force | Out-Null

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Output "Registered task '$TaskName'"
Write-Output "  runs       : powershell -File $wrapper $RunArgs"
Write-Output "  first run  : $($info.NextRunTime)"
Write-Output "  repeats    : every $($task.Triggers[0].Repetition.Interval) (duration: $(if ($task.Triggers[0].Repetition.Duration) { $task.Triggers[0].Repetition.Duration } else { 'indefinitely' }))"
Write-Output "  overlap    : a new run is not started while one is still running"
