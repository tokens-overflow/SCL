"""FastAPI 后端：REST API + WebSocket。

启动：
    uv run uvicorn backend.main:app --reload --port 8000

接口：
    POST /api/sessions                  创建会话，返回 session_id
    WS   /ws/chat/{session_id}          发 {"message": "..."}，
                                        收 {"type": "text|tool_use|tool_result|escalated|done|error", ...}
    GET  /api/admin/{table}             admin 页查表（users/orders/refunds/tickets/chat_logs）
    GET  /api/health
"""
import asyncio
import json
import uuid

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from backend.agent import CSAgent
from backend.db import get_session
from backend.models import ChatLog, Order, Refund, Ticket, User

app = FastAPI(title="AI 客服 Agent API")

# session_id -> CSAgent。demo 级实现：进程内存保存，重启即失
AGENTS: dict[str, CSAgent] = {}


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/sessions")
def create_chat_session():
    session_id = f"web-{uuid.uuid4().hex[:12]}"
    AGENTS[session_id] = CSAgent(session_id)
    return {"session_id": session_id}


@app.websocket("/ws/chat/{session_id}")
async def chat_ws(ws: WebSocket, session_id: str):
    await ws.accept()
    agent = AGENTS.get(session_id)
    if agent is None:
        await ws.send_json({"type": "error", "message": "会话不存在，请先 POST /api/sessions"})
        await ws.close()
        return

    loop = asyncio.get_running_loop()
    try:
        while True:
            data = json.loads(await ws.receive_text())
            user_message = data.get("message", "").strip()
            if not user_message:
                continue

            queue: asyncio.Queue = asyncio.Queue()

            def on_event(event_type: str, payload: dict) -> None:
                # 工具回调跑在工作线程里，用 call_soon_threadsafe 投回事件循环
                loop.call_soon_threadsafe(queue.put_nowait, {"type": event_type, **payload})

            # agent.run 是同步阻塞调用（Anthropic SDK 同步客户端），丢进线程池
            task = asyncio.create_task(asyncio.to_thread(agent.run, user_message, on_event))

            while True:
                get_event = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {task, get_event}, return_when=asyncio.FIRST_COMPLETED
                )
                if get_event in done:
                    await ws.send_json(get_event.result())
                if task in done:
                    get_event.cancel()
                    # 任务结束后把队列里剩余事件清空发完
                    while not queue.empty():
                        await ws.send_json(queue.get_nowait())
                    break

            exc = task.exception()
            if exc is not None:
                await ws.send_json({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            await ws.send_json({"type": "done", "escalated": agent.state.escalated})
    except WebSocketDisconnect:
        pass


# ---------------------------------------------------------------------------
# Admin：实时查看 CRM 数据表
# ---------------------------------------------------------------------------

ADMIN_TABLES = {
    "users": (User, ["id", "name", "phone", "email", "member_level", "created_at"]),
    "orders": (Order, ["id", "order_no", "user_id", "product_name", "quantity",
                       "amount", "status", "shipping_address", "carrier",
                       "tracking_no", "created_at", "updated_at"]),
    "refunds": (Refund, ["id", "refund_no", "order_id", "user_id", "amount",
                         "reason", "status", "created_at"]),
    "tickets": (Ticket, ["id", "ticket_no", "user_id", "summary", "priority",
                         "status", "created_at"]),
    "chat_logs": (ChatLog, ["id", "session_id", "role", "content", "created_at"]),
}


@app.get("/api/admin/{table}")
def admin_table(table: str, limit: int = 100):
    if table not in ADMIN_TABLES:
        raise HTTPException(404, f"未知表：{table}，可选：{list(ADMIN_TABLES)}")
    model, columns = ADMIN_TABLES[table]
    db = get_session()
    try:
        rows = db.scalars(select(model).order_by(model.id.desc()).limit(limit)).all()
        result = []
        for row in rows:
            item = {}
            for col in columns:
                value = getattr(row, col)
                if hasattr(value, "value"):       # 枚举
                    value = value.value
                elif hasattr(value, "strftime"):  # 时间
                    value = value.strftime("%Y-%m-%d %H:%M:%S")
                elif isinstance(value, str) and len(value) > 120:
                    value = value[:120] + "…"
                item[col] = value
            result.append(item)
        return {"table": table, "columns": columns, "rows": result}
    finally:
        db.close()
