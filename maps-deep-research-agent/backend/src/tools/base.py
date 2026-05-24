"""Generic Tool interface and a small in-memory registry.

A ``Tool`` accepts a JSON-serialisable arguments dict and returns a
``ToolResult`` containing both human-readable text (for the LLM context) and
structured data (for the frontend / itinerary planner).
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    cached: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class Tool(abc.ABC):
    """Base class for a callable tool exposed to the agent."""

    name: str = ""
    description: str = ""

    @abc.abstractmethod
    async def run(self, args: dict[str, Any]) -> ToolResult:
        """Execute the tool with the given arguments."""


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("Tool requires a non-empty name")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"No tool registered with name '{name}'")
        return self._tools[name]

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return list(self._tools.keys())
