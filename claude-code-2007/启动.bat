@echo off
rem Windows 双击启动:开服务并自动打开浏览器
chcp 65001 >nul
cd /d "%~dp0"
start "" cmd /c "ping -n 3 127.0.0.1 >nul & start http://localhost:8787"
where python >nul 2>nul && ( python server.py ) || ( py -3 server.py )
pause
