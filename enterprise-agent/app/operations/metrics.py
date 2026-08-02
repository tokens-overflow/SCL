"""指标采集。

运营层的一个标准症状是：「上周还好好的，这周不行了，但没人说得清哪里变了」。
指标就是用来消除这句话的。

本模块提供**进程内的最小实现**：计数器、直方图、以及按任务归因的成本统计。
接口刻意对齐 Prometheus 的心智模型（counter / histogram + labels），
需要接入真实监控时替换 `MetricsCollector` 的实现即可，调用方不用改。

必须采集的四类指标（对应架构文档的运营层要求）：

* 成功率：按任务类型、按工具；
* 延迟：端到端、分步骤；
* Token 与成本：**按任务归因**，否则算不出「这次编排花了多少钱」；
* 重试 / 审批 / 补偿次数：这三个是系统健康度的先行指标——
  它们上涨通常早于成功率下跌。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class MetricsCollector:
    """进程内指标收集器（线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = defaultdict(list)
        self._cost_by_task: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    @staticmethod
    def _key(name: str, labels: dict[str, str] | None) -> tuple[str, tuple[tuple[str, str], ...]]:
        return name, tuple(sorted((labels or {}).items()))

    def increment(self, name: str, value: float = 1.0, **labels: str) -> None:
        """累加计数器。"""
        with self._lock:
            self._counters[self._key(name, labels)] += value

    def observe(self, name: str, value: float, **labels: str) -> None:
        """记录一次观测值（延迟、大小等）。"""
        with self._lock:
            self._histograms[self._key(name, labels)].append(value)

    @contextmanager
    def timer(self, name: str, **labels: str) -> Iterator[None]:
        """计时上下文管理器。

        Example:
            >>> with metrics.timer("tool_latency_seconds", tool="apply_discount"):
            ...     await tool.execute(...)
        """
        started = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, time.perf_counter() - started, **labels)

    def record_cost(self, task_id: str, *, tokens_in: int, tokens_out: int, amount: float = 0.0) -> None:
        """按任务归因 LLM 成本。

        为什么必须按任务归因：并行跑五个子 Agent 时，成本大致是单 Agent 的
        五倍以上（每个都带完整上下文），而不是五分之一。
        没有按任务的成本数据，这种「架构看起来高级但成本翻五倍」的问题
        要到账单出来才会被发现。
        """
        with self._lock:
            bucket = self._cost_by_task[task_id]
            bucket["tokens_in"] += tokens_in
            bucket["tokens_out"] += tokens_out
            bucket["amount"] += amount
            bucket["llm_calls"] += 1

    def task_cost(self, task_id: str) -> dict[str, float]:
        """取某任务的累计成本。"""
        with self._lock:
            return dict(self._cost_by_task.get(task_id, {}))

    def snapshot(self) -> dict[str, Any]:
        """导出当前所有指标（用于 `/admin/metrics`）。"""
        with self._lock:
            counters = {
                f"{name}{_render_labels(labels)}": value
                for (name, labels), value in self._counters.items()
            }
            histograms = {}
            for (name, labels), values in self._histograms.items():
                if not values:
                    continue
                ordered = sorted(values)
                histograms[f"{name}{_render_labels(labels)}"] = {
                    "count": len(ordered),
                    "sum": round(sum(ordered), 6),
                    "avg": round(sum(ordered) / len(ordered), 6),
                    "p50": round(ordered[len(ordered) // 2], 6),
                    # p95 在小样本下没什么意义，但接口先留着，
                    # 生产接入真实监控后自然会有足够样本。
                    "p95": round(ordered[int(len(ordered) * 0.95)], 6),
                    "max": round(ordered[-1], 6),
                }
            return {
                "counters": counters,
                "histograms": histograms,
                "cost_by_task": {k: dict(v) for k, v in self._cost_by_task.items()},
            }

    def reset(self) -> None:
        """清空所有指标（测试用）。"""
        with self._lock:
            self._counters.clear()
            self._histograms.clear()
            self._cost_by_task.clear()


def _render_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{v}"' for k, v in labels)
    return "{" + inner + "}"


#: 全局指标收集器。
metrics = MetricsCollector()
