"""DAG 执行原语（拓扑调度器 + 单任务工具执行器）。

属于"业务感知但 Stage 无感知"层：
    * 知道 ``TaskNode`` / ``ToolRegistry``
    * 但不知道 LLM、不发事件

被 ``ExecuteStage`` 组合使用，叠加上 LLM 流式总结 + SSE 事件。
"""
