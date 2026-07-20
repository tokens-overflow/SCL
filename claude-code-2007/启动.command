#!/bin/bash
# 双击这个文件即可启动「无边框原生窗口」版 Claude Code 2007。
# 窗口的 🗕/🗖/✕ 是真正的软件窗口最小化/最大化/关闭（像 QQ 那样），不是浏览器的。
cd "$(dirname "$0")"
exec python3 app_native.py
