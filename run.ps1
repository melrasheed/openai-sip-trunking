<#
.SYNOPSIS
    Creates the virtual environment, installs requirements, and starts the
    contextualised banking voice agent.

.DESCRIPTION
    Safe to re-run: the virtual environment is only created once, and
    dependencies are reinstalled only when requirements.txt changes.
    The demo database is created and seeded automatically on first start.

.EXAMPLE
    .\run.ps1
    .\run.ps1 -Port 8100
    .\run.ps1 -Seed
    .\run.ps1 -Reinstall
#>
[CmdletBinding()]
param(
    # Override the port. Defaults to PORT from .env, or 8000.
    [int]$Port,

    # Recreate the virtual environment from scratch.
    [switch]$Reinstall,

    # Replace the customer table with the demo seed data before starting.
    [switch]$Seed
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$venvDir = Join-Path $PSScriptRoot '.venv'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
$requirements = Join-Path $PSScriptRoot 'requirements.txt'
$stamp = Join-Path $venvDir '.requirements.sha256'

function Resolve-Python {
    # Prefer a specific modern version, then fall back to whatever is present.
    foreach ($candidate in @(@('py', '-3.12'), @('py', '-3'), @('python'))) {
        $exe, $extra = $candidate[0], $candidate[1..($candidate.Count - 1)]
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $found = & $exe @extra -c 'import sys; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and $found) { return $found.Trim() }
        }
        catch { continue }
    }
    throw 'No Python interpreter found. Install Python 3.10 or later from https://python.org'
}

if ($Reinstall -and (Test-Path $venvDir)) {
    Write-Host 'Removing existing virtual environment...' -ForegroundColor Yellow
    Remove-Item -Recurse -Force $venvDir
}

if (-not (Test-Path $venvPython)) {
    $python = Resolve-Python
    Write-Host "Creating virtual environment using $python" -ForegroundColor Cyan
    & $python -m venv $venvDir
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create the virtual environment' }
}

# Reinstall only when requirements.txt has actually changed.
$hash = (Get-FileHash -Path $requirements -Algorithm SHA256).Hash
$installed = if (Test-Path $stamp) { (Get-Content $stamp -Raw).Trim() } else { '' }

if ($hash -ne $installed) {
    Write-Host 'Installing dependencies...' -ForegroundColor Cyan
    & $venvPython -m pip install --upgrade pip --quiet --disable-pip-version-check
    & $venvPython -m pip install -r $requirements --quiet --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install dependencies' }
    Set-Content -Path $stamp -Value $hash -Encoding ascii
}
else {
    Write-Host 'Dependencies already up to date.' -ForegroundColor DarkGray
}

if (-not (Test-Path (Join-Path $PSScriptRoot '.env'))) {
    Write-Warning 'No .env found. Copy .env.example to .env and fill in your Azure OpenAI values.'
}

if ($PSBoundParameters.ContainsKey('Port')) { $env:PORT = "$Port" }

if ($Seed) {
    Write-Host 'Reseeding the demo customer data...' -ForegroundColor Cyan
    & $venvPython -c "import sys; sys.path.insert(0, 'src'); import db; print('Seeded', db.seed(force=True), 'customers')"
    if ($LASTEXITCODE -ne 0) { throw 'Seeding failed' }
}

$uiPort = if ($env:PORT) { $env:PORT } else { '8000' }
Write-Host "Starting the agent (Ctrl+C to stop). UI: http://127.0.0.1:$uiPort/" -ForegroundColor Green
& $venvPython -u (Join-Path $PSScriptRoot 'src\app.py')
