# רושם משימה מתוזמנת בחלונות לפי השעות שב-config.yaml.
# הרץ מ-PowerShell רגיל (לא צריך הרשאות מנהל):
#   .\scripts\install_task.ps1
# להסרה:
#   Unregister-ScheduledTask -TaskName "סיכום מידע יומי" -Confirm:$false

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$taskName = "סיכום מידע יומי"

$configPath = Join-Path $root "config.yaml"
if (-not (Test-Path $configPath)) {
    Write-Error "לא נמצא config.yaml. פתח את setup.html, מלא את הטופס והורד אותו לתיקייה."
}

# קריאת שעות הריצה מתוך config.yaml
$times = python -c @"
import sys, yaml
cfg = yaml.safe_load(open(r'$configPath', encoding='utf-8')) or {}
print(' '.join((cfg.get('schedule') or {}).get('run_at') or ['08:00']))
"@
if (-not $times) { $times = "08:00" }

$triggers = @()
foreach ($t in $times.Trim().Split(" ")) {
    $triggers += New-ScheduledTaskTrigger -Daily -At $t
}

$python = (Get-Command python).Source
$action = New-ScheduledTaskAction -Execute $python `
    -Argument "-m src.main" -WorkingDirectory $root

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers `
    -Settings $settings -Description "סיכום יומי של מקורות המידע שהוגדרו" -Force | Out-Null

Write-Host "נרשמה משימה '$taskName' בשעות: $times" -ForegroundColor Green
Write-Host "בדיקה מיידית:  Start-ScheduledTask -TaskName '$taskName'"
