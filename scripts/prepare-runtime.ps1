# Prepare the bundled Windows runtime: Python+GDAL, PostgreSQL, PostGIS, pgvector.
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts/prepare-runtime.ps1
#   ... -PythonOnly      rebuild desktop/runtime/py only
#   ... -PostgresOnly    rebuild desktop/runtime/pgsql only
param(
    [string]$RuntimeDir = "",
    [string]$CacheDir = "",
    [switch]$PythonOnly,
    [switch]$PostgresOnly
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $RuntimeDir) { $RuntimeDir = Join-Path $root "desktop\runtime" }
if (-not $CacheDir) { $CacheDir = Join-Path $root "desktop\cache" }
New-Item -ItemType Directory -Force -Path $RuntimeDir, $CacheDir | Out-Null

$PG_VERSION = "16.15"
$POSTGIS_VERSION = "3.6.2"
$PGVECTOR_VERSION = "0.8.6"
$PG_URL = "https://get.enterprisedb.com/postgresql/postgresql-$PG_VERSION-1-windows-x64-binaries.zip"
$POSTGIS_URL = "http://download.osgeo.org/postgis/windows/pg16/postgis-bundle-pg16-$POSTGIS_VERSION" + "x64.zip"

function Get-File([string]$Url, [string]$OutFile) {
    if (Test-Path $OutFile) {
        Write-Host "  cached $OutFile"
        return
    }
    Write-Host "  downloading $Url"
    Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
}

function Get-Micromamba {
    $exe = Join-Path $CacheDir "micromamba.exe"
    if (Test-Path $exe) { return $exe }
    $archive = Join-Path $CacheDir "micromamba.tar.bz2"
    Get-File "https://micro.mamba.pm/api/micromamba/win-64/latest" $archive
    $tmp = Join-Path $CacheDir "micromamba-extract"
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    & tar -xjf $archive -C $tmp
    $found = Get-ChildItem $tmp -Recurse -Filter "micromamba.exe" | Select-Object -First 1
    if (-not $found) { throw "micromamba.exe not found in archive" }
    Copy-Item $found.FullName $exe -Force
    return $exe
}

function Invoke-PythonRuntime([string]$Mamba) {
    $pyDir = Join-Path $RuntimeDir "py"
    if (Test-Path (Join-Path $pyDir "python.exe")) {
        Write-Host "Python runtime already present: $pyDir"
    } else {
        Write-Host "Creating Python 3.13 + GDAL environment..."
        & $Mamba create -y -p $pyDir -c conda-forge python=3.13 pip gdal
        if ($LASTEXITCODE -ne 0) { throw "micromamba create failed" }
    }
    $python = Join-Path $pyDir "python.exe"
    Write-Host "Installing Python dependencies..."
    & $python -m pip install --upgrade pip
    & $python -m pip install -r (Join-Path $root "desktop\requirements-runtime.txt")
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
    Write-Host "Installing OGR PostgreSQL plugin (libgdal-pg)..."
    & $Mamba install -y -p $pyDir -c conda-forge "libgdal-pg=3.13.3"
    if ($LASTEXITCODE -ne 0) { throw "libgdal-pg install failed" }
}

