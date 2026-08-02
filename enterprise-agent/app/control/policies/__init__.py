"""策略集合。

**为什么控制层要用组合式策略，而不是一个大函数？**

因为「能不能做」这个问题实际上是十几个正交问题的合取：
身份是谁、Agent 授权了吗、工具在白名单里吗、参数合法吗、
业务规则允许吗、风险多高、要不要审批、数据范围够吗、有没有超频、
有没有敏感信息要拦。

写成一个大函数会有三个后果：

1. 加一条规则就要动一个所有人都在改的函数；
2. 单独测试某条规则必须构造全套上下文；
3. 审计时无法回答「具体是哪条规则拒的」。

拆成策略之后，每条规则是一个独立、可单测、可单独开关的单元，
PolicyEngine 只负责按顺序跑并聚合。
"""

from __future__ import annotations

from typing import Protocol

from app.control.models import PolicyEvaluationRequest, PolicyEvaluationResult


class Policy(Protocol):
    """策略接口。

    所有策略必须是**无副作用**的：只读取请求、返回裁决，
    不修改数据库、不发通知、不调用外部系统。

    唯一的例外是 `ParameterPolicy` 会把校验后的参数写进结果的
    `validated_arguments`——但那也只是返回值的一部分，不是外部副作用。

    为什么强调无副作用：策略可能被执行多次（审批通过后要重跑一遍），
    有副作用的策略会在第二次执行时产生意外后果。
    """

    name: str

    async def evaluate(self, request: PolicyEvaluationRequest) -> PolicyEvaluationResult:
        """评估一次动作请求。"""
        ...


from app.control.policies.approval import ApprovalPolicy  # noqa: E402
from app.control.policies.business_rule import BusinessRulePolicy  # noqa: E402
from app.control.policies.data_access import DataAccessPolicy  # noqa: E402
from app.control.policies.identity import IdentityPolicy  # noqa: E402
from app.control.policies.parameter import ParameterPolicy  # noqa: E402
from app.control.policies.permissions import (  # noqa: E402
    AgentPermissionPolicy,
    ToolPermissionPolicy,
)
from app.control.policies.rate_limit import RateLimitPolicy  # noqa: E402
from app.control.policies.risk import RiskPolicy  # noqa: E402
from app.control.policies.sensitive_data import SensitiveDataPolicy  # noqa: E402

__all__ = [
    "Policy",
    "IdentityPolicy",
    "AgentPermissionPolicy",
    "ToolPermissionPolicy",
    "ParameterPolicy",
    "BusinessRulePolicy",
    "RiskPolicy",
    "ApprovalPolicy",
    "DataAccessPolicy",
    "RateLimitPolicy",
    "SensitiveDataPolicy",
]
