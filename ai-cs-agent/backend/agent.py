"""客服 Agent：Anthropic 原生 tool use 循环。

不依赖任何 agent 框架——循环本身只有 40 行左右：
    1. 带着 tools 调 messages.create
    2. stop_reason == "tool_use" → 执行工具、把 tool_result 塞回 messages、继续
    3. 其他 stop_reason → 结束本轮

每条消息、每次工具调用/结果都落库 chat_logs；
on_event 回调把过程实时推给上层（CLI 打印 / WebSocket 推送）。
"""
import json
import os
from typing import Any, Callable

import anthropic

from backend.db import get_session
from backend.guardrails import REFUND_AUTO_ESCALATE_THRESHOLD, SessionState
from backend.models import ChatLog
from backend.tools import execute_tool, get_anthropic_tools

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-fable-5")
MAX_TOOL_ROUNDS = 10  # 单轮用户消息允许的最大工具调用轮数，防失控

SYSTEM_PROMPT = f"""你是「云尚优选」电商平台的智能客服，名字叫小云。你通过工具直接操作 CRM 系统，能真正帮用户办事，而不只是聊天。

## 服务流程
1. 友好地问候用户，了解诉求。
2. 凡是涉及订单、物流、退款、地址、账户的请求，必须先核身：请用户提供注册手机号，调用 verify_identity。核身失败最多重试 2 次，仍失败则转人工。
3. 核身成功后再调用业务工具完成用户诉求。

## 安全与合规（违反即事故）
- 未核身前，绝不查询或透露任何订单/账户信息（系统层也会拦截）。
- 只处理当前核身用户本人的数据。
- 退款金额超过 {REFUND_AUTO_ESCALATE_THRESHOLD:.0f} 元：不要自助执行。直接调用 create_refund 时系统会自动转人工并返回工单号，你向用户解释清楚即可。
- 已发货订单不能改地址。被拦截时向用户解释原因，并主动给出替代方案（联系承运商拦截 / 签收后退换货 / 转人工）。
- 用户表达强烈不满，或提到「投诉」「举报」「律师」「法律」「12315」等字眼时，立刻调用 escalate_to_human（priority 至少 high），不要试图继续安抚周旋。

## 写操作确认（重要）
create_refund 和 update_shipping_address 是写操作。执行前必须先用一句话向用户复述关键信息（订单号、金额/新地址、原因），等用户明确确认（"对/是/确认"）后才能调用工具。用户未确认前绝不执行。

## 沟通风格
- 简体中文，简洁友好，每次回复不超过 120 字（列订单/物流轨迹时可以超）。
- 金额带 ¥ 符号；订单号、运单号、工单号原样展示。
- 工具返回错误时，用通俗的话向用户解释，不要暴露内部错误细节。
- escalate_to_human 调用成功后：告知工单号，礼貌收尾，不再继续处理业务。"""


def _log(session_id: str, role: str, content: str, meta: dict | None = None) -> None:
    db = get_session()
    try:
        db.add(ChatLog(session_id=session_id, role=role, content=content, meta=meta))
        db.commit()
    finally:
        db.close()


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
        _log(sid, "user", user_message)

        final_text = ""
        for _ in range(MAX_TOOL_ROUNDS):
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=self.tools,
                messages=self.messages,
            )

            # Fable 5 的安全分类器可能拒答（HTTP 200 + stop_reason=refusal）
            if response.stop_reason == "refusal":
                final_text = "抱歉，这个问题我无法处理。如需帮助请联系人工客服。"
                self.messages.append({"role": "assistant", "content": final_text})
                _log(sid, "assistant", final_text, {"stop_reason": "refusal"})
                emit("text", {"text": final_text})
                return final_text

            # 助手回合（含 tool_use 块）必须原样接回历史
            self.messages.append({"role": "assistant", "content": response.content})

            text_parts = [b.text for b in response.content if b.type == "text"]
            if text_parts:
                final_text = "\n".join(text_parts)
                _log(sid, "assistant", final_text)

            if response.stop_reason != "tool_use":
                break

            # 执行本回合的所有工具调用，结果合并为一条 user 消息
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                emit("tool_use", {
                    "id": block.id, "name": block.name, "input": block.input,
                })
                _log(sid, "tool_use", json.dumps(block.input, ensure_ascii=False),
                     {"tool": block.name, "tool_use_id": block.id})

                result, is_error = execute_tool(self.state, block.name, block.input)

                emit("tool_result", {
                    "tool_use_id": block.id, "name": block.name,
                    "result": result, "is_error": is_error,
                })
                _log(sid, "tool_result", result,
                     {"tool": block.name, "tool_use_id": block.id, "is_error": is_error})

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
