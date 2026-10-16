param([switch]$Login)
$ErrorActionPreference = 'Stop'
$controlRepo = Split-Path -Parent $PSScriptRoot
$controlProject = Join-Path $controlRepo 'cloudflare\control-plane'
$controlWrangler = Join-Path $controlProject 'node_modules\.bin\wrangler.cmd'
if (-not (Test-Path -LiteralPath $controlWrangler)) { throw 'Run npm ci in cloudflare/control-plane first.' }
Push-Location $controlProject
try {
    if ($Login) {
        & $controlWrangler login --device --scopes account:read user:read workers:write workers_scripts:write workers_routes:write
        if ($LASTEXITCODE -ne 0) { throw 'Cloudflare login did not finish.' }
    }
    & $controlWrangler deploy
    if ($LASTEXITCODE -ne 0) { throw 'Cloudflare deployment failed. Existing sender configuration was not changed.' }
    $controlKey = Join-Path $controlRepo 'var\cloud-control-operator.key'
    New-Item -ItemType Directory -Path (Join-Path $controlRepo 'var') -Force | Out-Null
    if (-not (Test-Path -LiteralPath $controlKey)) {
        $controlBytes = New-Object byte[] 48
        [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($controlBytes)
        [System.IO.File]::WriteAllText($controlKey, [Convert]::ToBase64String($controlBytes))
    }
    # Send the secret through stdin; never put it on the command line or in console output.
    Get-Content -LiteralPath $controlKey -Raw | & $controlWrangler secret put OPERATOR_TOKEN
    if ($LASTEXITCODE -ne 0) { throw 'Worker exists but its private operator secret still needs configuration.' }
    Write-Output 'Cloudflare control service deployed. It starts stopped; bind an executor before adopting the existing policy.'
} finally { Pop-Location }
