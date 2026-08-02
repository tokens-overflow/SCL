"""风险聚合工具。

`RiskPolicy` 负责单次动作的风险评定；这个模块负责**任务级**的风险聚合：
一个任务由多个步骤组成，任务的风险等级应该是各步骤的最高值，
而不是最后一步的值——否则一个「查询 + 大额折扣 + 通知」的任务，
会因为最后一步是低风险通知而被整体标成低风险。
"""

from __future__ import annotations

from collections.abc import Iterable

from app.core.enums import RiskLevel


def aggregate_risk(levels: Iterable[RiskLevel | str]) -> RiskLevel:
    """聚合多个风险等级，返回最高的那个。

    Args:
        levels: 风险等级序列。

    Returns:
        最高风险等级；空序列返回 ``RiskLevel.NONE``。
    """
    highest = RiskLevel.NONE
    for level in levels:
        try:
            current = RiskLevel(level)
        except ValueError:
            # 无法识别的等级按最高处理：默认保守。
            current = RiskLevel.CRITICAL
        if current.order > highest.order:
            highest = current
    return highest


def requires_human(level: RiskLevel, threshold: RiskLevel = RiskLevel.HIGH) -> bool:
    """判断某个风险等级是否需要人工介入。"""
    return level.order >= threshold.order
