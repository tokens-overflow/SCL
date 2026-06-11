"""Tool registry and CRM tool implementations（与 LLM provider 无关）。"""

# Import tool modules for decorator side effects: each module registers tools.
from backend.app.tools import address as _address
from backend.app.tools import human as _human
from backend.app.tools import identity as _identity
from backend.app.tools import logistics as _logistics
from backend.app.tools import order as _order
from backend.app.tools import refund as _refund
from backend.app.tools import user as _user
from backend.app.tools.registry import execute_tool, get_tool_specs, tool

__all__ = ["execute_tool", "get_tool_specs", "tool"]
