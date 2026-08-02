"""效果评估。

**「怎么判断它真的做完了，而不是看起来对」** —— 这是六根承重柱里最难的一根。

本模块提供两类评估：

1. **完成度判定（done-ness）**：基于**可验证的验收标准**，不是模型自我感觉良好。
   例如折扣任务的验收标准是：折扣记录存在 + 状态为 ACTIVE + 折扣率与请求一致。
   这些都能用一条 SQL 查出来，不需要问模型。

2. **回归评估（regression）**：用固定的输入集跑一遍，
   对比结构化输出与期望值。这是「Prompt 改了之后有没有变坏」的唯一可靠答案。

两者都刻意**不使用 LLM 做评判**。用模型评判模型在质量维度上有价值，
但在验收维度上不行——验收必须是确定性的，否则「通过」这个词就没有意义。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.runtime.models import AgentTask


@dataclass
class AcceptanceCriterion:
    """一条可验证的验收标准。

    Attributes:
        name: 标准名称。
        check: 判定函数，接收任务与业务事实，返回是否通过。
        description: 说明，出现在评估报告里。
        required: 是否必须通过。
    """

    name: str
    check: Callable[[AgentTask, dict[str, Any]], bool]
    description: str = ""
    required: bool = True


@dataclass
class EvaluationReport:
    """评估报告。"""

    task_id: str
    passed: bool
    results: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {"task_id": self.task_id, "passed": self.passed, "results": self.results}


class TaskEvaluator:
    """基于验收标准的任务完成度评估器。

    Example:
        >>> evaluator = TaskEvaluator(DISCOUNT_CRITERIA)
        >>> report = evaluator.evaluate(task, {"discount_rate": 0.05})
    """

    def __init__(self, criteria: list[AcceptanceCriterion]) -> None:
        self.criteria = criteria

    def evaluate(self, task: AgentTask, facts: dict[str, Any]) -> EvaluationReport:
        """执行评估。

        Args:
            task: 任务。
            facts: 从**业务系统查出来的事实**（不是从任务输出里读的）。
                这一点很关键：如果用任务自己记录的输出来验收，
                那验收的只是「我们有没有正确记录」，
                不是「外部世界是不是真的变成了预期的样子」。

        Returns:
            评估报告。
        """
        results: list[dict[str, Any]] = []
        passed = True
        for criterion in self.criteria:
            try:
                ok = criterion.check(task, facts)
            except Exception as exc:  # noqa: BLE001 - 评估本身不应该让流程崩
                ok = False
                results.append(
                    {
                        "name": criterion.name,
                        "passed": False,
                        "required": criterion.required,
                        "error": str(exc),
                    }
                )
                if criterion.required:
                    passed = False
                continue
            results.append(
                {
                    "name": criterion.name,
                    "passed": ok,
                    "required": criterion.required,
                    "description": criterion.description,
                }
            )
            if criterion.required and not ok:
                passed = False
        return EvaluationReport(task_id=task.task_id, passed=passed, results=results)


#: 折扣任务的验收标准（示例）。
#: 注意每一条都是**可以用一条查询验证**的，没有一条依赖模型判断。
DISCOUNT_ACCEPTANCE_CRITERIA: list[AcceptanceCriterion] = [
    AcceptanceCriterion(
        name="discount_record_exists",
        check=lambda task, facts: bool(facts.get("discount_id")),
        description="折扣记录已在计费系统中创建",
    ),
    AcceptanceCriterion(
        name="discount_rate_matches",
        check=lambda task, facts: (
            abs(float(facts.get("actual_rate", -1)) - float(facts.get("requested_rate", -2))) < 1e-9
        ),
        description="实际生效折扣率与申请一致（防止模型在中途改了数字）",
    ),
    AcceptanceCriterion(
        name="no_duplicate_discount",
        check=lambda task, facts: int(facts.get("active_discount_count", 0)) <= 1,
        description="同一客户不存在重复的生效折扣（幂等性验收）",
    ),
    AcceptanceCriterion(
        name="notification_attempted",
        check=lambda task, facts: bool(facts.get("notification_attempted", True)),
        description="通知已尝试发送",
        # 非必需：通知失败时任务可以是 PARTIAL_SUCCESS，这仍然是一个可接受的结局。
        required=False,
    ),
]
