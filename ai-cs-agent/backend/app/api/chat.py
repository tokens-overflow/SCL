"""会话与聊天接口：创建会话 + WebSocket tool use 流式推送。"""
import asyncio
import json
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.app.agent.agent import CSAgent

router = APIRouter()

# session_id -> CSAgent。demo 级实现：进程内存保存，重启即失
AGENTS: dict[str, CSAgent] = {}


@router.post("/api/sessions")
def create_chat_session():
    session_id = f"web-{uuid.uuid4().hex[:12]}"
    AGENTS[session_id] = CSAgent(session_id)
    return {"session_id": session_id}


@router.websocket("/ws/chat/{session_id}")
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
