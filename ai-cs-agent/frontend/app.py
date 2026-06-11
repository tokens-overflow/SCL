"""NiceGUI 前端（独立进程，通过 HTTP/WebSocket 调后端）。

启动：
    uv run python frontend/app.py     # 默认 http://127.0.0.1:8080

页面：
    /        聊天页：左侧对话窗口，右侧实时展示 agent 工具调用过程
    /admin   CRM 数据表实时视图（2 秒轮询，演示数据真的被改了）
"""
import json
import os

import httpx
import websockets
from nicegui import app, ui

BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000")
WS_URL = BACKEND_URL.replace("http://", "ws://").replace("https://", "wss://")


# ---------------------------------------------------------------------------
# 聊天页
# ---------------------------------------------------------------------------


@ui.page("/")
def chat_page():
    state = {"session_id": None, "busy": False}

    ui.page_title("AI 客服 Agent Demo")
    with ui.header().classes("bg-blue-9 items-center"):
        ui.label("云尚优选 · AI 客服 Agent").classes("text-lg font-bold")
        ui.space()
        ui.link("Admin 数据面板", "/admin", new_tab=True).classes("text-white")

    with ui.row().classes("w-full no-wrap").style("height: calc(100vh - 110px)"):
        # ---- 左：聊天窗口 ----
        with ui.column().classes("w-1/2 h-full"):
            ui.label("对话").classes("text-base font-bold text-grey-8")
            chat_area = ui.column().classes(
                "w-full grow overflow-y-auto rounded-lg bg-grey-1 p-3"
            )
            with ui.row().classes("w-full items-center"):
                msg_input = ui.input(placeholder="输入消息，回车发送…").classes("grow").props("outlined dense")
                send_btn = ui.button("发送")

        # ---- 右：工具调用过程（演示亮点）----
        with ui.column().classes("w-1/2 h-full"):
            ui.label("Agent 工具调用过程").classes("text-base font-bold text-grey-8")
            tool_area = ui.column().classes(
                "w-full grow overflow-y-auto rounded-lg bg-grey-10 p-3 gap-2"
            )

    def add_chat(role: str, text: str):
        with chat_area:
            ui.chat_message(text, name="我" if role == "user" else "小云",
                            sent=(role == "user")).classes("w-full")
        ui.run_javascript(
            f"getElement({chat_area.id}).$el.scrollTop = getElement({chat_area.id}).$el.scrollHeight"
        )

    def add_tool_event(event: dict):
        """右侧逐条流式展示工具名 / 入参 / 返回结果。"""
        with tool_area:
            if event["type"] == "tool_use":
                with ui.card().classes("w-full bg-blue-grey-9 text-white p-2"):
                    ui.label(f"⚙ {event['name']}").classes("font-mono font-bold text-amber-4")
                    ui.label("入参 " + json.dumps(event["input"], ensure_ascii=False)) \
                        .classes("font-mono text-xs text-blue-2 break-all")
            elif event["type"] == "tool_result":
                ok = not event.get("is_error")
                with ui.card().classes(
                    f"w-full p-2 {'bg-green-10' if ok else 'bg-red-10'} text-white"
                ):
                    ui.label(("↩ 返回 " if ok else "✗ 拦截/出错 ") + event["name"]) \
                        .classes(f"font-mono font-bold {'text-green-3' if ok else 'text-red-3'}")
                    pretty = event["result"]
                    try:
                        pretty = json.dumps(json.loads(pretty), ensure_ascii=False, indent=1)
                    except (json.JSONDecodeError, TypeError):
                        pass
                    ui.label(pretty[:600]).classes("font-mono text-xs text-grey-4 whitespace-pre-wrap break-all")
            elif event["type"] == "escalated":
                with ui.card().classes("w-full bg-deep-orange-10 text-white p-2"):
                    ui.label("▲ 已转人工，agent 结束接待").classes("font-bold text-orange-3")
        ui.run_javascript(
            f"getElement({tool_area.id}).$el.scrollTop = getElement({tool_area.id}).$el.scrollHeight"
        )

    async def ensure_session() -> str:
        if state["session_id"] is None:
            async with httpx.AsyncClient() as client:
                resp = await client.post(f"{BACKEND_URL}/api/sessions")
                state["session_id"] = resp.json()["session_id"]
        return state["session_id"]

    async def send():
        text = msg_input.value.strip()
        if not text or state["busy"]:
            return
        state["busy"] = True
        send_btn.disable()
        msg_input.value = ""
        add_chat("user", text)
        try:
            sid = await ensure_session()
            async with websockets.connect(f"{WS_URL}/ws/chat/{sid}") as ws:
                await ws.send(json.dumps({"message": text}))
                async for raw in ws:
                    event = json.loads(raw)
                    if event["type"] == "text":
                        add_chat("assistant", event["text"])
                    elif event["type"] in ("tool_use", "tool_result", "escalated"):
                        add_tool_event(event)
                    elif event["type"] == "error":
                        add_chat("assistant", f"出错了：{event['message']}")
                    elif event["type"] == "done":
                        break
        except Exception as e:
            add_chat("assistant", f"连接后端失败：{e}（后端是否已启动？）")
        finally:
            state["busy"] = False
            send_btn.enable()

    send_btn.on_click(send)
    msg_input.on("keydown.enter", send)

    with chat_area:
        ui.chat_message(
            "您好，我是云尚优选智能客服小云～ 查订单、查物流、退款、改地址都可以找我。"
            "试试：「我想查一下我的快递到哪了」（演示手机号 13800000001 ~ 15）",
            name="小云",
        ).classes("w-full")


# ---------------------------------------------------------------------------
# Admin 页：实时查看 CRM 数据
# ---------------------------------------------------------------------------

ADMIN_TABLES = ["orders", "refunds", "tickets", "users", "chat_logs"]


@ui.page("/admin")
def admin_page():
    ui.page_title("CRM 数据面板")
    with ui.header().classes("bg-grey-9 items-center"):
        ui.label("CRM 数据面板（2 秒自动刷新）").classes("text-lg font-bold")
        ui.space()
        ui.link("返回聊天", "/").classes("text-white")

    grids: dict[str, ui.aggrid] = {}

    with ui.tabs().classes("w-full") as tabs:
        for name in ADMIN_TABLES:
            ui.tab(name)
    with ui.tab_panels(tabs, value=ADMIN_TABLES[0]).classes("w-full"):
        for name in ADMIN_TABLES:
            with ui.tab_panel(name):
                grids[name] = ui.aggrid({
                    "columnDefs": [],
                    "rowData": [],
                    "defaultColDef": {"resizable": True, "sortable": True},
                }).classes("w-full").style("height: calc(100vh - 220px)")

    async def refresh():
        async with httpx.AsyncClient() as client:
            for name, grid in grids.items():
                try:
                    resp = await client.get(f"{BACKEND_URL}/api/admin/{name}")
                    data = resp.json()
                    grid.options["columnDefs"] = [
                        {"field": c, "headerName": c} for c in data["columns"]
                    ]
                    grid.options["rowData"] = data["rows"]
                    grid.update()
                except Exception:
                    pass  # 后端未启动时静默重试

    ui.timer(2.0, refresh)


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(host="127.0.0.1", port=8080, title="AI 客服 Agent Demo", reload=False, show=False)
