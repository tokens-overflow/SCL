@echo off
chcp 65001 >nul
REM Maps Deep Research Agent - double-click start (backend + frontend + browser)
REM Real logic lives in start.ps1
REM Prefer pwsh (PowerShell 7+) for UTF-8 + modern syntax; fall back to powershell 5.x

where pwsh >nul 2>nul
if %errorlevel%==0 (
  set "PS_EXE=pwsh"
) else (
  set "PS_EXE=powershell"
)
%PS_EXE% -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
echo.
echo (backend / frontend are running in two new windows; you can close this one)
pause
