param(
    [string]$Version = "0.6.9rc10"
)

$ErrorActionPreference = "Stop"
if ($Version -notmatch '^[0-9A-Za-z][0-9A-Za-z._-]*$') {
    throw "Version must contain only letters, digits, dots, underscores, or hyphens."
}
$project = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$bundle = Join-Path $project "dist\PDF书签工具"
$portable = Join-Path $project "dist\portable"
$zip = Join-Path $portable "PDF书签工具-免安装版-$Version.zip"
$partial = "$zip.partial"

foreach ($filename in @("PDF书签工具.exe", "PDF书签命令行.exe")) {
    if (-not (Test-Path -LiteralPath (Join-Path $bundle $filename) -PathType Leaf)) {
        throw "Missing bundled executable: $filename. Build the PyInstaller onedir bundle first."
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $bundle "_internal") -PathType Container)) {
    throw "Missing bundled dependencies: $bundle\_internal"
}
$pdfium = Join-Path $bundle "_internal\pypdfium2_raw\pdfium.dll"
if (-not (Test-Path -LiteralPath $pdfium -PathType Leaf)) {
    throw "Missing bundled PDFium DLL: $pdfium"
}
if (Test-Path -LiteralPath (Join-Path $bundle "_internal\tools")) {
    throw "The bundle still contains old external PDF tools. Rebuild before packaging."
}

$portableReadme = Get-Content -LiteralPath (Join-Path $PSScriptRoot "PORTABLE_README.md") -Raw -Encoding UTF8
$portableReadme = $portableReadme.Replace("{{VERSION}}", $Version)
Set-Content -LiteralPath (Join-Path $bundle "README.md") -Value $portableReadme -Encoding UTF8
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "THIRD_PARTY_NOTICES.md") -Destination (Join-Path $bundle "THIRD_PARTY_NOTICES.md") -Force
$bundleLicenses = Join-Path $bundle "licenses"
New-Item -ItemType Directory -Path $bundleLicenses -Force | Out-Null
$licenseNames = @(
    "CPython-LICENSE.txt", "Tcl-Tk-license.terms.txt", "OpenSSL-LICENSE.txt",
    "libffi-LICENSE.txt", "cffi-LICENSE.txt", "pypdf-LICENSE.txt",
    "pdfplumber-LICENSE.txt", "pdfminer.six-LICENSE.txt", "Pillow-LICENSE.txt",
    "cryptography-LICENSE.txt", "cryptography-LICENSE.APACHE.txt",
    "cryptography-LICENSE.BSD.txt", "charset-normalizer-LICENSE.txt",
    "PyInstaller-COPYING.txt", "PyInstaller-hooks-contrib-LICENSE.txt",
    "Tabler-Icons-LICENSE.txt"
)
foreach ($filename in $licenseNames) {
    $source = Join-Path (Join-Path $PSScriptRoot "licenses") $filename
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Missing license file: $source"
    }
    Copy-Item -LiteralPath $source -Destination $bundleLicenses -Force
}
$pdfiumLicenses = Join-Path $PSScriptRoot "licenses\pypdfium2"
if (-not (Test-Path -LiteralPath $pdfiumLicenses -PathType Container)) {
    throw "Missing PDFium license directory: $pdfiumLicenses"
}
Copy-Item -LiteralPath $pdfiumLicenses -Destination $bundleLicenses -Recurse -Force
New-Item -ItemType Directory -Path $portable -Force | Out-Null
Add-Type -AssemblyName System.IO.Compression.FileSystem
if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Force }
try {
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $bundle,
        $partial,
        [System.IO.Compression.CompressionLevel]::Optimal,
        $true
    )
    if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
    Move-Item -LiteralPath $partial -Destination $zip
} catch {
    if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Force }
    throw
}
Write-Host "Portable ZIP: $zip"
