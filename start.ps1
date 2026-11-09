$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8:strict'
$env:PYTHONUTF8 = '1'
$OutputEncoding = [System.Text.UTF8Encoding]::new($false, $true)
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false, $true)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false, $true)
$apPython = if ($env:AUTO_PROMOTION_PYTHON) { $env:AUTO_PROMOTION_PYTHON } else { Join-Path $PSScriptRoot '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $apPython)) { throw 'Run install.ps1 first.' }
$apArguments = @('serve', '--port', '8780')
if ($args.Count) { $apArguments = @($args) }
& $apPython -m auto_promotion.cli @apArguments
exit $LASTEXITCODE
