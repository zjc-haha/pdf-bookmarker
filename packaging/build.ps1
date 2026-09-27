param(
    [string]$Python = "python",
    # Kept so existing build commands continue to work; this script always
    # creates the portable ZIP and never builds an installer.
    [switch]$PortableOnly
)

$ErrorActionPreference = "Stop"
$project = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $project

$pythonVersion = & $Python -c 'import sys; print(".".join(map(str, sys.version_info[:3])))'
if ($LASTEXITCODE -ne 0 -or $pythonVersion -notmatch '^3\.12\.') {
    throw "Build 0.6.8 with a Python 3.12 virtual environment containing packaging/release-requirements.txt. Found: $pythonVersion"
}
& $Python -m pip check
if ($LASTEXITCODE -ne 0) { throw "Python release environment has broken dependencies." }

& $Python -m PyInstaller --noconfirm --clean --distpath dist --workpath tmp\pyinstaller-build packaging\PDFBookmarker.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

& (Join-Path $PSScriptRoot "package_portable.ps1")
