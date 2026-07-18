#!/bin/bash
# macOS 双击启动:开服务并自动打开浏览器
cd "$(dirname "$0")"
( sleep 1.5; open "http://localhost:8787" ) &
python3 server.py
