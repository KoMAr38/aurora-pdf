# Shortcuts and the "Open with" entry for Aurora PDF, for the current user only.
#   install  : Desktop + Start menu shortcuts; with -OpenWith also an "Open with" entry for .pdf
#   remove   : moves the shortcuts to the Recycle Bin and removes the registry entries
# Never changes which program opens PDF files by default.
param(
    [Parameter(Mandatory = $true)][string]$App,
    [ValidateSet('install', 'remove')][string]$Action = 'install',
    [switch]$OpenWith
)
$ErrorActionPreference = 'Stop'
$App = $App.TrimEnd('\', '"', '.').TrimEnd('\')
$pyw = Join-Path $App '.venv\Scripts\pythonw.exe'
$main = Join-Path $App 'main.py'
$icon = Join-Path $App 'web\assets\aurora.ico'
$links = @(
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Aurora PDF.lnk'),
    (Join-Path ([Environment]::GetFolderPath('Programs')) 'Aurora PDF.lnk')
)
$progId = 'AuroraPDF.Document'
$hkcu = [Microsoft.Win32.Registry]::CurrentUser

if ($Action -eq 'install') {
    $shell = New-Object -ComObject WScript.Shell
    foreach ($lnk in $links) {
        $s = $shell.CreateShortcut($lnk)
        $s.TargetPath = $pyw
        $s.Arguments = '"' + $main + '"'
        $s.WorkingDirectory = $App
        $s.IconLocation = $icon
        $s.Description = 'Aurora PDF'
        $s.Save()
    }
    Write-Host '  Snarveier: skrivebordet og Start-menyen'
    if ($OpenWith) {
        # CreateSubKey opens an existing key without touching what other programs put there
        $k = $hkcu.CreateSubKey("Software\Classes\$progId")
        $k.SetValue('', 'PDF (Aurora PDF)')
        $k.CreateSubKey('DefaultIcon').SetValue('', $icon)
        $k.CreateSubKey('shell\open\command').SetValue('', '"' + $pyw + '" "' + $main + '" "%1"')
        $k.Close()
        $k = $hkcu.CreateSubKey('Software\Classes\.pdf\OpenWithProgids')
        $k.SetValue($progId, [byte[]]@(), [Microsoft.Win32.RegistryValueKind]::None)
        $k.Close()
        Write-Host '  «Åpne med»: Aurora PDF er lagt til for PDF-filer'
    }
}
else {
    Add-Type -AssemblyName Microsoft.VisualBasic
    foreach ($lnk in $links) {
        if (Test-Path -LiteralPath $lnk) {
            [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($lnk, 'OnlyErrorDialogs', 'SendToRecycleBin')
        }
    }
    $k = $hkcu.OpenSubKey('Software\Classes\.pdf\OpenWithProgids', $true)
    if ($k) { $k.DeleteValue($progId, $false); $k.Close() }
    $hkcu.DeleteSubKeyTree("Software\Classes\$progId", $false)
    Write-Host '  Snarveiene er flyttet til papirkurven, og Aurora PDF er fjernet fra «Åpne med»'
}
