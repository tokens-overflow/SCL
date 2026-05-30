"""LLM 用例：单个任务的证据 → 流式 Markdown 摘要。

* 输入：本任务的标题/意图 + 工具返回的证据文本
* 输出：流式（按 chunk 回调）累计出的最终 Markdown 字符串
"""

from __future__ import annotations

from pydantic import BaseModel

from ..core.llm_task import StreamingLLMTask
from ..prompts import summarizer_messages


class SummarizeInput(BaseModel):
    topic: str           # 整体研究主题，给 Prompt 上下文用
    task_title: str      # 当前正在总结的子任务名
    task_intent: str     # 该子任务要解决的问题
    evidence_block: str  # 工具产出的证据文本（Places / Routes / ...）


class SummarizeTask(StreamingLLMTask[SummarizeInput, str]):
    """流式生成单任务摘要；最终输出 = 累积 chunk 的 trim 字符串。"""

    def build_messages(self, input: SummarizeInput) -> list[dict[str, str]]:
        return summarizer_messages(
            topic=input.topic,
            task_title=input.task_title,
            task_intent=input.task_intent,
            evidence_block=input.evidence_block,
        )

    def parse(self, raw: str, input: SummarizeInput) -> str:
        # 流式调用已经在 stream() 内把 chunk 拼好，这里只做空白裁剪
        return raw.strip()
