param([Parameter(ValueFromRemainingArguments=$true)][string[]]$AppArgs)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
# WSL interop processes may not inherit a recently set Windows user environment value.
# Read it in memory only; never write or print the key.
if (-not $env:OPENAI_API_KEY) {
    $env:OPENAI_API_KEY = [Environment]::GetEnvironmentVariable("OPENAI_API_KEY", "User")
}
& "$repo\.venv-win\Scripts\python.exe" -m realtime_subtitles @AppArgs
exit $LASTEXITCODE
