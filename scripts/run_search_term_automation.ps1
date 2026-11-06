# Hourly Windows Task Scheduler wrapper for the Search term automation
# (search_term_Automation -> gen_ai_db.searchtermreport). Windows twin of
# run_search_term_automation.sh.
#
#  * resolves the project directory from this script's own location;
#  * uses the project's virtualenv interpreter and passes .env explicitly;
#  * one START / END line per run in logs\search_term_cron.log; the last run's
#    console output in logs\search_term_cron_last_run.log (the full log is
#    logs\search_term_automation.log);
#  * overlapping runs are refused by the application's own lock (exit 11) and by
#    the task's "do not start a new instance" setting;
#  * exits with the Python process's status, so Task Scheduler shows failures.
#
# Extra arguments are passed to run.py (default: --load).

$ErrorActionPreference = 'Stop'
$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $ProjectDir '.venv\Scripts\python.exe'
$EnvFile = Join-Path $ProjectDir '.env'
$LogDir = Join-Path $ProjectDir 'logs'
$CronLog = Join-Path $LogDir 'search_term_cron.log'
$LastRun = Join-Path $LogDir 'search_term_cron_last_run.log'
$LastRunErr = Join-Path $LogDir 'search_term_cron_last_run.err.log'
$RunArgs = if ($args.Count -gt 0) { $args } else { @('--load') }

New-Item -ItemType Directory -Force -Path $LogDir, (Join-Path $ProjectDir 'data') | Out-Null

function Write-CronLog([string]$Message) {
    $utc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    $ist = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId((Get-Date), 'India Standard Time').ToString('HH:mm')
    Add-Content -Path $CronLog -Value "$utc ($ist IST) $Message" -Encoding utf8
}

if (-not (Test-Path $Python)) { Write-CronLog "ERROR python interpreter not found: $Python"; exit 2 }
if (-not (Test-Path $EnvFile)) { Write-CronLog "ERROR env file not found: $EnvFile"; exit 2 }

Set-Location $ProjectDir
Write-CronLog "START search term automation ($($RunArgs -join ' '))"
$argList = @("`"$(Join-Path $ProjectDir 'run.py')`"", '--env-file', "`"$EnvFile`"") + $RunArgs
$process = Start-Process -FilePath $Python -ArgumentList $argList -WorkingDirectory $ProjectDir `
    -NoNewWindow -Wait -PassThru -RedirectStandardOutput $LastRun -RedirectStandardError $LastRunErr
$status = $process.ExitCode
if ($status -eq 0) {
    Write-CronLog 'END status=0 SUCCESS'
} elseif ($status -eq 11) {
    Write-CronLog 'END status=11 SKIPPED (previous run still in progress)'
} else {
    Write-CronLog "END status=$status FAILED (see logs\search_term_automation.log)"
}
exit $status
