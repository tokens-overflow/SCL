"""
通用 LLM 适配层
===============
屏蔽底层差异，让所有 Agent 范式代码与具体模型提供商解耦。

支持：
  - Anthropic  (claude-*)
  - OpenAI     (gpt-*, o1-*, o3-*)
  - DeepSeek   (deepseek-chat, deepseek-reasoner)
  - Ollama     (本地 llama3 / qwen / gemma 等)
  - 任何兼容 OpenAI API 格式的服务

用法：
    from llm_adapter import LLMClient

    # Anthropic
    client = LLMClient(provider="anthropic", model="claude-sonnet-4-20250514")

    # OpenAI
    client = LLMClient(provider="openai", model="gpt-4o")

    # DeepSeek
    client = LLMClient(provider="deepseek", model="deepseek-chat")

    # 本地 Ollama
    client = LLMClient(provider="ollama", model="llama3")

    # 任意兼容 OpenAI 格式的服务
    client = LLMClient(
        provider="openai_compat",
        model="my-model",
        base_url="https://my-llm-service.com/v1",
        api_key="sk-xxx"
    )

    # 统一调用接口
    reply = client.chat(
        system="你是一个助手",
        user="你好",
        max_tokens=500,
        stop=["Observation:"],   # 可选
    )
"""

import os
from typing import Optional


# ============================================================
# 统一消息格式
# ============================================================
class LLMResponse:
    def __init__(self, text: str, stop_reason: str = "end_turn"):
        self.text = text
        self.stop_reason = stop_reason   # "end_turn" | "stop_sequence" | "max_tokens"

    def __str__(self):
        return self.text


# ============================================================
# 基类
# ============================================================
class BaseLLMClient:
    def chat(
        self,
        user: str,
        system: str = "",
        max_tokens: int = 1000,
        stop: Optional[list[str]] = None,
        temperature: float = 0.7,
    ) -> LLMResponse:
        raise NotImplementedError

    def __call__(self, user: str, **kwargs) -> str:
        """快捷调用，直接返回文本字符串"""
        return self.chat(user, **kwargs).text


# ============================================================
# Anthropic 适配器
# ============================================================
class AnthropicClient(BaseLLMClient):
    def __init__(self, model: str = "claude-sonnet-4-20250514", api_key: str = ""):
        import anthropic
        self.model = model
        self.client = anthropic.Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        )

    def chat(self, user, system="", max_tokens=1000, stop=None, temperature=0.7):
        kwargs = dict(
            model=self.model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": user}],
        )
        if system:
            kwargs["system"] = system
        if stop:
            kwargs["stop_sequences"] = stop
        # Anthropic 扩展思考模式不支持 temperature，普通模式支持
        # kwargs["temperature"] = temperature  # 按需启用

        resp = self.client.messages.create(**kwargs)
        text = resp.content[0].text
        stop_reason = resp.stop_reason  # "end_turn" | "stop_sequence" | "max_tokens"
        return LLMResponse(text, stop_reason)


# ============================================================
# OpenAI 适配器（含 o1/o3 推理模型特殊处理）
# ============================================================
class OpenAIClient(BaseLLMClient):
    def __init__(self, model: str = "gpt-4o", api_key: str = "", base_url: str = ""):
        from openai import OpenAI
        self.model = model
        self.client = OpenAI(
            api_key=api_key or os.environ.get("OPENAI_API_KEY", ""),
            base_url=base_url or None,
        )
        # o1/o3 系列不支持 system message，需要特殊处理
        self._is_reasoning = model.startswith(("o1", "o3"))

    def chat(self, user, system="", max_tokens=1000, stop=None, temperature=0.7):
        messages = []
        if system:
            if self._is_reasoning:
                # o1/o3：system 内容合并到第一条 user 消息
                messages.append({"role": "user", "content": f"{system}\n\n{user}"})
            else:
                messages.append({"role": "system", "content": system})
                messages.append({"role": "user", "content": user})
        else:
            messages.append({"role": "user", "content": user})

        kwargs = dict(model=self.model, messages=messages)

        if self._is_reasoning:
            # o1/o3：使用 max_completion_tokens，不支持 temperature/stop
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = max_tokens
            kwargs["temperature"] = temperature
            if stop:
                kwargs["stop"] = stop

        resp = self.client.chat.completions.create(**kwargs)
        text = resp.choices[0].message.content or ""
        finish = resp.choices[0].finish_reason  # "stop" | "length" | "stop"
        stop_reason = {
            "stop": "end_turn",
            "length": "max_tokens",
        }.get(finish, "end_turn")
        return LLMResponse(text, stop_reason)


# ============================================================
# DeepSeek 适配器（兼容 OpenAI 格式）
# ============================================================
class DeepSeekClient(BaseLLMClient):
    def __init__(self, model: str = "deepseek-chat", api_key: str = ""):
        from openai import OpenAI
        self.model = model
        self.client = OpenAI(
            api_key=api_key or os.environ.get("DEEPSEEK_API_KEY", ""),
            base_url="https://api.deepseek.com/v1",
        )
        # deepseek-reasoner 是推理模型，有特殊 reasoning_content 字段
        self._is_reasoner = "reasoner" in model

    def chat(self, user, system="", max_tokens=1000, stop=None, temperature=0.7):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})

        kwargs = dict(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
        )
        if not self._is_reasoner:
            kwargs["temperature"] = temperature
        if stop:
            kwargs["stop"] = stop

        resp = self.client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        text = choice.message.content or ""

        # deepseek-reasoner 可选：同时获取推理过程
        reasoning = getattr(choice.message, "reasoning_content", None)
        if reasoning:
            # 把推理过程作为注释附加（按需保留）
            text = f"[思考过程]\n{reasoning}\n\n[最终答案]\n{text}"

        stop_reason = "end_turn" if choice.finish_reason == "stop" else "max_tokens"
        return LLMResponse(text, stop_reason)


