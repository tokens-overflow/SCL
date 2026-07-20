@echo off
REM 双击启动「无边框原生窗口」版 Claude Code 2007（Windows）。
REM 窗口的最小化/最大化/关闭是真正的软件窗口操作，不是浏览器的。
cd /d "%~dp0"
python app_native.py
pause
