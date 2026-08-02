"""检索接口（RAG）。

**RAG 是「检索并回填上下文」的方法，不是单独的一层存储。**
所以它在这里是 Context 组件的一个可替换实现，而不是架构里的第七层。

默认实现用内存里的业务知识片段 + 关键词打分，刻意不引入向量数据库：

* 企业里大量场景的知识规模只有几十上百条，关键词检索完全够用；
* 过早引入向量库会带来一整套运维成本，而收益要在数据规模上来之后才出现；
* 更重要的是——**接口稳定的前提下，换成向量库是一件局部改动**。
  今天用 SQLite，明天换 pgvector 或 Qdrant，`Retriever` 协议不用变。
"""

from __future__ import annotations

import re
from typing import Protocol

from app.context.models import AgentContext, RetrievedDocument


class Retriever(Protocol):
    """检索器接口。

    实现方只需要保证：给定查询和上下文，返回按相关度降序的文档列表。
    """

    async def retrieve(
        self,
        query: str,
        context: AgentContext,
        limit: int = 5,
    ) -> list[RetrievedDocument]:
        """检索相关文档。"""
        ...


#: Demo 知识库。真实项目里这些内容会来自 Wiki / 制度文档 / 产品手册。
#: 注意：**业务硬规则（折扣上限等）虽然写在这里给模型看，但它同时被
#: 控制层以代码形式实现了一遍**。知识库里的这份只是让模型少提无效方案，
#: 绝不是执行依据——文档改错了不会导致越权，代码才是红线。
DEMO_KNOWLEDGE: list[RetrievedDocument] = [
    RetrievedDocument(
        doc_id="kb_discount_policy",
        title="折扣审批制度",
        content=(
            "普通客服最多可自助批准 5% 折扣；5%~15% 需要客服经理审批；"
            "超过 15% 一律拒绝，不接受任何形式的特批。"
        ),
        source="内部制度库",
    ),
    RetrievedDocument(
        doc_id="kb_vip_policy",
        title="VIP 客户特殊规则",
        content=(
            "VIP 客户可在标准额度基础上额外放宽 3 个百分点，"
            "但仍然要经过控制层判定，不存在自动放行。"
        ),
        source="内部制度库",
    ),
    RetrievedDocument(
        doc_id="kb_duplicate_discount",
        title="重复折扣约束",
        content="同一客户已有生效折扣时，不允许重复创建新的折扣记录，需先撤销旧折扣。",
        source="内部制度库",
    ),
    RetrievedDocument(
        doc_id="kb_notification",
        title="客户通知规范",
        content=(
            "折扣生效后需通知客户。通知失败可以重试，"
            "但绝不允许因为通知失败而重复创建折扣。"
        ),
        source="内部制度库",
    ),
    RetrievedDocument(
        doc_id="kb_refund",
        title="退款流程",
        content="退款需要财务系统权限，客服 Agent 无权发起退款，应转交财务工单。",
        source="内部制度库",
    ),
]


def _tokenize(text: str) -> list[str]:
    """极简分词：英文按词、中文按字。

    刻意保持简单——检索质量在这个规模上够用，而且没有任何外部依赖。
    需要更好的效果时，把这个函数换成 jieba 或直接换成向量检索即可，
    `Retriever` 接口不受影响。
    """
    text = text.lower()
    tokens = re.findall(r"[a-z0-9_]+", text)
    tokens += re.findall(r"[一-鿿]", text)
    return tokens


class InMemoryRetriever:
    """基于关键词重合度的内存检索器（默认实现）。

    Attributes:
        documents: 知识库文档列表。
    """

    def __init__(self, documents: list[RetrievedDocument] | None = None) -> None:
        self.documents = list(documents if documents is not None else DEMO_KNOWLEDGE)

    async def retrieve(
        self,
        query: str,
        context: AgentContext,
        limit: int = 5,
    ) -> list[RetrievedDocument]:
        """按关键词重合度检索。

        Args:
            query: 查询串，通常是用户输入。
            context: 当前上下文。这里用它做**任务类型加权**——
                折扣任务优先命中折扣相关文档，这比纯文本相似度稳定得多。
            limit: 返回条数上限。上限存在的意义是控制 token 成本，
                不是为了「返回得少一点好看」。

        Returns:
            按分值降序的文档列表。分值为 0 的文档不会返回——
            **宁可不给，也不要给一堆不相关的内容把关键信息淹没。**
        """
        query_tokens = set(_tokenize(query))
        scored: list[tuple[float, RetrievedDocument]] = []

        for doc in self.documents:
            doc_tokens = set(_tokenize(f"{doc.title} {doc.content}"))
            overlap = len(query_tokens & doc_tokens)
            if overlap == 0:
                continue
            score = overlap / max(len(query_tokens), 1)
            # 任务类型加权：折扣任务里，折扣相关文档天然更重要。
            if context.task_type and context.task_type.split("_")[0] in doc.doc_id:
                score += 0.5
            scored.append((score, doc))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            doc.model_copy(update={"score": round(score, 4)})
            for score, doc in scored[:limit]
        ]


class NullRetriever:
    """空检索器：永远返回空列表。

    用途：需要**验证「没有 RAG 时系统仍能正确工作」**的测试场景。
    这一点很重要——如果去掉检索之后业务就跑不通了，说明业务硬规则
    被错误地依赖在了知识库文本上，而不是落在控制层代码里。
    """

    async def retrieve(
        self,
        query: str,
        context: AgentContext,
        limit: int = 5,
    ) -> list[RetrievedDocument]:
        """始终返回空结果。"""
        return []


#: 默认检索器实例。
default_retriever: Retriever = InMemoryRetriever()
