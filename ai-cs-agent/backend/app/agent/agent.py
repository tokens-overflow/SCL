"""客服 Agent：Anthropic 原生 tool use 循环。

不依赖任何 agent 框架——循环本身只有 40 行左右：
    1. 带着 tools 调 messages.create
    2. stop_reason == "tool_use" → 执行工具、把 tool_result 塞回 messages、继续
    3. 其他 stop_reason → 结束本轮

每条消息、每次工具调用/结果都落库 chat_logs（见 services.chat_log_service）；
on_event 回调把过程实时推给上层（CLI 打印 / WebSocket 推送）。
"""
import json
from typing import Any, Callable

import anthropic

from backend.app.agent.prompt import SYSTEM_PROMPT
from backend.app.agent.state import SessionState
from backend.app.core.config import ANTHROPIC_MODEL, MAX_TOOL_ROUNDS
from backend.app.services.chat_log_service import (
    log_message,
    log_tool_result,
    log_tool_use,
)
from backend.app.tools import execute_tool, get_anthropic_tools

# on_event(event_type, payload)
#   event_type: "text" | "tool_use" | "tool_result" | "escalated"
EventCallback = Callable[[str, dict[str, Any]], None]


class CSAgent:
    """一个会话一个实例。messages 历史保存在内存，落库 chat_logs 供审计。"""

    def __init__(self, session_id: str):
        self.client = anthropic.Anthropic()
        self.state = SessionState(session_id=session_id)
        self.messages: list[dict] = []
        self.tools = get_anthropic_tools()

    def run(self, user_message: str, on_event: EventCallback | None = None) -> str:
        """处理一条用户消息，跑完 tool use 循环，返回最终回复文本。"""
        emit = on_event or (lambda *_: None)
        sid = self.state.session_id

        self.messages.append({"role": "user", "content": user_message})
        log_message(sid, "user", user_message)

        final_text = ""
        for _ in range(MAX_TOOL_ROUNDS):
            response = self.client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=self.tools,
                messages=self.messages,
            )

            # Fable 5 的安全分类器可能拒答（HTTP 200 + stop_reason=refusal）
            if response.stop_reason == "refusal":
                final_text = "抱歉，这个问题我无法处理。如需帮助请联系人工客服。"
                self.messages.append({"role": "assistant", "content": final_text})
                log_message(sid, "assistant", final_text, {"stop_reason": "refusal"})
                emit("text", {"text": final_text})
                return final_text

            # 助手回合（含 tool_use 块）必须原样接回历史
            self.messages.append({"role": "assistant", "content": response.content})

            text_parts = [b.text for b in response.content if b.type == "text"]
            if text_parts:
                final_text = "\n".join(text_parts)
                log_message(sid, "assistant", final_text)

            if response.stop_reason != "tool_use":
                break

            # 执行本回合的所有工具调用，结果合并为一条 user 消息
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                emit("tool_use", {"id": block.id, "name": block.name, "input": block.input})
                log_tool_use(sid, block.name, block.id, block.input)

                result, is_error = execute_tool(self.state, block.name, block.input)

                emit("tool_result", {
                    "tool_use_id": block.id, "name": block.name,
                    "result": result, "is_error": is_error,
                })
                log_tool_result(sid, block.name, block.id, result, is_error)

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                    "is_error": is_error,
                })
            self.messages.append({"role": "user", "content": tool_results})
        else:
            final_text = final_text or "处理步骤过多，已停止。请换个方式描述问题或转人工。"

        if self.state.escalated:
            emit("escalated", {"session_id": sid})

        emit("text", {"text": final_text})
        return final_text
