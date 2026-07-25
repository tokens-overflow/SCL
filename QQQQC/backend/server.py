#!/usr/bin/env python3
"""Compatibility entrypoint for the modular Claude Code 2007 backend."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from backend.api import AppContext, QuietHTTPServer, make_handler, open_app_window, serve

# server.py 在 backend/ 下，工程根是 parents[1]（config.json / data / frontend 都在根）
BASE = Path(__file__).resolve().parents[1]
APP = AppContext(BASE)
CONFIG = APP.config
MANAGER = APP.task_service
SCHEDULER = APP.scheduler
FRIENDS = APP.friends
PROFILE = APP.profile.get()
MOMENTS = APP.moments
Handler = make_handler(APP)


def main() -> None:
    arguments = list(sys.argv[1:])
    app_mode = "--app" in arguments
    ports = [int(value) for value in arguments if value.isdigit()]
    port = ports[0] if ports else 8787
    if shutil.which(APP.cli.executable_name) is None and APP.cli.executable_name == "claude":
        print("[警告] 找不到 `claude`，请先安装并登录 Claude Code CLI")
    serve(APP, port, app_mode=app_mode)


if __name__ == "__main__":
    main()
