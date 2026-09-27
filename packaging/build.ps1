param(
    [string]$Python = "python",
    [string]$Version = "0.6.10",
    # Kept so existing build commands continue to work; this script always
    # creates the portable ZIP and never builds an installer.
    [switch]$PortableOnly
)

$ErrorActionPreference = "Stop"
$project = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $project

$pythonVersion = & $Python -c 'import sys; print(".".join(map(str, sys.version_info[:3])))'
if ($LASTEXITCODE -ne 0 -or $pythonVersion -notmatch '^3\.12\.') {
    throw "Build with a Python 3.12 virtual environment containing packaging/release-requirements.txt. Found: $pythonVersion"
}
& $Python -m pip check
if ($LASTEXITCODE -ne 0) { throw "Python release environment has broken dependencies." }

$previousConfigDir = $env:PYINSTALLER_CONFIG_DIR
$env:PYINSTALLER_CONFIG_DIR = Join-Path $project "tmp\pyinstaller-config"
try {
    New-Item -ItemType Directory -Path $env:PYINSTALLER_CONFIG_DIR -Force | Out-Null
    & $Python -m PyInstaller --noconfirm --clean --distpath dist --workpath tmp\pyinstaller-build packaging\PDFBookmarker.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
} finally {
    $env:PYINSTALLER_CONFIG_DIR = $previousConfigDir
}

& (Join-Path $PSScriptRoot "package_portable.ps1") -Version $Version
