"""工具注册表。

**这是防「模型幻觉出一个工具」的第一道闸。**

三条不可妥协的规则：

1. **只有注册过的工具才能被调用。** 注册表里没有的名字一律拒绝。
2. **不通过字符串动态 import 任意模块。** 注册是显式的函数调用，
   工具类必须在代码里被真实引用过。如果允许 `importlib.import_module(tool_name)`，
   一个精心构造的工具名就等于任意代码执行。
3. **不使用 `eval` / `exec` 执行模型生成的任何内容。**

在此之上还有三层可见性/可调用性过滤：

    已注册 → Agent 白名单可见 → 用户权限可调用

注意可见与可调用是**两件事**：一个工具可能对某 Agent 可见（会出现在给 LLM 的清单里），
但对当前用户不可调用（权限不足）。让它可见的好处是模型能给出「这件事需要更高权限」
这样有用的回复，而不是茫然地说「我做不到」。
"""

from __future__ import annotations

from collections.abc import Iterable

from app.actions.base import AgentTool
from app.core.errors import ToolNotRegisteredError
from app.operations.logging import get_logger
from app.security.identity import AgentIdentity, ResolvedIdentity

logger = get_logger(__name__)


class ToolRegistry:
    """工具注册表。

    Example:
        >>> registry = ToolRegistry()
        >>> registry.register(QueryCustomerTool())
        >>> tool = registry.get("query_customer")
    """

    def __init__(self) -> None:
        self._tools: dict[str, AgentTool] = {}

    def register(self, tool: AgentTool, *, override: bool = False) -> None:
        """注册一个工具实例。

        Args:
            tool: 工具实例。
            override: 是否允许覆盖同名工具。

        Raises:
            ValueError: 工具没有 name、或重名且未允许覆盖。

        Note:
            重名默认报错而不是静默覆盖。静默覆盖是一类很难查的事故：
            两个模块各注册了一个 `send_notification`，
            线上跑的是哪个取决于 import 顺序。
        """
        if not tool.name:
            raise ValueError(f"工具 {type(tool).__name__} 未声明 name")
        if not hasattr(tool, "args_model"):
            raise ValueError(f"工具 {tool.name} 未声明 args_model，禁止注册未定义参数模型的工具")
        if tool.name in self._tools and not override:
            raise ValueError(f"工具名冲突：{tool.name} 已被注册")

        # 一致性断言：写操作必须幂等。不幂等的写工具一旦重试就是重复副作用，
        # 与其在生产上发现，不如在注册时就拒绝。
        from app.core.enums import StepType

        if tool.step_type in (StepType.WRITE, StepType.NOTIFY) and not tool.idempotent:
            raise ValueError(
                f"工具 {tool.name} 是写操作但声明为非幂等，禁止注册："
                "写操作必须支持幂等，否则重试会产生重复副作用"
            )

        self._tools[tool.name] = tool
        logger.info(
            "tool_registered",
            tool_name=tool.name,
            risk_level=str(tool.risk_level),
            step_type=str(tool.step_type),
        )

    def unregister(self, name: str) -> None:
        """注销工具（测试用）。"""
        self._tools.pop(name, None)

    def get(self, name: str) -> AgentTool:
        """按名字取工具。

        Raises:
            ToolNotRegisteredError: 工具未注册。**这是模型幻觉工具名时的落点**，
                也是「不允许调用未注册工具」这条规则的执行点。
        """
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotRegisteredError(
                f"工具未注册：{name}",
                details={"tool_name": name, "registered": sorted(self._tools)},
            )
        return tool

    def has(self, name: str) -> bool:
        """判断工具是否已注册。"""
        return name in self._tools

    def all_tools(self) -> list[AgentTool]:
        """返回全部已注册工具。"""
        return list(self._tools.values())

    def visible_to_agent(self, agent: AgentIdentity) -> list[AgentTool]:
        """按 Agent 白名单过滤可见工具。

        Args:
            agent: Agent 身份。

        Returns:
            该 Agent 被允许看到的工具。**白名单为空表示什么都看不到**，
            而不是「没限制所以全都能看」——默认拒绝比默认放行安全得多。
        """
        return [tool for name, tool in sorted(self._tools.items()) if name in agent.allowed_tools]

    def callable_by(self, identity: ResolvedIdentity) -> list[AgentTool]:
        """按「用户 ∩ Agent ∩ 服务账号」权限过滤可调用工具。

        Args:
            identity: 已解析的三方身份。

        Returns:
            当前身份组合下真正**可以调用**的工具。

        Note:
            这里只做粗粒度过滤，用于生成上下文和 `/tools` 接口。
            **真正的放行判定仍然在控制层**——因为权限只是九道策略中的一道，
            参数、业务规则、风险、限流都还没查。
            绝不能因为「工具出现在这个列表里」就跳过 PolicyEngine。
        """
        result: list[AgentTool] = []
        for tool in self.visible_to_agent(identity.agent):
            # 每个工具背后的服务账号不同，权限交集要按工具逐个算。
            per_tool_identity = identity.model_copy(update={"service": identity.service})
            if not tool.required_permissions:
                result.append(tool)
                continue
            if not per_tool_identity.missing_permissions(set(tool.required_permissions)):
                result.append(tool)
        return result

    def describe_all(self, tools: Iterable[AgentTool] | None = None) -> list[dict[str, object]]:
        """返回工具自描述列表。"""
        target = tools if tools is not None else self.all_tools()
        return [type(tool).describe() for tool in target]


#: 进程级默认注册表。
default_registry = ToolRegistry()


def register_builtin_tools(registry: ToolRegistry | None = None) -> ToolRegistry:
    """注册内置演示工具。

    **显式 import + 显式注册**，没有任何动态发现机制。
    自动扫描目录注册看起来优雅，但它意味着「往目录里丢一个文件就能获得执行权」，
    在有权限边界的系统里这是一条不该开的口子。

    Args:
        registry: 目标注册表，缺省用全局默认。

    Returns:
        注册完成的注册表。
    """
    from app.actions.tools.apply_discount import ApplyDiscountTool
    from app.actions.tools.query_customer import QueryCustomerTool
    from app.actions.tools.send_notification import SendNotificationTool

    target = registry or default_registry
    for tool in (QueryCustomerTool(), ApplyDiscountTool(), SendNotificationTool()):
        if not target.has(tool.name):
            target.register(tool)
    return target