# ============================================================
# Ollama 本地适配器
# ============================================================
class OllamaClient(BaseLLMClient):
    def __init__(self, model: str = "llama3", base_url: str = "http://localhost:11434"):
        self.model = model
        self.base_url = base_url.rstrip("/")

    def chat(self, user, system="", max_tokens=1000, stop=None, temperature=0.7):
        import urllib.request, json as _json

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if stop:
            payload["options"]["stop"] = stop

        data = _json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            result = _json.loads(r.read())

        text = result.get("message", {}).get("content", "")
        done_reason = result.get("done_reason", "stop")
        stop_reason = "end_turn" if done_reason == "stop" else "max_tokens"
        return LLMResponse(text, stop_reason)


# ============================================================
# 任意兼容 OpenAI 格式的服务（通用）
# ============================================================
class OpenAICompatClient(BaseLLMClient):
    """
    适用于：
    - 硅基流动 (siliconflow.cn)
    - 月之暗面 Kimi (platform.moonshot.cn)
    - 智谱 GLM (open.bigmodel.cn)
    - 阿里通义 (dashscope.aliyuncs.com)
    - 百川 (api.baichuan-ai.com)
    - 任何自部署的 OpenAI 兼容服务
    """
    def __init__(self, model: str, base_url: str, api_key: str = ""):
        from openai import OpenAI
        self.model = model
        self.client = OpenAI(api_key=api_key, base_url=base_url)

    def chat(self, user, system="", max_tokens=1000, stop=None, temperature=0.7):
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})

        kwargs = dict(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if stop:
            kwargs["stop"] = stop

        resp = self.client.chat.completions.create(**kwargs)
        text = resp.choices[0].message.content or ""
        stop_reason = "end_turn" if resp.choices[0].finish_reason == "stop" else "max_tokens"
        return LLMResponse(text, stop_reason)


# ============================================================
# 工厂函数：统一入口
# ============================================================
PROVIDER_MAP = {
    "anthropic":    AnthropicClient,
    "openai":       OpenAIClient,
    "deepseek":     DeepSeekClient,
    "ollama":       OllamaClient,
    "openai_compat": OpenAICompatClient,
    # 常用别名
    "claude":       AnthropicClient,
    "gpt":          OpenAIClient,
}

# 常用服务的 base_url 预设
PRESET_BASE_URLS = {
    "siliconflow":  "https://api.siliconflow.cn/v1",
    "kimi":         "https://api.moonshot.cn/v1",
    "zhipu":        "https://open.bigmodel.cn/api/paas/v4/",
    "qwen":         "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "baichuan":     "https://api.baichuan-ai.com/v1",
    "together":     "https://api.together.xyz/v1",
    "groq":         "https://api.groq.com/openai/v1",
}

def LLMClient(
    provider: str = "anthropic",
    model: str = "",
    api_key: str = "",
    base_url: str = "",
    **kwargs,
) -> BaseLLMClient:
    """
    工厂函数，返回对应 provider 的客户端实例。

    Examples:
        LLMClient("anthropic", "claude-sonnet-4-20250514")
        LLMClient("openai", "gpt-4o")
        LLMClient("deepseek", "deepseek-reasoner")
        LLMClient("ollama", "qwen2.5:7b")
        LLMClient("openai_compat", "Qwen/Qwen2.5-72B-Instruct",
                  base_url="https://api.siliconflow.cn/v1", api_key="sk-xxx")
        LLMClient("kimi", "moonshot-v1-8k")   # 使用预设 base_url
    """
    # 检查是否是预设服务名
    if provider in PRESET_BASE_URLS and not base_url:
        base_url = PRESET_BASE_URLS[provider]
        provider = "openai_compat"

    cls = PROVIDER_MAP.get(provider)
    if cls is None:
        raise ValueError(
            f"未知 provider: {provider}\n"
            f"支持: {list(PROVIDER_MAP.keys())} 或预设服务: {list(PRESET_BASE_URLS.keys())}"
        )

    # 按构造函数需要的参数传递
    if provider in ("openai", "openai_compat", "claude", "gpt", "anthropic"):
        if base_url:
            return cls(model=model, api_key=api_key, base_url=base_url, **kwargs)
        elif api_key:
            return cls(model=model, api_key=api_key, **kwargs)
        else:
            return cls(model=model, **kwargs)
    elif provider == "ollama":
        return cls(model=model, base_url=base_url or "http://localhost:11434", **kwargs)
    else:
        return cls(model=model, api_key=api_key, **kwargs)


# ============================================================
# 如何在 Agent 范式中替换
# ============================================================
"""
# ── 原来的写法（绑定 Anthropic）──
import anthropic
client = anthropic.Anthropic()
resp = client.messages.create(
    model="claude-sonnet-4-20250514",
    max_tokens=500,
    system=SYSTEM,
    messages=[{"role": "user", "content": user_msg}],
    stop_sequences=["Observation:"],
)
output = resp.content[0].text

# ── 换成通用写法 ──
from llm_adapter import LLMClient
client = LLMClient("anthropic", "claude-sonnet-4-20250514")
# 或换成任意 provider：
# client = LLMClient("openai", "gpt-4o")
# client = LLMClient("deepseek", "deepseek-chat")
# client = LLMClient("ollama", "qwen2.5:7b")

resp = client.chat(
    system=SYSTEM,
    user=user_msg,
    max_tokens=500,
    stop=["Observation:"],
)
output = resp.text
# stop_reason 判断：resp.stop_reason == "end_turn" | "stop_sequence" | "max_tokens"
"""
