# Build the frontend, the PyInstaller launcher, and the dist/GeoLens payload.
# Usage: powershell -ExecutionPolicy Bypass -File scripts/build.ps1 [-SkipFrontend] [-SkipLauncher]
param(
    [switch]$SkipFrontend,
    [switch]$SkipLauncher
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$desktop = Join-Path $root "desktop"
$runtime = Join-Path $desktop "runtime"
$python = Join-Path $runtime "py\python.exe"
$out = Join-Path $desktop "dist\GeoLens"

function Assert-Path([string]$Path, [string]$Hint) {
    if (-not (Test-Path $Path)) { throw "Missing $Path. $Hint" }
}

function Copy-Tree([string]$From, [string]$To) {
    New-Item -ItemType Directory -Force -Path $To | Out-Null
    & robocopy $From $To /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed: $From -> $To ($LASTEXITCODE)" }
}

Assert-Path $python "Run scripts/prepare-runtime.ps1 first."
Assert-Path (Join-Path $runtime "pgsql\bin\pg_ctl.exe") "Run scripts/prepare-runtime.ps1 first."

# 1. Frontend
if (-not $SkipFrontend) {
    Write-Host "Building frontend..."
    Push-Location (Join-Path $root "app\frontend")
    try {
        if (-not (Test-Path "node_modules")) { & npm ci } else { & npm ci }
        if ($LASTEXITCODE -ne 0) { throw "npm ci failed" }
        & npm run build
        if ($LASTEXITCODE -ne 0) { throw "npm run build failed" }
    } finally {
        Pop-Location
    }
}
Assert-Path (Join-Path $root "app\frontend\dist\index.html") "Frontend build output is missing."

# 2. PyInstaller launcher
$launcherOut = Join-Path $desktop "dist\launcher"
if (-not $SkipLauncher) {
    Write-Host "Building GeoLens.exe..."
    Push-Location $desktop
    try {
        & $python -m pip install --upgrade pyinstaller
        & $python -m PyInstaller --noconfirm --distpath "dist\launcher" --workpath "build" GeoLens.spec
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
    } finally {
        Pop-Location
    }
}
Assert-Path (Join-Path $launcherOut "GeoLens\GeoLens.exe") "PyInstaller output is missing."

# 3. Assemble the payload. The launcher lives in dist\launcher so it is never
# overwritten by the assembled tree at dist\GeoLens.
Write-Host "Assembling $out ..."
Remove-Item -Recurse -Force $out -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $out | Out-Null

Copy-Tree (Join-Path $launcherOut "GeoLens") $out
Copy-Tree (Join-Path $root "app\frontend\dist") (Join-Path $out "app\frontend_dist")

& robocopy (Join-Path $root "app\backend") (Join-Path $out "app\backend") /E `
    /XD tests __pycache__ .pytest_cache .mypy_cache .ruff_cache .venv /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy backend failed" }

$appDir = Join-Path $out "app"
Copy-Item (Join-Path $desktop "src\server_entry.py") $appDir -Force
Copy-Item (Join-Path $desktop "src\geolens_server.py") $appDir -Force
Copy-Item (Join-Path $desktop "src\geolens_web.py") $appDir -Force

& robocopy (Join-Path $desktop "src\desktop") (Join-Path $appDir "desktop") /E `
    /XD __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy desktop source failed" }

Copy-Tree (Join-Path $runtime "py") (Join-Path $out "runtime\py")
Copy-Tree (Join-Path $runtime "pgsql") (Join-Path $out "runtime\pgsql")

Copy-Item (Join-Path $desktop "launchers\*") $out -Force
Copy-Item (Join-Path $root "docs\USAGE.md") (Join-Path $out "README-使用说明.md") -Force
foreach ($doc in @("LICENSE", "NOTICE", "THIRD_PARTY-LICENSES.md", "THIRD_PARTY_DATA.md", "TRADEMARKS.md")) {
    $src = Join-Path $root $doc
    if (Test-Path $src) { Copy-Item $src $out -Force }
}

# Never ship local state.
foreach ($junk in @("data", "cache", "runtime\py\pkgs", "runtime\py\conda-meta")) {
    Remove-Item -Recurse -Force (Join-Path $out $junk) -ErrorAction SilentlyContinue
}

$size = (Get-ChildItem $out -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("Payload ready: {0} ({1:N0} MB)" -f $out, $size)
Write-Host "Next: powershell -ExecutionPolicy Bypass -File scripts/package-release.ps1"
