param([Parameter(ValueFromRemainingArguments=$true)][string[]]$AppArgs)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
foreach ($name in @("OPENAI_API_KEY", "SPEECHMATICS_API_KEY")) {
    if (-not [Environment]::GetEnvironmentVariable($name, "Process")) {
        [Environment]::SetEnvironmentVariable($name, [Environment]::GetEnvironmentVariable($name, "User"), "Process")
    }
}
& "$repo\.venv-win\Scripts\python.exe" -m realtime_subtitles.comparison @AppArgs
exit $LASTEXITCODE
