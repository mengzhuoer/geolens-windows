# Produce the portable ZIP, the NSIS installer and SHA-256 files.
# Usage: powershell -ExecutionPolicy Bypass -File scripts/package-release.ps1 [-Makensis <path>]
param(
    [string]$Makensis = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$desktop = Join-Path $root "desktop"
$payload = Join-Path $desktop "dist\GeoLens"
$release = Join-Path $desktop "dist\release"

if (-not (Test-Path (Join-Path $payload "GeoLens.exe"))) {
    throw "Missing $payload. Run scripts/build.ps1 first."
}

$config = Get-Content (Join-Path $desktop "src\desktop\config.py") -Raw
if ($config -notmatch 'APP_VERSION\s*=\s*"([^"]+)"') { throw "APP_VERSION not found" }
$version = $Matches[1]
Write-Host "Version $version"

New-Item -ItemType Directory -Force -Path $release | Out-Null
$zip = Join-Path $release "GeoLens-Portable-$version-win64.zip"
Remove-Item -Force $zip -ErrorAction SilentlyContinue

# Stage under a top-level "GeoLens" folder so extracting does not scatter files.
$stage = Join-Path $desktop "dist\stage"
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $stage | Out-Null
& robocopy $payload (Join-Path $stage "GeoLens") /E /XD (Join-Path $payload "data") /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "staging failed" }

$sevenZip = @(
    "$env:ProgramFiles\7-Zip\7z.exe",
    "${env:ProgramFiles(x86)}\7-Zip\7z.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($sevenZip) {
    Write-Host "Compressing with $sevenZip ..."
    Push-Location $stage
    try {
        & $sevenZip a -tzip -mx=3 $zip "GeoLens" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "7z failed" }
    } finally {
        Pop-Location
    }
} else {
    Write-Host "7-Zip not found; using Compress-Archive (slower, 2 GB limit)..."
    Compress-Archive -Path (Join-Path $stage "GeoLens") -DestinationPath $zip -CompressionLevel Optimal
}

$zipHash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
"$zipHash *$(Split-Path $zip -Leaf)" | Set-Content "$zip.sha256" -Encoding ascii
Write-Host "ZIP: $zip"

# Installer
if (-not $Makensis) {
    $Makensis = @(
        "$env:ProgramFiles\NSIS\makensis.exe",
        "${env:ProgramFiles(x86)}\NSIS\makensis.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $Makensis) {
        $cmd = Get-Command makensis.exe -ErrorAction SilentlyContinue
        if ($cmd) { $Makensis = $cmd.Source }
    }
}
if ($Makensis) {
    Write-Host "Building installer with $Makensis ..."
    & $Makensis (Join-Path $desktop "installer\geolens.nsi")
    if ($LASTEXITCODE -ne 0) { throw "makensis failed" }
    $exe = Join-Path $release "GeoLens-Setup-$version-win64.exe"
    if (Test-Path $exe) {
        $exeHash = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()
        "$exeHash *$(Split-Path $exe -Leaf)" | Set-Content "$exe.sha256" -Encoding ascii
        Write-Host "Installer: $exe"
    }
} else {
    Write-Warning "makensis.exe not found; skipped the installer. Install NSIS or pass -Makensis <path>."
}

Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
Write-Host "Done. Artifacts in $release"
