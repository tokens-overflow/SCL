"""OpenAI 兼容 provider 适配器。

覆盖 OpenAI 官方 API 与一切 OpenAI 兼容协议（如 DeepSeek，仅 base_url 不同）。
"""
import json

from backend.app.llm.base import LLMClient, LLMResponse, Message, ProviderConfig, ToolCall


def _to_messages(system: str, messages: list[Message]) -> list[dict]:
    """归一化历史 → OpenAI chat.completions messages 格式。"""
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            msg: dict = {"role": "assistant", "content": m.get("text") or None}
            tool_calls = m.get("tool_calls", [])
            if tool_calls:
                msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.input, ensure_ascii=False),
                        },
                    }
                    for tc in tool_calls
                ]
            out.append(msg)
        elif role == "tool":
            # OpenAI 要求每个 tool_call 对应一条独立的 tool 角色消息
            for r in m["results"]:
                out.append({
                    "role": "tool",
                    "tool_call_id": r["tool_use_id"],
                    "content": r["content"],
                })
    return out


def _to_tools(tools: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


class OpenAIClient(LLMClient):
    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        from openai import OpenAI

        kwargs = {}
        if config.api_key:
            kwargs["api_key"] = config.api_key
        if config.base_url:
            kwargs["base_url"] = config.base_url
        self._client = OpenAI(**kwargs)

    def create(self, *, system: str, messages: list[Message], tools: list[dict]) -> LLMResponse:
        resp = self._client.chat.completions.create(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            messages=_to_messages(system, messages),
            tools=_to_tools(tools),
        )
        choice = resp.choices[0]
        msg = choice.message
        tool_calls = [
            ToolCall(
                id=tc.id,
                name=tc.function.name,
                input=json.loads(tc.function.arguments or "{}"),
            )
            for tc in (msg.tool_calls or [])
        ]
        if choice.finish_reason == "tool_calls" or tool_calls:
            stop = "tool_use"
        elif choice.finish_reason == "content_filter":
            stop = "refusal"
        else:
            stop = "end"
        return LLMResponse(text=msg.content or "", tool_calls=tool_calls, stop_reason=stop)
