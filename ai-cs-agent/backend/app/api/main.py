"""FastAPI 后端入口：聚合 chat / admin 路由。

启动：
    uv run uvicorn backend.app.api.main:app --reload --port 8000

接口：
    POST /api/sessions                  创建会话，返回 session_id
    WS   /ws/chat/{session_id}          发 {"message": "..."}，
                                        收 {"type": "text|tool_use|tool_result|escalated|done|error", ...}
    GET  /api/admin/{table}             admin 页查表（users/orders/refunds/tickets/chat_logs）
    GET  /api/health
"""
from fastapi import FastAPI

from backend.app.api import admin, chat

app = FastAPI(title="AI 客服 Agent API")
app.include_router(chat.router)
app.include_router(admin.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}
