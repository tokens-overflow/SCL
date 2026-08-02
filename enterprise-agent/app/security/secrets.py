"""密钥读取与日志脱敏。

两条不可妥协的纪律：

1. **源码里不允许出现任何 Secret。** 一律从环境变量 / 密钥管理服务读取。
   仓库里只保留 `.env.example`，里面全是占位符。
2. **日志里不允许出现任何 Secret。** 这里提供 `redact()`，
   在写审计和日志之前统一过一遍。哪怕多余，也比某天在 grep 日志时
   看到一整行 `sk-...` 要好。
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from typing import Any, Protocol

#: 需要在日志中屏蔽的字段名片段（小写匹配）。
SENSITIVE_FIELD_MARKERS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "passwd",
    "credential",
    "authorization",
    "cookie",
    "private_key",
    "session_id",
)

#: 常见密钥形态的正则。用于兜底：即使字段名没命中，值本身像密钥也要遮掉。
_SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),           # OpenAI 风格
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),       # Anthropic 风格
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),             # GitHub token
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}"),    # Bearer token
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT
)

REDACTED = "***REDACTED***"


class SecretProvider(Protocol):
    """密钥读取接口。

    Demo 用环境变量；生产可以换成 Vault / AWS Secrets Manager / K8s Secret，
    调用方代码零改动。
    """

    def get(self, name: str, default: str | None = None) -> str | None:
        """读取一个密钥。"""
        ...


class EnvSecretProvider:
    """从环境变量读取密钥的默认实现。"""

    def get(self, name: str, default: str | None = None) -> str | None:
        """读取环境变量。

        Args:
            name: 环境变量名。
            default: 缺省值。

        Returns:
            密钥值或 ``default``。**调用方拿到之后不要往日志里放。**
        """
        return os.environ.get(name, default)


default_secret_provider: SecretProvider = EnvSecretProvider()


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_FIELD_MARKERS)


def redact_text(text: str) -> str:
    """遮盖字符串里疑似密钥的片段。"""
    for pattern in _SECRET_VALUE_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact(value: Any, *, extra_keys: Iterable[str] = ()) -> Any:
    """递归遮盖结构中的敏感字段，返回可安全写入日志的副本。

    Args:
        value: 任意结构（dict / list / str / 标量）。
        extra_keys: 额外要屏蔽的字段名。

    Returns:
        遮盖后的**新对象**，原对象不会被修改。

    Note:
        这个函数只负责「密钥类」敏感信息。个人信息（手机号、身份证、卡号）
        的处理在 :mod:`app.control.data_masking`——两者目的不同，
        不要合并：一个是防泄密，一个是防个人信息出企业边界。
    """
    markers = SENSITIVE_FIELD_MARKERS + tuple(k.lower() for k in extra_keys)

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for k, v in node.items():
                key = str(k)
                if any(m in key.lower() for m in markers):
                    out[key] = REDACTED if v not in (None, "") else None
                else:
                    out[key] = _walk(v)
            return out
        if isinstance(node, (list, tuple)):
            return [_walk(v) for v in node]
        if isinstance(node, str):
            return redact_text(node)
        return node

    return _walk(value)
