@echo off
rem Starter Aurora PDF med et konsollvindu som viser feilmeldinger. Til vanlig brukes snarveien.
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo   Kjør 1_INSTALLER.bat først.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" main.py %*
if errorlevel 1 pause
