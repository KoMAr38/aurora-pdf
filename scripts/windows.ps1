# Shortcuts and the PDF registration for Aurora PDF, for the current user only.
#   install  : Desktop + Start menu shortcuts; with -OpenWith Aurora PDF is also offered for .pdf,
#              in "Open with" and in Settings > Default apps
#   remove   : moves the shortcuts to the Recycle Bin and removes the registry entries
# Never changes which program opens PDF files by default; Windows leaves that choice to the user.
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
$appId = 'Aurora.PDF.Desktop'  # APP_ID in main.py: the taskbar ties window, shortcuts and PDF handler together by it
$name = 'Aurora PDF'  # must match the RegisteredApplications value name
$caps = 'Software\AuroraPDF\Capabilities'
$about = 'PDF-leser med passord, vannmerke, kommentarer, signatur og OCR'
$hkcu = [Microsoft.Win32.Registry]::CurrentUser

function Update-Shell {
    # tells Explorer the associations changed, so menus and icons refresh without a restart
    Add-Type -Namespace Aurora -Name Shell -MemberDefinition '[DllImport("shell32.dll")] public static extern void SHChangeNotify(int e, uint f, IntPtr a, IntPtr b);'
    [Aurora.Shell]::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero)
}

if ($Action -eq 'install') {
    # WScript.Shell cannot write System.AppUserModel.ID on a shortcut; the shortcut's property store can
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

namespace Aurora {
    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPropertyStore {
        [PreserveSig] int GetCount(out uint count);
        [PreserveSig] int GetAt(uint index, IntPtr key);
        [PreserveSig] int GetValue(IntPtr key, IntPtr value);
        [PreserveSig] int SetValue(ref PropertyKey key, ref PropVariant value);
        [PreserveSig] int Commit();
    }

    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    struct PropertyKey { public Guid FormatId; public uint PropertyId; }

    [StructLayout(LayoutKind.Sequential)]
    struct PropVariant { public ushort Type; public ushort R1, R2, R3; public IntPtr Value, Value2; }

    public static class Link {
        public static void SetAppId(string path, string appId) {
            object link = Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("00021401-0000-0000-C000-000000000046")));
            try {
                IPersistFile file = (IPersistFile)link;
                file.Load(path, 2);  // STGM_READWRITE
                PropertyKey key = new PropertyKey { FormatId = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), PropertyId = 5 };
                PropVariant value = new PropVariant { Type = 31, Value = Marshal.StringToCoTaskMemUni(appId) };  // VT_LPWSTR
                try {
                    IPropertyStore store = (IPropertyStore)link;
                    Marshal.ThrowExceptionForHR(store.SetValue(ref key, ref value));
                    Marshal.ThrowExceptionForHR(store.Commit());
                } finally {
                    Marshal.FreeCoTaskMem(value.Value);
                }
                file.Save(path, true);
            } finally {
                Marshal.ReleaseComObject(link);
            }
        }
    }
}
'@
    $shell = New-Object -ComObject WScript.Shell
    foreach ($lnk in $links) {
        $s = $shell.CreateShortcut($lnk)
        $s.TargetPath = $pyw
        $s.Arguments = '"' + $main + '"'
        $s.WorkingDirectory = $App
        $s.IconLocation = $icon
        $s.Description = 'Aurora PDF'
        $s.Save()
        # without the window's ID on the shortcut, pinning the running window pins pythonw.exe itself
        [Aurora.Link]::SetAppId($lnk, $appId)
    }
    Write-Host '  Snarveier: skrivebordet og Start-menyen'
    if ($OpenWith) {
        # CreateSubKey opens an existing key without touching what other programs put there
        $k = $hkcu.CreateSubKey("Software\Classes\$progId")
        $k.SetValue('', 'PDF (Aurora PDF)')
        $k.SetValue('AppUserModelID', $appId)
        $k.CreateSubKey('DefaultIcon').SetValue('', $icon)
        $k.CreateSubKey('shell\open\command').SetValue('', '"' + $pyw + '" "' + $main + '" "%1"')
        # the name and icon Windows shows for this handler; without them it takes both from pythonw.exe
        $a = $k.CreateSubKey('Application')
        $a.SetValue('ApplicationName', $name)
        $a.SetValue('ApplicationIcon', "$icon,0")
        $a.SetValue('ApplicationDescription', $about)
        $a.Close()
        $k.Close()
        $k = $hkcu.CreateSubKey('Software\Classes\.pdf\OpenWithProgids')
        $k.SetValue($progId, [byte[]]@(), [Microsoft.Win32.RegistryValueKind]::None)
        $k.Close()
        # Settings > Default apps only lists programs registered here
        $k = $hkcu.CreateSubKey($caps)
        $k.SetValue('ApplicationName', $name)
        $k.SetValue('ApplicationDescription', $about)
        $k.SetValue('ApplicationIcon', "$icon,0")
        $k.CreateSubKey('FileAssociations').SetValue('.pdf', $progId)
        $k.Close()
        $k = $hkcu.CreateSubKey('Software\RegisteredApplications')
        $k.SetValue($name, $caps)
        $k.Close()
        Update-Shell
        Write-Host '  PDF: Aurora PDF finnes nå under «Åpne med» og i Innstillinger > Standardapper'
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
    $k = $hkcu.OpenSubKey('Software\RegisteredApplications', $true)
    if ($k) { $k.DeleteValue($name, $false); $k.Close() }
    $hkcu.DeleteSubKeyTree('Software\AuroraPDF', $false)
    Update-Shell
    Write-Host '  Snarveiene er flyttet til papirkurven, og Aurora PDF er fjernet fra «Åpne med» og standardappene'
}
