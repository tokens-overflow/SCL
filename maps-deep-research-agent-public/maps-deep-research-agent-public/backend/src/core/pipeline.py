"""通用三段式流水线（Pipeline）。

一条 :class:`Pipeline` 就是一组按顺序排列的 :class:`Stage`。
每个 stage 拿到共享的 :class:`ResearchState` 和一个 :data:`EventEmitter`，
在 state 上原地修改、按需 emit 事件。Agent 在启动时装配一次 Pipeline，
之后每个 HTTP 请求都复用同一条流水线。

**整个流程只在这里被定义一次**::

    plan → execute → compose

* 加新阶段 = 在列表里追加一个 Stage；
* 调整 / 跳过阶段 = 在 Agent 构造时换一套 Stage 列表；
* Stage 之间互相不感知 —— 只通过 ResearchState 上的字段间接通信。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import ResearchState
from .events import EventEmitter


@runtime_checkable
class Stage(Protocol):
    """流水线中的一步：读写 state、按需发事件。"""

    name: str  # 日志 / 监控用的简短标识

    async def run(self, state: ResearchState, emit: EventEmitter) -> None:
        """对 state 执行本阶段。

        实现约定：
        * 读 state 上一阶段写入的字段；
        * 把本阶段结果回写到 state；
        * 通过 emit 报告进度（调用方可能丢弃）；
        * 不可恢复错误直接 raise，由 Agent 统一包装成 ErrorEvent。
        """
        ...


class Pipeline:
    """按顺序驱动一组 Stage 跑完同一个 state。"""

    def __init__(self, stages: list[Stage]) -> None:
        self._stages = stages

    @property
    def stages(self) -> list[Stage]:
        return list(self._stages)

    async def execute(self, state: ResearchState, emit: EventEmitter) -> None:
        """逐个 stage 串行执行；任意 stage 抛异常都直接向上冒泡。"""
        for stage in self._stages:
            await stage.run(state, emit)
