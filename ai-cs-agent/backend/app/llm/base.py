"""Provider-agnostic LLM 抽象。

agent 循环只依赖这里的归一化类型（ToolCall / LLMResponse），不接触任何具体
厂商的消息格式。各 provider 适配器（anthropic_client / openai_client）负责把归一化
的会话历史翻译成自己的 wire format，再把响应翻译回 LLMResponse。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    """模型发起的一次工具调用（已归一化，与厂商无关）。"""

    id: str
    name: str
    input: dict


@dataclass
class LLMResponse:
    """一次 LLM 调用的归一化结果。"""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    # "tool_use"：模型要求执行工具；"end"：正常结束；"refusal"：安全拒答
    stop_reason: str = "end"


@dataclass
class ProviderConfig:
    """单个 LLM provider 的配置（由 llm.yaml 解析得到）。"""

    name: str
    type: str  # "anthropic" | "openai"
    model: str
    api_key: str | None = None
    base_url: str | None = None
    max_tokens: int = 4096


# 归一化会话消息（agent 维护，provider 翻译）。entry 形态：
#   {"role": "user", "content": str}
#   {"role": "assistant", "text": str, "tool_calls": list[ToolCall]}
#   {"role": "tool", "results": [{"tool_use_id", "name", "content", "is_error"}, ...]}
Message = dict


class LLMClient(ABC):
    """LLM 客户端接口。一个 provider 一个实现。"""

    def __init__(self, config: ProviderConfig):
        self.config = config

    @abstractmethod
    def create(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[dict],
    ) -> LLMResponse:
        """发起一次补全。tools 为中立工具规格 [{name, description, input_schema}]。"""
        raise NotImplementedError
