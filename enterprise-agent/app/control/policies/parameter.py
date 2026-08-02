"""ParameterPolicy：参数强校验。

**这条策略是「结构化输出只是必要条件，不是充分条件」这句话的执行点。**

模型给出的 `proposal.arguments` 是一个普通的 dict。它可能：

* 少一个必填字段（模型忘了）；
* 多一个字段（模型自作主张）；
* 类型不对（把 0.1 写成 "10%"）；
* 值越界（discount_rate = 3.0）；
* 语义错位（把「九折」理解成 0.9 而不是 0.1）。

这条策略用工具自己声明的 `args_model` 做强校验，把 dict 变成一个
**类型安全、取值合法的对象**，写进 `validated_arguments`。

之后的执行器**只接受 `PolicyDecision.validated_arguments`**，
永远不碰 `proposal.arguments`。这就是「禁止把未经验证的字典直接传进工具」
在架构层面的落地方式——不是靠约定，是靠数据流向。
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError as PydanticValidationError

from app.actions.registry import ToolRegistry
from app.control.models import PolicyEvaluationRequest, PolicyEvaluationResult
from app.core.enums import RiskLevel
from app.core.errors import ToolNotRegisteredError


class ParameterPolicy:
    """用工具的 Pydantic 参数模型做强校验与规范化。

    Args:
        registry: 工具注册表，用于取 `args_model`。
    """

    name = "ParameterPolicy"

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def evaluate(self, request: PolicyEvaluationRequest) -> PolicyEvaluationResult:
        """校验并规范化参数。

        Returns:
            成功时 ALLOW，且 `validated_arguments` 里是**规范化后**的参数；
            失败时 DENY，并把 Pydantic 的错误明细放进 metadata——
            这份明细会回灌给认知层，让模型知道「此路不通、以及为什么」，
            而不是笼统地重试一遍同样的错误。
        """
        try:
            tool = self.registry.get(request.tool_name)
        except ToolNotRegisteredError:
            # 正常情况下 AgentPermissionPolicy 已经拦下了；
            # 这里是纵深防御——万一策略顺序被调整过。
            return PolicyEvaluationResult.deny(
                self.name,
                "TOOL_NOT_REGISTERED",
                f"工具 {request.tool_name} 未注册",
                risk_level=RiskLevel.HIGH,
            )

        raw: dict[str, Any] = dict(request.proposal.arguments or {})

        try:
            validated = tool.args_model.model_validate(raw)
        except PydanticValidationError as exc:
            return PolicyEvaluationResult.deny(
                self.name,
                "PARAMETER_INVALID",
                f"动作参数不合法：{_first_error_message(exc)}",
                risk_level=RiskLevel.MEDIUM,
                metadata={
                    "tool_name": request.tool_name,
                    # 结构化的错误明细，供认知层重新生成方案时参考。
                    "errors": [
                        {
                            "field": ".".join(str(x) for x in err["loc"]),
                            "type": err["type"],
                            "message": err["msg"],
                        }
                        for err in exc.errors()
                    ],
                    "received_keys": sorted(raw),
                },
            )

        # 规范化输出：用 model_dump() 而不是原始 dict。
        # 这一步会应用 Pydantic 的类型转换、默认值和字段别名，
        # 保证下游拿到的永远是同一种形状——这对幂等键的稳定性至关重要，
        # 因为幂等键是按参数内容算出来的。
        return PolicyEvaluationResult.allow(
            self.name,
            validated_arguments=validated.model_dump(mode="json"),
            metadata={"tool_name": request.tool_name},
        )


def _first_error_message(exc: PydanticValidationError) -> str:
    errors = exc.errors()
    if not errors:  # pragma: no cover
        return "未知校验错误"
    first = errors[0]
    field = ".".join(str(x) for x in first["loc"]) or "(root)"
    return f"{field}: {first['msg']}"
