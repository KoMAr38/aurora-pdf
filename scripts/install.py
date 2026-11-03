"""install.exe and uninstall.exe for Aurora PDF, built from this one file by scripts/bygg.bat.

install.exe carries the finished program (PyInstaller folder "app"). It puts it in
%LOCALAPPDATA%\\Programs\\Aurora PDF, runs the installation check, adds the shortcuts, the
"Open with" entry and the entry in Settings > Apps, and starts the program.
uninstall.exe removes the program, its shortcuts and its registry entries. The user's
settings, signatures and Digital IDs in %APPDATA%\\AuroraPDF stay: they are not the program.
Both work for the current user only, without administrator rights.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

NAME = "Aurora PDF"
TARGET = Path(os.environ["LOCALAPPDATA"]) / "Programs" / NAME
EXE = TARGET / f"{NAME}.exe"
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
# the program is another frozen app: it must not take this one's PyInstaller settings for its own
CHILD_ENV = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")


def fail(text: str) -> None:
    print(text)
    ctypes.windll.user32.MessageBoxW(None, text, NAME, 0x10)
    sys.exit(1)


def running() -> bool:
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {NAME}.exe", "/NH"], capture_output=True, text=True).stdout
    return f"{NAME}.exe".lower() in out.lower()


def shell(action: str) -> None:
    """Shortcuts and registry entries: scripts/windows.ps1, the same file the tests check."""
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(BUNDLE / "windows.ps1"),
                        "-App", str(TARGET), "-Action", action])
    if r.returncode:
        fail(f"Snarveiene og registeret kunne ikke oppdateres ({action}). Se meldingene i vinduet.")


def ours(folder: Path) -> bool:
    """The program folder, also after a removal that stopped halfway on a locked file."""
    return any((folder / n).exists() for n in (f"{NAME}.exe", "uninstall.exe", "_internal")) or not any(folder.iterdir())


def install() -> None:
    print(f"{NAME} - installasjon\n")
    for _ in range(30):  # an update in the program starts us just before the program closes itself
        if not running():
            break
        time.sleep(0.5)
    else:
        fail(f"Lukk {NAME} først, og kjør install.exe på nytt.")
    if TARGET.exists():
        if not ours(TARGET):
            fail(f"{TARGET} finnes allerede og er ikke {NAME}. Ingenting er endret.")
        print("  Fjerner den gamle versjonen")
        shutil.rmtree(TARGET)
    print(f"  Kopierer programmet til {TARGET}")
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(BUNDLE / "app"), str(TARGET))  # a rename when the temp folder is on the same drive
    shutil.copy2(BUNDLE / "uninstall.exe", TARGET / "uninstall.exe")
    print("  Sjekker at alt virker")
    if subprocess.run([str(EXE), "--selftest"], env=CHILD_ENV).returncode:
        report = Path(os.environ["APPDATA"]) / "AuroraPDF" / "selftest.txt"
        fail(f"Installasjonssjekken fant feil. Rapporten ligger i {report}")
    print("  Lager snarveier og «Åpne med»")
    shell("install")
    subprocess.Popen([str(EXE)], cwd=TARGET, env=CHILD_ENV)
    print("\nFerdig.")


def uninstall() -> None:
    print(f"{NAME} - avinstallering\n")
    if running():
        fail(f"Lukk {NAME} først, og kjør uninstall.exe på nytt.")
    shell("remove")
    inside = TARGET in Path(sys.executable).resolve().parents
    if TARGET.exists() and not inside:
        shutil.rmtree(TARGET)
    ctypes.windll.user32.MessageBoxW(
        None, f"{NAME} er fjernet.\n\nInnstillinger, signaturer og digitale ID-er ligger fortsatt i "
              f"{Path(os.environ['APPDATA']) / 'AuroraPDF'}.", NAME, 0x40)
    if TARGET.exists() and inside:
        # this exe lives in the folder and cannot delete itself: a detached cmd does it once we have exited
        subprocess.Popen(f'cmd /c ping -n 3 127.0.0.1 >nul & rd /s /q "{TARGET}"',
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW)


if __name__ == "__main__":
    try:
        uninstall() if Path(sys.argv[0]).stem.lower().startswith("uninstall") else install()
    except Exception as exc:  # the console closes at once: without a message box the user sees only a flash
        fail(f"Noe gikk galt: {exc}\n\nKjør filen på nytt. Hjelper ikke det, start PC-en på nytt først.")
