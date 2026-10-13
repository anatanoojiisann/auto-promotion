param([switch]$SkipBrowser)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Creating the Python virtual environment failed.' }
}
$apPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $apPython -m pip install -e '.[all]'
if ($LASTEXITCODE -ne 0) { throw 'Installing dependencies failed.' }
if (-not $SkipBrowser) {
    & $apPython -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { throw 'Installing Chromium failed.' }
}
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
& $apPython -m auto_promotion.cli init
if ($LASTEXITCODE -ne 0) { throw 'Initializing the local database failed.' }
& $apPython -m auto_promotion.cli doctor
if ($LASTEXITCODE -ne 0) { throw 'Checking the local environment failed.' }
& $apPython -m auto_promotion.mcp_server --project-dir $PSScriptRoot --print-config | Set-Content -LiteralPath 'var\mcp-client-config.json' -Encoding utf8
if ($LASTEXITCODE -ne 0) { throw 'Generating MCP client configuration failed.' }
& $apPython 'scripts\check_mcp.py'
if ($LASTEXITCODE -ne 0) { throw 'MCP connection check failed.' }
Write-Output 'MCP is installed. Client configuration: var\mcp-client-config.json'
