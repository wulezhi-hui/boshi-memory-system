# Ensure the current Hermes PM environment has the extra deps boshi memory needs.
# Usage: powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\.boshi\ensure_hermes_deps.ps1"
# Note: chromadb / onnxruntime / transformers / pyyaml are NOT declared Hermes deps,
#       so a new PM generation created by `hermes update` may lack them.
#       This install is idempotent: if already present, uv exits quickly.

$ErrorActionPreference = 'Stop'
$hermes = Join-Path $env:LOCALAPPDATA 'hermes'
$facts  = Join-Path $hermes 'installs\2b4421044f1d482c\facts.json'
$uv     = Join-Path $hermes 'bin\uv.exe'

if (-not (Test-Path $facts)) { Write-Host "facts.json not found: $facts"; exit 1 }
$envPath = (Get-Content $facts -Raw | ConvertFrom-Json).packages.venv.environment
$py = Join-Path $envPath 'Scripts\python.exe'
if (-not (Test-Path $py)) { Write-Host "env python not found: $py"; exit 1 }
Write-Host "PM env: $envPath"

Write-Host "installing/verifying boshi deps (chromadb, onnxruntime, transformers, pyyaml)..."
& $uv pip install --python $py chromadb onnxruntime transformers pyyaml
if ($LASTEXITCODE -ne 0) { Write-Host "uv install failed"; exit 1 }

$check = Join-Path $PSScriptRoot 'check_deps.py'
& $py $check
