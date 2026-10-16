@echo off
setlocal
chcp 65001 >nul
title Aurora PDF - bygg .exe
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo   Kjør 1_INSTALLER.bat først.
  pause
  exit /b 1
)
set "VPY=.venv\Scripts\python.exe"
"%VPY%" -m pip install --disable-pip-version-check -q pyinstaller==6.22.3 || goto :fail
"%VPY%" -m PyInstaller --noconfirm --clean --windowed --name "Aurora PDF" --icon "web\assets\aurora.ico" --add-data "web;web" --add-data "resources;resources" main.py || goto :fail
rem The licence texts go with the program: AGPL-3.0 and those of the parts it is made of
copy /y LICENSE "dist\Aurora PDF\" >nul || goto :fail
copy /y THIRD_PARTY_NOTICES.md "dist\Aurora PDF\" >nul || goto :fail
echo.
echo   Ferdig: dist\Aurora PDF\Aurora PDF.exe
echo   Mappen dist\Aurora PDF kan kopieres til en annen PC uten Python. Den PC-en trenger WebView2.
pause
exit /b 0

:fail
echo.
echo   Byggingen feilet. Se meldingene over.
pause
exit /b 1
