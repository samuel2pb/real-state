<#
rental-finder installer for Windows (PowerShell 5.1+).

One-liner:
    irm https://raw.githubusercontent.com/samuel2pb/real-state/main/scripts/install.ps1 | iex

`irm ... | iex` cannot pass parameters, so the same options are also read from
environment variables set before the one-liner:
    $env:RENTAL_FINDER_REF = "my-branch"     # same as -Ref my-branch (default: main)
    $env:RENTAL_FINDER_NO_INIT = "1"         # same as -NoInit
    $env:RENTAL_FINDER_HOME = "D:\rf"        # where to clone (default: $env:USERPROFILE\rental-finder)

Downloaded and run as a file, it takes them as parameters:
    .\install.ps1 -Ref my-branch -NoInit
#>
param(
    [string]$Ref = "",
    [switch]$NoInit
)

$ErrorActionPreference = "Stop"

$RepoUrl = "https://github.com/samuel2pb/real-state.git"

if (-not $Ref) { $Ref = $env:RENTAL_FINDER_REF }
if (-not $Ref) { $Ref = "main" }
if ($env:RENTAL_FINDER_NO_INIT -and $env:RENTAL_FINDER_NO_INIT -ne "0") { $NoInit = $true }
$Dest = $env:RENTAL_FINDER_HOME
if (-not $Dest) { $Dest = Join-Path $env:USERPROFILE "rental-finder" }

function Say([string]$msg) { Write-Host "==> $msg" }

function Fail([string]$msg) {
    Write-Host "error: $msg" -ForegroundColor Red
    # `exit` would close the window of someone who ran this through `iex`.
    throw $msg
}

function Invoke-Native([string]$exe, [string[]]$argv) {
    & $exe @argv
    if ($LASTEXITCODE -ne 0) { Fail "$exe $($argv -join ' ') failed (exit $LASTEXITCODE)" }
}

function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Fail "git is required but was not found on PATH. Install it from https://git-scm.com/download/win (or: winget install Git.Git), then open a new PowerShell and re-run."
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Say "Installing uv (https://astral.sh/uv)"
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    Update-SessionPath
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    $uvHome = Join-Path $env:USERPROFILE ".local\bin"
    $env:Path = "$uvHome;$env:Path"
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Fail "uv was installed but is not on PATH; open a new PowerShell and re-run."
}

if (Test-Path (Join-Path $Dest ".git")) {
    Say "Updating $Dest ($Ref)"
    Invoke-Native git @("-C", $Dest, "fetch", "--quiet", "origin", $Ref)
    Invoke-Native git @("-C", $Dest, "checkout", "--quiet", $Ref)
    Invoke-Native git @("-C", $Dest, "pull", "--ff-only", "--quiet", "origin", $Ref)
}
elseif (Test-Path $Dest) {
    Fail "$Dest exists and is not a git checkout; move it or set `$env:RENTAL_FINDER_HOME."
}
else {
    Say "Cloning $RepoUrl ($Ref) into $Dest"
    Invoke-Native git @("clone", "--quiet", "--branch", $Ref, $RepoUrl, $Dest)
}

Push-Location $Dest
try {
    Say "Installing dependencies (uv sync)"
    Invoke-Native uv @("sync")

    if (-not $NoInit) {
        if (Test-Path ".env") {
            Say ".env already exists; skipping the wizard (re-run: uv run rental-finder init --force)"
        }
        elseif ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected) {
            Say "Starting the setup wizard"
            Invoke-Native uv @("run", "rental-finder", "init")
        }
        else {
            Say "No interactive console; run the wizard yourself:"
            Write-Host "    cd `"$Dest`"; uv run rental-finder init"
        }
    }
}
finally {
    Pop-Location
}

Write-Host ""
Write-Host "rental-finder is installed in $Dest"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  cd `"$Dest`""
Write-Host "  uv run rental-finder init                 # if you skipped it: writes .env, creates the Notion databases"
Write-Host "  uv run rental-finder doctor               # checks .env, Notion access, uv / claude on PATH"
Write-Host "  uv run rental-finder run-once             # one rent cycle now (run-buy-once for buy)"
Write-Host "  uv run rental-finder install-scheduler    # run it on SCHEDULE_CRON_TIMES via Task Scheduler"
