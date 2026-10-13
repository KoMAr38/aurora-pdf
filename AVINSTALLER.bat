@echo off
setlocal
chcp 65001 >nul
title Aurora PDF - avinstaller
cd /d "%~dp0"
set "APPDIR=%~dp0"
set "APPDIR=%APPDIR:~0,-1%"
echo.
echo   Flytter snarveiene til papirkurven og fjerner Aurora PDF fra «Åpne med».
echo   Programmappen og innstillingene i %APPDATA%\AuroraPDF blir liggende.
echo.
choice /c JN /n /m "  Fortsette? [J/N] "
if errorlevel 2 exit /b 0
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\windows.ps1" -App "%APPDIR%" -Action remove
pause
