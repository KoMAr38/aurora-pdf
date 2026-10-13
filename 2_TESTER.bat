@echo off
setlocal
chcp 65001 >nul
title Aurora PDF - tester
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo   Kjør 1_INSTALLER.bat først.
  pause
  exit /b 1
)
set "VPY=.venv\Scripts\python.exe"
echo   Installerer pytest og Playwright
"%VPY%" -m pip install --disable-pip-version-check -q -r requirements-dev.txt || goto :fail
echo   Kjører testene. Ende-til-ende-testene styrer Microsoft Edge og tar et par minutter.
"%VPY%" -m pytest || goto :fail
pause
exit /b 0

:fail
echo.
echo   Noe feilet. Se meldingene over.
pause
exit /b 1
