"""三个 Pipeline Stage —— 一次 agent run 的三段。

每个 stage 是 ``llm_tasks/`` 和 ``execution/`` 的组合层，
唯一允许"混合 LLM 用例 + 工具调用 + 事件分发"的地方。其余模块都单一职责。

执行顺序：
    PlanStage    —— 研究主题 → 任务 DAG
    ExecuteStage —— 驱动 DAG，跑工具，流式总结每个任务
    ComposeStage —— 并行生成最终 Markdown 报告 + JSON 行程
"""
