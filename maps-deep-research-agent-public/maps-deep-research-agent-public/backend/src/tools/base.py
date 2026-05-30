"""通用 Tool 接口 + 类型化 ToolResult。

``ToolResult`` 对 payload 类型 ``T`` 做了泛型：

* ``ToolResult[PlacesPayload]``  —— Places 工具
* ``ToolResult[RoutesPayload]``  —— Directions 工具
* ``ToolResult[GeocodePayload]`` —— Geocoding 工具
* ``ToolResult[MatrixPayload]``  —— Distance Matrix 工具

只关心成功 / 失败的调用方看 ``ok`` / ``error``。
需要取 payload 的调用方（比如 ToolRunner 构造 evidence）通过
``isinstance(result.data, XxxPayload)`` 分派，不再到处写 if/else。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

T = TypeVar("T")


@dataclass
class ToolResult(Generic[T]):
    """单次工具调用的输出。

    字段：
        text:   给 LLM 总结器看的人类可读证据。
        data:   类型化 payload（``tools/maps/results.py`` 里那一组 *Payload）。
        cached: 本次返回是否命中缓存。
        error:  非空表示失败；``ok`` 会变成 False。
    """

    text: str = ""
    data: T | None = None
    cached: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class Tool(abc.ABC):
    """所有暴露给 agent 的工具的基类。"""

    name: str = ""
    description: str = ""

    @abc.abstractmethod
    async def run(self, args: dict[str, Any]) -> ToolResult[Any]:
        """用给定 args 执行工具。"""


class ToolRegistry:
    """很小的"工具名 → Tool 实例"映射，供调度器查找用。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("Tool 必须有非空的 name 字段")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"未注册的工具名 '{name}'")
        return self._tools[name]

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return list(self._tools.keys())
