param(
    [string]$TaskName = "MECO Daily Market Watch",
    [string]$PythonPath = "",
    [string]$UserId = "$env:USERDOMAIN\$env:USERNAME",
    [string]$LogPath = "logs\meco_news.jsonl"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$envFile = Join-Path $projectRoot ".env"

if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
    throw "Create $envFile from .env.example before installing the task."
}

if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $PythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
}
$python = (Resolve-Path -LiteralPath $PythonPath -ErrorAction Stop).Path
$logAbsolute = Join-Path $projectRoot $LogPath
$argument = "-m meco_news --run-if-due --log-file `"$logAbsolute`""
$action = New-ScheduledTaskAction -Execute $python -Argument $argument -WorkingDirectory $projectRoot
$firstRun = (Get-Date).AddMinutes(1)
$trigger = New-ScheduledTaskTrigger -Once -At $firstRun -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 15) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType S4U -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Collect and deliver PT Meco Inoxprima market news to Telegram." `
    -Force | Out-Null

Write-Output "Installed '$TaskName' using $python."
Write-Output "The task checks every 15 minutes; the application computes the Asia/Jakarta due window."

