#!/usr/bin/env python3
"""QQQQC —— 无边框原生窗口版。

用 pywebview 把界面套进一个 frameless 的原生窗口：没有 macOS 那条灰色标题栏，
只剩下界面里那条蓝色 QQ 风标题栏，效果跟当年的 QQ 一模一样。窗口拖动靠标题栏
（.pywebview-drag-region），最小化 / 最大化 / 关闭三个按钮走 pywebview API。

    python3 app_native.py [端口]      # 默认 8787

需要 pywebview：pip install --user pywebview（mac 上依赖 pyobjc，通常已自带）。
不想装依赖就用 `python3 server.py --app`（Chrome app 模式，但会带 macOS 标题栏）。
"""

import os
import sys
import threading
from pathlib import Path

import server  # 复用同一个后端（导入即完成 MANAGER / CONFIG / SCHEDULER 初始化）

ICON_PATH = str(Path(__file__).resolve().parent / "icon.png")


def _set_dock_icon():
    """mac：把 Dock 图标换成企鹅（默认是 Python 火箭）。用 pyobjc 在主线程替换。"""
    if sys.platform != "darwin" or not os.path.exists(ICON_PATH):
        return
    try:
        from AppKit import NSApplication, NSImage
        from PyObjCTools import AppHelper

        def apply():
            img = NSImage.alloc().initByReferencingFile_(ICON_PATH)
            if img:
                NSApplication.sharedApplication().setApplicationIconImage_(img)
        AppHelper.callAfter(apply)   # 切到主线程执行，确保生效
    except Exception:
        pass


class WindowApi:
    """暴露给前端 JS 的窗口控制（window.pywebview.api.*）。"""

    def __init__(self):
        self.window = None

    def minimize(self):
        if self.window:
            self.window.minimize()

    def set_bounds(self, x, y, w, h):
        # 前端算好目标位置和大小（避开菜单栏/程序坞的工作区），这里 resize + move。
        # 不用 pywebview 的 maximize/restore：maximize 只 resize 不挪位置会顶出屏幕，
        # restore 其实是「从最小化恢复」，都不符合需求。
        if self.window:
            self.window.resize(int(w), int(h))
            self.window.move(int(x), int(y))

    def get_workarea(self):
        # 返回主屏「可用工作区」(去掉菜单栏/程序坞) 的 [x, y, w, h]，
        # 其中 x,y 已换算成 move() 用的「左上原点、y 向下」坐标，前端直接喂给 set_bounds。
        try:
            from AppKit import NSScreen
            scr = NSScreen.mainScreen()
            vf = scr.visibleFrame()   # cocoa 底左原点，已排除菜单栏/程序坞
            full = scr.frame()
            x = vf.origin.x
            y = full.size.height - (vf.origin.y + vf.size.height)
            return [int(x), int(y), int(vf.size.width), int(vf.size.height)]
        except Exception:
            return None

    def close(self):
        # 单窗口应用，关闭即退出。pywebview 6.1 在 mac 上 destroy() 会卡住主线程（转圈关不掉），
        # 所以直接在独立线程里强制退出进程——窗口随进程一起消失，100% 关得掉。
        threading.Timer(0.12, lambda: os._exit(0)).start()

    def move(self, x, y):
        # move() 用「左上原点、y 向下」的屏幕坐标（和前端 screenX/screenY 一致），
        # 前端只用鼠标坐标算好绝对位置传进来，不读 window.x/y（那个在 mac 上有坐标系 bug）。
        if self.window:
            self.window.move(int(x), int(y))


def start_server(port):
    srv = server.QuietHTTPServer(("127.0.0.1", port), server.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _run_in_browser(port):
    """没装 pywebview 时的兜底：起服务 + 用浏览器 app 模式打开（保证一定能用）。"""
    srv = server.QuietHTTPServer(("127.0.0.1", port), server.Handler)
    srv.daemon_threads = True
    url = f"http://localhost:{port}"
    print(f"未检测到 pywebview，改用浏览器打开：{url}")
    print("（想要无边框窗口效果，可执行：pip install --user pywebview）")
    threading.Timer(0.6, server.open_app_window, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    ports = [a for a in sys.argv[1:] if a.isdigit()]
    port = int(ports[0]) if ports else 8787

    try:
        import webview
    except ImportError:
        _run_in_browser(port)   # 傻瓜化：没装 pywebview 也能一键跑起来
        return

    start_server(port)
    print(f"QQQQC（无边框）已启动: http://localhost:{port}")

    api = WindowApi()
    api.window = webview.create_window(
        "QQQQC",
        f"http://localhost:{port}",
        width=980, height=760, min_size=(720, 520),
        frameless=True,        # 去掉原生边框和标题栏
        easy_drag=False,       # 拖动交给 .pywebview-drag-region（标题栏）
        background_color="#1450b8",
        js_api=api,
    )
    # mac 的 Dock 图标要等 NSApp 起来后再换，用 Timer 延迟设置
    threading.Timer(1.2, _set_dock_icon).start()
    # Windows/GTK 的窗口/任务栏图标直接传给 start（mac 会忽略这个参数）
    try:
        webview.start(icon=ICON_PATH)
    except TypeError:
        webview.start()   # 老版本 pywebview 没有 icon 参数


if __name__ == "__main__":
    main()
