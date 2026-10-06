param([switch]$OneFile, [switch]$Console)
$ErrorActionPreference = "Stop"
if ($env:OS -ne "Windows_NT") { throw "Run with Windows PowerShell / Windows Python." }
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$python = Join-Path $repo ".venv-win\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Create .venv-win using Windows Python first." }
& $python -m pip install -e '.[packaging]'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$mode = if ($OneFile) { "--onefile" } else { "--onedir" }
$ui = if ($Console) { "--console" } else { "--windowed" }
$appName = if ($Console) { "RealtimeSubtitlesConsole" } else { "RealtimeSubtitles" }
& $python -m PyInstaller --noconfirm --clean $mode $ui --name $appName `
    --paths src --collect-all soxr `
    --exclude-module pytest --exclude-module ruff --exclude-module numpy.tests `
    --specpath build scripts\entrypoint.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output "Built under $repo\dist. Distribute the entire RealtimeSubtitles folder for onedir."
