param([Parameter(ValueFromRemainingArguments=$true)][string[]]$AppArgs)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
# WSL interop processes may not inherit a recently set Windows user environment value.
# Read it in memory only; never write or print the key.
foreach ($name in @("OPENAI_API_KEY", "SPEECHMATICS_API_KEY")) {
    if (-not [Environment]::GetEnvironmentVariable($name, "Process")) {
        [Environment]::SetEnvironmentVariable($name, [Environment]::GetEnvironmentVariable($name, "User"), "Process")
    }
}
& "$repo\.venv-win\Scripts\python.exe" -m realtime_subtitles @AppArgs
exit $LASTEXITCODE
