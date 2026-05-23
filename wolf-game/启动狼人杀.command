#!/bin/bash
# macOS 双击启动入口：Finder 双击会在 Terminal 里跑这个脚本
# 切到脚本所在目录，再启动 Python launcher（会拉起 node proxy + pywebview 窗口）
cd "$(dirname "$0")" || exit 1
exec python3 launcher/launcher.py
