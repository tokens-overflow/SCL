@echo off
chcp 65001 >nul
REM Maps Deep Research Agent - double-click first-time setup
REM Real logic lives in setup.ps1
REM Prefer pwsh (PowerShell 7+) for UTF-8 + modern syntax; fall back to powershell 5.x

where pwsh >nul 2>nul
if %errorlevel%==0 (
  set "PS_EXE=pwsh"
) else (
  set "PS_EXE=powershell"
)
%PS_EXE% -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
echo.
pause
