@echo off
chcp 65001 >nul
REM Q-CC - Claude Code (QQ2007 skin) launcher
REM entry moved to backend/, run as a module from project root so package imports work
cd /d "%~dp0"
python -m backend.app_native
pause
