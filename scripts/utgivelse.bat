@echo off
rem Publishes a new version for the update button: builds install.exe (scripts\bygg.bat) and puts it on
rem GitHub Releases as v<VERSION>. First raise VERSION in core\paths.py and the Version in
rem packaging\AppxManifest.xml (a test keeps the two equal). Needs the GitHub CLI (gh auth login).
setlocal
chcp 65001 >nul
title Aurora PDF - ny versjon
cd /d "%~dp0.."
where gh >nul 2>&1 || (echo   Installer GitHub CLI fra https://cli.github.com og kjør gh auth login & goto :fail)
rem a pause inside a called script would end this one too
set "NOPAUSE=1"
call "%~dp0bygg.bat" || goto :fail
rem no quote at the start of the command: for /f would strip it and break the call
for /f "delims=" %%v in ('.venv\Scripts\python.exe -c "from core.paths import VERSION; print(VERSION)"') do set "V=%%v"
if not defined V goto :fail
echo   Legger ut v%V% på GitHub
gh release create "v%V%" "install.exe" "uninstall.exe" --title "Aurora PDF %V%" --generate-notes || goto :fail
echo.
echo   Ferdig: v%V% ligger på GitHub, og «Se etter oppdateringer» i programmet finner den.
pause
exit /b 0

:fail
echo.
echo   Utgivelsen feilet. Se meldingene over.
pause
exit /b 1
