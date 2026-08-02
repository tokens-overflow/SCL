"""故障注入开关（仅用于演示与测试）。

真实系统里没有这个模块——超时和失败会自己发生。但在一个**教学用的骨架**里，
必须能按需复现下面这些场景，否则「我们处理了超时」这句话是无法被验证的：

* 场景四：工具执行超时，但外部系统**其实已经成功**（最难的一种）；
* 场景四变体：超时，且外部系统**确实没执行**（可以安全重试）；
* 场景五：折扣成功但通知失败（部分成功）。

这个模块刻意做得非常简单：一个进程级的开关字典。
使用方式见 `tests/` 与 `app/examples/discount_workflow.py`。

Warning:
    生产部署应该把这个模块删掉，或者用 `settings.environment == "prod"` 做硬拦截。
    保留它只是为了让读者能亲手跑一遍这些路径。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

FaultMode = Literal[
    "none",
    # 超时，但**写入已经落到外部系统**。这是最危险的一种：
    # 如果按失败回滚，就凭空少一笔；如果直接重试，就多一笔。
    "timeout_after_commit",
    # 超时，且写入**没有发生**。对账查明后可以安全重试。
    "timeout_before_commit",
    # 明确的、可重试的技术失败（下游 5xx）。
    "transient_failure",
    # 明确的、不可重试的业务失败。
    "permanent_failure",
    # 进程崩溃模拟：抛出一个非 AgentError 的异常。
    "crash",
]


@dataclass
class FaultConfig:
    """单个工具的故障注入配置。

    Attributes:
        mode: 故障模式。
        remaining: 还要生效几次。用它可以精确表达
            「第一次超时、第二次成功」这类场景——
            这正是重试逻辑需要被验证的地方。
    """

    mode: FaultMode = "none"
    remaining: int = 1


@dataclass
class FaultInjector:
    """进程级故障注入器。"""

    faults: dict[str, FaultConfig] = field(default_factory=dict)

    def set(self, tool_name: str, mode: FaultMode, times: int = 1) -> None:
        """为某个工具设置故障。

        Args:
            tool_name: 工具名。
            mode: 故障模式。
            times: 生效次数。
        """
        self.faults[tool_name] = FaultConfig(mode=mode, remaining=times)

    def clear(self, tool_name: str | None = None) -> None:
        """清除故障配置。"""
        if tool_name is None:
            self.faults.clear()
        else:
            self.faults.pop(tool_name, None)

    def take(self, tool_name: str) -> FaultMode:
        """消费一次故障配置。

        Returns:
            本次应该触发的故障模式；没有配置时返回 ``"none"``。
        """
        cfg = self.faults.get(tool_name)
        if cfg is None or cfg.mode == "none" or cfg.remaining <= 0:
            return "none"
        cfg.remaining -= 1
        if cfg.remaining <= 0:
            self.faults.pop(tool_name, None)
        return cfg.mode


#: 全局故障注入器实例。
fault_injector = FaultInjector()