function Invoke-PostgresRuntime([string]$Mamba) {
    $pgDir = Join-Path $RuntimeDir "pgsql"
    Remove-Item -Recurse -Force $pgDir -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $pgDir | Out-Null

    Write-Host "Preparing PostgreSQL $PG_VERSION..."
    $pgZip = Join-Path $CacheDir "pg16.zip"
    Get-File $PG_URL $pgZip
    $pgTmp = Join-Path $CacheDir "pg16"
    Remove-Item -Recurse -Force $pgTmp -ErrorAction SilentlyContinue
    Expand-Archive $pgZip $pgTmp -Force
    $pgSrc = Join-Path $pgTmp "pgsql"
    if (-not (Test-Path $pgSrc)) { $pgSrc = (Get-ChildItem $pgTmp -Directory | Select-Object -First 1).FullName }
    Copy-Item (Join-Path $pgSrc "*") $pgDir -Recurse -Force

    Write-Host "Merging PostGIS $POSTGIS_VERSION..."
    $gisZip = Join-Path $CacheDir "postgis16.zip"
    Get-File $POSTGIS_URL $gisZip
    $gisTmp = Join-Path $CacheDir "postgis16"
    Remove-Item -Recurse -Force $gisTmp -ErrorAction SilentlyContinue
    Expand-Archive $gisZip $gisTmp -Force
    $gisSrc = $gisTmp
    $nested = Get-ChildItem $gisTmp -Directory -Filter "postgis*" | Select-Object -First 1
    if ($nested -and (Test-Path (Join-Path $nested.FullName "bin"))) { $gisSrc = $nested.FullName }

    # These DLLs ship with PostgreSQL; the PostGIS bundle carries slightly
    # different builds and overwriting them breaks pg_ctl.
    $skip = @(
        "libcrypto-3-x64.dll", "libcurl.dll", "libiconv-2.dll", "liblz4.dll",
        "libssl-3-x64.dll", "libzstd.dll", "zlib1.dll"
    )
    foreach ($sub in @("bin", "lib", "share")) {
        $from = Join-Path $gisSrc $sub
        if (-not (Test-Path $from)) { continue }
        $to = Join-Path $pgDir $sub
        New-Item -ItemType Directory -Force -Path $to | Out-Null
        Get-ChildItem $from -Recurse -File | ForEach-Object {
            if ($skip -contains $_.Name) { return }
            $rel = $_.FullName.Substring($from.Length).TrimStart('\')
            $dest = Join-Path $to $rel
            New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
            Copy-Item $_.FullName $dest -Force
        }
    }
    $gisData = Join-Path $gisSrc "gdal-data"
    if (Test-Path $gisData) {
        Copy-Item $gisData (Join-Path $pgDir "gdal-data") -Recurse -Force
    }

    Write-Host "Installing pgvector $PGVECTOR_VERSION..."
    $pgv = Join-Path $CacheDir "pgv"
    if (-not (Test-Path (Join-Path $pgv "Library\lib\vector.dll"))) {
        Remove-Item -Recurse -Force $pgv -ErrorAction SilentlyContinue
        & $Mamba create -y -p $pgv -c conda-forge "pgvector=$PGVECTOR_VERSION"
        if ($LASTEXITCODE -ne 0) { throw "pgvector install failed" }
    }
    Copy-Item (Join-Path $pgv "Library\lib\vector.dll") (Join-Path $pgDir "lib") -Force
    Copy-Item (Join-Path $pgv "Library\share\extension\vector*") (Join-Path $pgDir "share\extension") -Force

    foreach ($junk in @("pgAdmin 4", "StackBuilder", "doc", "include", "symbols")) {
        Remove-Item -Recurse -Force (Join-Path $pgDir $junk) -ErrorAction SilentlyContinue
    }

    Write-Host "Verifying PostgreSQL + PostGIS + pgvector..."
    $smoke = Join-Path $CacheDir "pg-smoke"
    Remove-Item -Recurse -Force $smoke -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $smoke | Out-Null
    $pw = Join-Path $smoke "pw.txt"
    Set-Content $pw "postgres" -NoNewline -Encoding ascii
    & (Join-Path $pgDir "bin\initdb.exe") -D (Join-Path $smoke "data") -U postgres "--pwfile=$pw" --encoding=UTF8 --locale=C | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "initdb failed" }
    & (Join-Path $pgDir "bin\pg_ctl.exe") -D (Join-Path $smoke "data") -l (Join-Path $smoke "pg.log") -o "-p 55432" -w start
    try {
        & (Join-Path $pgDir "bin\psql.exe") -h 127.0.0.1 -p 55432 -U postgres -d postgres -v ON_ERROR_STOP=1 `
            -c "CREATE EXTENSION postgis; CREATE EXTENSION vector;" `
            -c "CREATE TABLE t(e vector(4)); CREATE INDEX ON t USING hnsw (e vector_cosine_ops);"
        if ($LASTEXITCODE -ne 0) { throw "PostGIS/pgvector smoke test failed" }
    } finally {
        & (Join-Path $pgDir "bin\pg_ctl.exe") -D (Join-Path $smoke "data") -m fast stop | Out-Null
        Remove-Item -Recurse -Force $smoke -ErrorAction SilentlyContinue
    }
    Write-Host "PostgreSQL runtime OK."
}

$mamba = Get-Micromamba
if (-not $PostgresOnly) { Invoke-PythonRuntime $mamba }
if (-not $PythonOnly) { Invoke-PostgresRuntime $mamba }

Write-Host ""
Write-Host "Runtime ready:"
Write-Host "  $RuntimeDir\py"
Write-Host "  $RuntimeDir\pgsql"
