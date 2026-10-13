@echo off
setlocal EnableExtensions
chcp 65001 >nul
title Aurora PDF - installasjon
cd /d "%~dp0"
set "APP=%~dp0"
set "APPDIR=%APP:~0,-1%"
set "VERCHECK=import sys; sys.exit(not (3, 11) <= sys.version_info[:2] <= (3, 14))"

echo.
echo   Aurora PDF - installasjon
echo   =========================
echo.
echo   [1/4] Ser etter Python 3.11-3.14
set "PY="
for %%v in (3.13 3.12 3.14 3.11) do if not defined PY py -%%v -c "pass" >nul 2>&1 && set "PY=py -%%v"
if not defined PY python -c "%VERCHECK%" >nul 2>&1 && set "PY=python"
if defined PY goto :have_python

echo         Fant ingen passende Python.
where winget >nul 2>&1 || goto :no_python
choice /c JN /n /m "        Installere Python 3.13 nå, bare for denne brukeren? [J/N] "
if errorlevel 2 goto :no_python
winget install -e --id Python.Python.3.13 --scope user --accept-package-agreements --accept-source-agreements
set "PY313=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PY313%" goto :no_python
set PY="%PY313%"

:have_python
echo         %PY%

echo   [2/4] Lager .venv
if not exist ".venv\Scripts\python.exe" goto :make_venv
".venv\Scripts\python.exe" -c "%VERCHECK%" >nul 2>&1 && goto :have_venv
rem An environment from an unsupported Python is moved aside, never deleted
if not exist "_to_delete" mkdir "_to_delete"
move ".venv" "_to_delete\venv-%RANDOM%" >nul || goto :fail
echo         Den gamle .venv er flyttet til _to_delete
:make_venv
%PY% -m venv .venv || goto :fail
:have_venv
set "VPY=%APP%.venv\Scripts\python.exe"

echo   [3/4] Installerer bibliotekene. Første gang tar det et par minutter.
"%VPY%" -m pip install --disable-pip-version-check --upgrade pip >nul
"%VPY%" -m pip install --disable-pip-version-check -r requirements.txt || goto :fail

echo   [4/4] Sjekker at alt virker
"%VPY%" main.py --selftest || goto :selftest_failed

echo.
choice /c JN /n /m "  Tilby Aurora PDF for PDF-filer («Åpne med» og standardapper)? [J/N] "
set "OPENWITH="
if not errorlevel 2 set "OPENWITH=-OpenWith"
powershell -NoProfile -ExecutionPolicy Bypass -File "%APP%scripts\windows.ps1" -App "%APPDIR%" -Action install %OPENWITH% || goto :fail
rem Windows lets only the user pick the default PDF program, so open Aurora PDF's page in Settings
if defined OPENWITH echo   Innstillinger åpnes nå. Velg Aurora PDF som standard for PDF der, Windows lar ikke programmer gjøre det selv.
if defined OPENWITH start "" "ms-settings:defaultapps?registeredAppUser=Aurora%%20PDF"

echo.
echo   Ferdig. Aurora PDF ligger på skrivebordet og i Start-menyen.
choice /c JN /n /m "  Starte Aurora PDF nå? [J/N] "
if errorlevel 2 exit /b 0
start "" "%APP%.venv\Scripts\pythonw.exe" "%APP%main.py"
exit /b 0

:no_python
echo.
echo   Installer Python 3.13 fra https://www.python.org/downloads/
echo   og kjør 1_INSTALLER.bat på nytt.
pause
exit /b 1

:selftest_failed
echo.
echo   Sjekken fant feil. Rapporten ligger i %APPDATA%\AuroraPDF\selftest.txt
pause
exit /b 1

:fail
echo.
echo   Noe gikk galt. Meldingene over viser hva.
pause
exit /b 1
