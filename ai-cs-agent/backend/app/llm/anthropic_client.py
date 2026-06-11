"""Anthropic provider 适配器（claude-* 系列）。"""
from backend.app.llm.base import LLMClient, LLMResponse, Message, ProviderConfig, ToolCall


def _to_messages(messages: list[Message]) -> list[dict]:
    """归一化历史 → Anthropic messages 格式。"""
    out: list[dict] = []
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            content: list[dict] = []
            if m.get("text"):
                content.append({"type": "text", "text": m["text"]})
            for tc in m.get("tool_calls", []):
                content.append(
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input}
                )
            out.append({"role": "assistant", "content": content})
        elif role == "tool":
            out.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": r["tool_use_id"],
                        "content": r["content"],
                        "is_error": r["is_error"],
                    }
                    for r in m["results"]
                ],
            })
    return out


def _to_tools(tools: list[dict]) -> list[dict]:
    return [
        {"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
        for t in tools
    ]


class AnthropicClient(LLMClient):
    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        import anthropic

        kwargs = {}
        if config.api_key:
            kwargs["api_key"] = config.api_key
        if config.base_url:
            kwargs["base_url"] = config.base_url
        self._client = anthropic.Anthropic(**kwargs)

    def create(self, *, system: str, messages: list[Message], tools: list[dict]) -> LLMResponse:
        resp = self._client.messages.create(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            system=system,
            tools=_to_tools(tools),
            messages=_to_messages(messages),
        )
        # Fable 5 的安全分类器可能拒答（HTTP 200 + stop_reason=refusal）
        if resp.stop_reason == "refusal":
            return LLMResponse(stop_reason="refusal")
        text = "\n".join(b.text for b in resp.content if b.type == "text")
        tool_calls = [
            ToolCall(id=b.id, name=b.name, input=b.input)
            for b in resp.content
            if b.type == "tool_use"
        ]
        stop = "tool_use" if resp.stop_reason == "tool_use" else "end"
        return LLMResponse(text=text, tool_calls=tool_calls, stop_reason=stop)
