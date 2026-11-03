@echo off
rem Builds Installer\install.exe and Installer\uninstall.exe (scripts\install.py) around the program
rem made by PyInstaller. The PC that runs install.exe needs neither Python nor internet.
setlocal
chcp 65001 >nul
title Aurora PDF - bygg install.exe
cd /d "%~dp0.."
set "ROOT=%CD%"
if exist ".venv\Scripts\python.exe" goto :have_venv
rem only 3.11-3.14: "py -3" would take the newest Python, which the pinned libraries may not support yet
set "PY="
for %%v in (3.13 3.12 3.14 3.11) do if not defined PY py -%%v -c "pass" >nul 2>&1 && set "PY=py -%%v"
if not defined PY (
  echo   Fant ingen Python 3.11-3.14. Installer Python 3.13 fra https://www.python.org/downloads/
  goto :fail
)
echo   Lager .venv med %PY%
%PY% -m venv .venv || goto :fail
:have_venv
set "VPY=%ROOT%\.venv\Scripts\python.exe"
echo   Installerer bibliotekene og PyInstaller
"%VPY%" -m pip install --disable-pip-version-check -q -r requirements.txt pyinstaller==6.22.3 || goto :fail
echo   Sjekker at alt virker
"%VPY%" main.py --selftest || goto :fail
set "OUT=%ROOT%\build\dist"
set PYI="%VPY%" -m PyInstaller --noconfirm --clean --log-level WARN --workpath "%ROOT%\build\work" --specpath "%ROOT%\build" --distpath "%OUT%" --icon "%ROOT%\web\assets\aurora.ico"
echo   Bygger programmet
%PYI% --windowed --name "Aurora PDF" --add-data "%ROOT%\web;web" --add-data "%ROOT%\resources;resources" --collect-submodules fontTools.ttLib.tables "%ROOT%\main.py" || goto :fail
rem The licence texts go with the program: AGPL-3.0 and those of the parts it is made of
copy /y LICENSE "%OUT%\Aurora PDF\" >nul || goto :fail
copy /y THIRD_PARTY_NOTICES.md "%OUT%\Aurora PDF\" >nul || goto :fail
echo   Bygger uninstall.exe og install.exe
%PYI% --onefile --name uninstall --add-data "%ROOT%\scripts\windows.ps1;." "%ROOT%\scripts\install.py" || goto :fail
%PYI% --onefile --name install --add-data "%ROOT%\scripts\windows.ps1;." --add-data "%OUT%\uninstall.exe;." --add-data "%OUT%\Aurora PDF;app" "%ROOT%\scripts\install.py" || goto :fail
if not exist "Installer" mkdir "Installer"
copy /y "%OUT%\install.exe" "Installer\" >nul || goto :fail
copy /y "%OUT%\uninstall.exe" "Installer\" >nul || goto :fail
echo.
echo   Ferdig: Installer\install.exe og Installer\uninstall.exe
pause
exit /b 0

:fail
echo.
echo   Byggingen feilet. Se meldingene over.
pause
exit /b 1
