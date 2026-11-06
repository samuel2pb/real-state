<#
Runner for the Windows scheduled tasks that `install-scheduler` creates.

Shows the cycle's output live in the task's console window and appends the same
lines to the log, so the window is worth watching and the log stays complete.
Closing the window stops the run. On failure the window stays up for 5 minutes
so the error can be read.

    run-task.ps1 -Uv <path to uv.exe> -Log logs\run-rent.log -Run "run-once"
#>
param(
    [Parameter(Mandatory = $true)][string]$Uv,
    [Parameter(Mandatory = $true)][string]$Log,
    # One string, split here: `--kind` passed as a bare argument would be taken
    # for a PowerShell parameter.
    [Parameter(Mandatory = $true)][string]$Run
)

$ErrorActionPreference = "Continue"
Set-Location (Split-Path -Parent $PSScriptRoot)
# @(): one word ("run-once") would otherwise come back as a bare string, and
# splatting a string passes only its first character.
$argv = @($Run -split " " | Where-Object { $_ })
$Host.UI.RawUI.WindowTitle = "rental-finder $Run"

# Unbuffered UTF-8 from Python: lines show up as they happen, accents survive.
$env:PYTHONUTF8 = "1"
$env:PYTHONUNBUFFERED = "1"
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8

$writer = New-Object System.IO.StreamWriter([IO.Path]::Combine((Get-Location).Path, $Log), $true, $utf8)
$writer.AutoFlush = $true
$code = 1
try {
    & $Uv run rental-finder @argv 2>&1 | ForEach-Object {
        $line = "$_"
        Write-Host $line
        $writer.WriteLine($line)
    }
    $code = $LASTEXITCODE
}
finally {
    $writer.Dispose()
}

if ($code -ne 0) {
    Write-Host ""
    Write-Host "rental-finder $Run exited with $code (log: $Log). Closing in 5 minutes."
    Start-Sleep -Seconds 300
}
exit $code
