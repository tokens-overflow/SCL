"""加载 llm.yaml，解析出当前启用的 provider 配置。

约定：
- 配置文件默认在项目根目录 `llm.yaml`，可用环境变量 `LLM_CONFIG` 覆盖路径。
- 启用哪个 provider 由 yaml 顶层 `active` 决定，可被环境变量 `LLM_PROVIDER` 覆盖。
- 值里支持 `${ENV_VAR}` 占位（如 api_key），从环境变量解析——密钥放 .env，不入库 yaml。
"""
import os
import re
from pathlib import Path

import yaml

from backend.app.core.config import PROJECT_ROOT
from backend.app.llm.base import ProviderConfig

LLM_CONFIG_PATH = Path(os.getenv("LLM_CONFIG", str(PROJECT_ROOT / "llm.yaml")))

_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _resolve_env(value):
    """把字符串里的 ${VAR} 替换成环境变量值（缺失则替换为空串）。"""
    if not isinstance(value, str):
        return value
    return _ENV_PATTERN.sub(lambda m: os.getenv(m.group(1), ""), value)


def load_config() -> dict:
    if not LLM_CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"未找到 LLM 配置文件：{LLM_CONFIG_PATH}。"
            "默认应为项目根目录的 llm.yaml，或用环境变量 LLM_CONFIG 指向你的配置。"
        )
    with open(LLM_CONFIG_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not data.get("providers"):
        raise ValueError(f"{LLM_CONFIG_PATH} 缺少 providers 配置。")
    return data


def load_active_provider() -> ProviderConfig:
    """返回当前启用 provider 的配置。"""
    data = load_config()
    active = os.getenv("LLM_PROVIDER") or data.get("active")
    providers: dict = data["providers"]
    if active not in providers:
        raise ValueError(
            f"启用的 provider「{active}」未在 {LLM_CONFIG_PATH} 的 providers 中定义。"
            f"可选：{list(providers)}"
        )
    p = providers[active] or {}
    if "model" not in p:
        raise ValueError(f"provider「{active}」缺少 model 字段。")
    api_key = _resolve_env(p.get("api_key")) or None
    base_url = _resolve_env(p.get("base_url")) or None
    return ProviderConfig(
        name=active,
        type=p.get("type", "anthropic"),
        model=p["model"],
        api_key=api_key,
        base_url=base_url,
        max_tokens=int(p.get("max_tokens", 4096)),
    )
