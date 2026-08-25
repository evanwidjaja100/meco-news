param(
    [string]$TaskName = "MECO Daily Market Watch"
)

$ErrorActionPreference = "Stop"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $task) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "Removed '$TaskName'."
} else {
    Write-Output "'$TaskName' was already absent."
}
Write-Output "The local .env, article history, logs, and backups were not deleted."

