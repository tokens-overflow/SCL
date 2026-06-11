"""客服 Agent：与厂商无关的 tool use 循环。

不依赖任何 agent 框架，循环本身只有约 40 行：
    1. 带着 tools 调 client.create（具体 provider 由 llm.yaml 决定）
    2. stop_reason == "tool_use" → 执行工具、把 tool_result 回填、继续
    3. 其它 stop_reason → 结束本轮

会话历史以归一化形式保存（见 llm.base.Message），由 provider 适配器翻译成各自的
wire format，因此同一套循环可同时跑 Anthropic / OpenAI / DeepSeek。

每条消息、每次工具调用/结果都落库 chat_logs；on_event 回调把过程实时推给上层
（CLI 打印 / WebSocket 推送）。
"""
from typing import Any, Callable

from backend.app.agent.prompt import SYSTEM_PROMPT
from backend.app.agent.state import SessionState
from backend.app.core.config import MAX_TOOL_ROUNDS
from backend.app.llm import LLMClient, Message, create_llm_client
from backend.app.services.chat_log_service import (
    log_message,
    log_tool_result,
    log_tool_use,
)
from backend.app.tools import execute_tool, get_tool_specs

# on_event(event_type, payload)
#   event_type: "text" | "tool_use" | "tool_result" | "escalated"
EventCallback = Callable[[str, dict[str, Any]], None]


class CSAgent:
    """一个会话一个实例。messages 历史保存在内存，落库 chat_logs 供审计。"""

    def __init__(self, session_id: str, client: LLMClient | None = None):
        self.client = client or create_llm_client()
        self.state = SessionState(session_id=session_id)
        self.messages: list[Message] = []
        self.tools = get_tool_specs()

    def run(self, user_message: str, on_event: EventCallback | None = None) -> str:
        """处理一条用户消息，跑完 tool use 循环，返回最终回复文本。"""
        emit = on_event or (lambda *_: None)
        sid = self.state.session_id

        self.messages.append({"role": "user", "content": user_message})
        log_message(sid, "user", user_message)

        final_text = ""
        for _ in range(MAX_TOOL_ROUNDS):
            resp = self.client.create(
                system=SYSTEM_PROMPT, messages=self.messages, tools=self.tools
            )

            if resp.stop_reason == "refusal":
                final_text = "抱歉，这个问题我无法处理。如需帮助请联系人工客服。"
                self.messages.append({"role": "assistant", "text": final_text, "tool_calls": []})
                log_message(sid, "assistant", final_text, {"stop_reason": "refusal"})
                emit("text", {"text": final_text})
                return final_text

            self.messages.append(
                {"role": "assistant", "text": resp.text, "tool_calls": resp.tool_calls}
            )
            if resp.text:
                final_text = resp.text
                log_message(sid, "assistant", final_text)

            if resp.stop_reason != "tool_use" or not resp.tool_calls:
                break

            # 执行本回合的所有工具调用，结果合并为一条 tool 消息
            results = []
            for call in resp.tool_calls:
                emit("tool_use", {"id": call.id, "name": call.name, "input": call.input})
                log_tool_use(sid, call.name, call.id, call.input)

                result, is_error = execute_tool(self.state, call.name, call.input)

                emit("tool_result", {
                    "tool_use_id": call.id, "name": call.name,
                    "result": result, "is_error": is_error,
                })
                log_tool_result(sid, call.name, call.id, result, is_error)

                results.append({
                    "tool_use_id": call.id,
                    "name": call.name,
                    "content": result,
                    "is_error": is_error,
                })
            self.messages.append({"role": "tool", "results": results})
        else:
            final_text = final_text or "处理步骤过多，已停止。请换个方式描述问题或转人工。"

        if self.state.escalated:
            emit("escalated", {"session_id": sid})

        emit("text", {"text": final_text})
        return final_text
