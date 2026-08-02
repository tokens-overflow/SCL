"""参数校验辅助。

`ParameterPolicy` 已经在策略链里完成了校验，这里提供的是**独立可用**的
校验函数，用于两个场景：

1. 执行器在真正调用工具前的**二次确认**（纵深防御：
   万一将来有人在策略链里调整了顺序，执行器这一层还能兜住）；
2. 单元测试里直接验证「非法参数会被挡下」，不需要构造整条策略链。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from app.actions.base import AgentTool
from app.core.errors import ValidationError


def validate_tool_arguments(tool: AgentTool, arguments: dict[str, Any]) -> BaseModel:
    """用工具自己的 `args_model` 校验参数。

    Args:
        tool: 目标工具。
        arguments: 待校验的参数字典。

    Returns:
        校验通过的 Pydantic 对象。**执行器只把这个对象传给工具**，
        永远不传原始字典。

    Raises:
        ValidationError: 参数不合法，`details["errors"]` 里是结构化错误明细。

    Note:
        这个函数是「禁止把未经验证的字典直接传入工具」这条规则的执行点。
        它的返回类型是 `BaseModel` 而不是 `dict`，
        所以在类型层面就无法把一个裸字典塞进 `tool.execute()`。
    """
    try:
        return tool.args_model.model_validate(arguments)
    except PydanticValidationError as exc:
        raise ValidationError(
            f"工具 {tool.name} 的参数校验失败",
            details={
                "tool_name": tool.name,
                "errors": [
                    {
                        "field": ".".join(str(x) for x in err["loc"]),
                        "type": err["type"],
                        "message": err["msg"],
                    }
                    for err in exc.errors()
                ],
            },
        ) from exc
