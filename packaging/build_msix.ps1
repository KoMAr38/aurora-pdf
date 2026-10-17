# Builds the MSIX package for the Microsoft Store: PyInstaller folder + AppxManifest + icons -> dist\AuroraPDF_<version>_x64.msix
# Runs on GitHub Actions (.github/workflows/store.yml) and on any Windows PC with Python and the Windows SDK.
# The package is not signed: the Store signs it. store.yml signs a copy with a throw-away certificate to test the install.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$manifest = [xml](Get-Content -Raw -Encoding UTF8 'packaging\AppxManifest.xml')
$version = $manifest.Package.Identity.Version
Write-Host "Aurora PDF $version"

# the newest Windows SDK that has the packaging tools
$sdk = Get-ChildItem 'C:\Program Files (x86)\Windows Kits\10\bin\*\x64\makeappx.exe' |
    Sort-Object { [version]$_.Directory.Parent.Name } -Descending | Select-Object -First 1
if (-not $sdk) { throw 'Windows SDK (makeappx.exe) not found' }
$tools = $sdk.DirectoryName
Write-Host "Windows SDK tools: $tools"

function Clear-Folder($path) {
    # an old build is moved aside, never deleted
    if (Test-Path $path) { Move-Item $path ("_to_delete\" + (Split-Path -Leaf $path) + '-' + (Get-Date -Format 'yyyyMMdd-HHmmss')) }
}
New-Item -ItemType Directory -Force '_to_delete' | Out-Null

function Invoke-Tool($exe, [string[]]$arguments) {
    & (Join-Path $tools $exe) @arguments
    if ($LASTEXITCODE -ne 0) { throw "$exe failed ($LASTEXITCODE)" }
}

# 1. the program as a folder of files, the same way 3_BYGG_EXE.bat makes it
python -m PyInstaller --noconfirm --clean --windowed --name AuroraPDF --icon 'web\assets\aurora.ico' `
    --add-data 'web;web' --add-data 'resources;resources' main.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }

# 2. the package layout: program, licence texts, manifest, icons
$layout = 'dist\msix'
Clear-Folder $layout
Copy-Item -Recurse 'dist\AuroraPDF' $layout
Copy-Item 'LICENSE', 'THIRD_PARTY_NOTICES.md' $layout
Copy-Item 'packaging\AppxManifest.xml' $layout
Copy-Item -Recurse 'packaging\assets' (Join-Path $layout 'Assets')
# python-docx ships the unpacked source of its default.docx. Its [Content_Types].xml is a name the package format
# keeps for itself (makeappx: 0x8007007b), and python-docx reads only default.docx
$docxSource = Join-Path $layout '_internal\docx\templates\default-docx-template'
if (Test-Path $docxSource) { Move-Item $docxSource ('_to_delete\default-docx-template-' + (Get-Date -Format 'yyyyMMdd-HHmmss')) }

# 3. resources.pri, so Windows picks the icon size and the unplated taskbar icons. Only the icons are indexed:
#    the program's own files are not resources
$pri = 'dist\pri'
Clear-Folder $pri
New-Item -ItemType Directory $pri | Out-Null
Copy-Item -Recurse 'packaging\assets' (Join-Path $pri 'Assets')
Invoke-Tool 'makepri.exe' @('createconfig', '/cf', 'dist\priconfig.xml', '/dq', 'en-US', '/pv', '10.0.0', '/o')
Invoke-Tool 'makepri.exe' @('new', '/pr', $pri, '/cf', 'dist\priconfig.xml', '/mn', "$layout\AppxManifest.xml",
    '/of', "$layout\resources.pri", '/o')

# 4. the package
$msix = "dist\AuroraPDF_${version}_x64.msix"
Invoke-Tool 'makeappx.exe' @('pack', '/d', $layout, '/p', $msix, '/o')
Write-Host "Package: $msix ($([math]::Round((Get-Item $msix).Length / 1MB)) MB)"
