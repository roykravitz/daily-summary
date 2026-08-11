# הרצה מקומית בחלונות.
#   .\run.ps1              ריצה רגילה
#   .\run.ps1 -DryRun      הדפסה למסך בלי לשלוח
#   .\run.ps1 -Force       התעלמות מחלון הטריות ומהיסטוריית השליחה
param(
    [switch]$DryRun,
    [switch]$Force,
    [switch]$Verbose
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"

$args = @()
if ($DryRun)  { $args += "--dry-run" }
if ($Force)   { $args += "--force" }
if ($Verbose) { $args += "--verbose" }

python -m src.main @args
exit $LASTEXITCODE
