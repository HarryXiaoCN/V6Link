# Build the standalone Windows executable.
$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location (Split-Path -Parent $scriptDir)

$python = Join-Path (Get-Location) ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Missing .venv. Create it and install requirements first."
}

& $python -m PyInstaller --noconfirm --clean V6Link.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE."
}

$artifact = Join-Path (Get-Location) "dist\V6Link.exe"
if (-not (Test-Path -LiteralPath $artifact)) {
    throw "Build finished but dist\V6Link.exe was not produced."
}
$size = [math]::Round((Get-Item -LiteralPath $artifact).Length / 1MB, 1)
Write-Host "Built $artifact ($size MB)"

