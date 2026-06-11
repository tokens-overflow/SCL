"""Agent loop, prompt, and shared runtime session state.

注意：CSAgent 不在包级别导出，请直接 ``from backend.app.agent.agent import CSAgent``。
agent.agent 依赖 backend.app.tools，而 tools 又依赖 backend.app.agent.state；若在本
__init__ 里 eager 导入 CSAgent 会触发循环导入。这里只导出零依赖的 SessionState。
"""

from backend.app.agent.state import SessionState

__all__ = ["SessionState"]
