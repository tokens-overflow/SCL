"""
RAG 渐进系列 ④ · Agentic RAG（智能体驱动检索）
================================================
Agentic RAG 与前三种的根本区别：

  前三种（Naive / Basic / Advanced）：
    检索策略固定，每次都按同样流程走
    用户问 → 检索 → 生成 → 返回

  Agentic RAG：
    LLM Agent 自主决定 WHEN / WHAT / HOW MANY 次检索
    Agent 拥有工具，用 ReAct 循环驱动多步推理

  ┌─────────────────────────────────────────────────────────┐
  │  Agent 工具箱                                            │
  │  · search(query)           全文检索，返回 top-k 文档     │
  │  · get_document(doc_id)    获取完整文档                  │
  │  · check_answer_quality    自评：现有信息够回答吗？       │
  └─────────────────────────────────────────────────────────┘

  Agent 能力：
  · 简单问题：直接回答，不调用检索工具（减少 latency）
  · 需要检索：一次或多次 search，直到信息足够
  · 多跳推理：先检索 A，再用 A 的结果检索 B（Multi-hop）
  · 主动放弃：若多次检索仍无法回答，明确说明不确定

  代表性多跳场景：
    "我用免费版，能开发付费 SaaS 吗？"
    → 需要同时知道「免费版限制」和「商业使用条款」两个文档

运行：python 12_rag_agentic.py
依赖：pip install anthropic
"""

import json
import math
import re
import anthropic
from collections import Counter
from dataclasses import dataclass, field

client = anthropic.Anthropic()


# ============================================================
# 知识库（同前）
# ============================================================
KNOWLEDGE_BASE = [
    {"id": "kb01", "title": "产品概述",
     "text": "AiSphere 是企业级 AI 应用开发平台，提供 API、SDK 和无代码工具。"
             "核心产品：AiSphere API（大模型调用）、Studio（可视化编排）、Knowledge（知识库管理）。"},
    {"id": "kb02", "title": "免费版（Free Plan）",
     "text": "免费版：每月 100,000 tokens，仅限个人非商业用途，不支持 SLA，"
             "社区支持，不可用于生产环境，每分钟最多 10 次请求（RPM）。"},
    {"id": "kb03", "title": "专业版（Pro Plan）",
     "text": "专业版：¥299/月，含 2,000,000 tokens，支持商业用途，99.5% SLA，"
             "邮件支持，100 RPM，支持 Fine-tuning，数据存储 1 GB。"},
    {"id": "kb04", "title": "企业版（Enterprise Plan）",
     "text": "企业版：按需定价，无限 tokens，99.99% SLA，专属客服经理，"
             "私有云/混合云部署，SSO + 审计日志，数据不出境，1000 RPM（可申请提升）。"},
    {"id": "kb05", "title": "商业使用条款",
     "text": "商业使用（Commercial Use）指用于产生收入的场景：SaaS 产品、付费服务、"
             "有 ROI 的内部工具。免费版明确禁止商业使用。专业版及以上可商用，需遵守内容政策。"},
    {"id": "kb06", "title": "API 认证",
     "text": "使用 Bearer Token：Authorization: Bearer YOUR_API_KEY。"
             "在控制台「设置 > API Keys」创建，每账户最多 10 个 Key，建议按项目隔离。"},
    {"id": "kb07", "title": "速率限制（Rate Limit）",
     "text": "免费版 10 RPM，专业版 100 RPM，企业版 1000 RPM（可申请提升）。"
             "超出返回 HTTP 429，建议指数退避重试（1s, 2s, 4s）。"},
    {"id": "kb08", "title": "错误码参考",
     "text": "400 请求格式错误 | 401 API Key 无效/过期 | 403 无权访问（检查套餐限制）"
             "| 429 超出速率限制 | 500 服务端错误（可重试）。"},
    {"id": "kb09", "title": "Fine-tuning（模型微调）",
     "text": "专业版及以上可用。训练数据：JSONL 格式，最少 100 条，最多 100,000 条。"
             "训练时长 2-8 小时。费用 ¥0.12/1000 训练 tokens。微调模型通过 model_id 调用。"},
    {"id": "kb10", "title": "数据安全",
     "text": "传输：TLS 1.3。存储：AES-256。用户数据默认不用于模型训练。"
             "企业版支持数据不出境（国内节点）。ISO 27001、SOC 2 Type II、GDPR 合规。"},
    {"id": "kb11", "title": "SLA 服务协议",
     "text": "免费版无 SLA。专业版 99.5%（每月可中断约 3.6 小时）。"
             "企业版 99.99%（每月可中断约 4.3 分钟）。"
             "SLA 违约：专业版下月免费，企业版按比例退款。"},
    {"id": "kb12", "title": "退款政策",
     "text": "专业版 7 天无理由退款（使用未超过 10% tokens）。"
             "企业版按合同约定。Token 用量不可退。联系 billing@aisphere.ai。"},
    {"id": "kb13", "title": "AiSphere Knowledge（内置 RAG）",
     "text": "内置 RAG 服务：支持 PDF/Word/TXT/Markdown，自动分块+向量化，REST API 查询，"
             "中英文混合检索。专业版 1 GB 存储，企业版 100 GB 存储。"},
    {"id": "kb14", "title": "客户支持渠道",
     "text": "免费版：社区论坛，响应不保证。专业版：邮件 support@aisphere.ai，工作日 24h 响应。"
             "企业版：专属客服经理 + 7×24 电话 + 微信群，P0/P1 问题 1 小时内响应。"},
    {"id": "kb15", "title": "从 OpenAI 迁移",
     "text": "兼容 OpenAI API 格式，修改 base_url 即可迁移。"
             "支持从 Azure OpenAI 迁移。提供迁移工具 migration-tool.aisphere.ai。"
             "企业版提供专属迁移支持。"},
]

KB_INDEX = {doc["id"]: doc for doc in KNOWLEDGE_BASE}


# ============================================================
# 检索层（BM25，复用自 11_rag_advanced.py）
# ============================================================
def tokenize(text: str) -> list[str]:
    return re.findall(r'[一-鿿]+|[a-zA-Z0-9]+', text.lower())


def build_bm25(docs: list[dict], k1: float = 1.5, b: float = 0.75):
    N   = len(docs)
    tok = [tokenize(d["text"]) for d in docs]
    avg = sum(len(t) for t in tok) / max(N, 1)

    df: dict[str, int] = Counter()
    for t in tok:
        df.update(set(t))
    idf = {t: math.log((N - f + 0.5) / (f + 0.5) + 1) for t, f in df.items()}

    indexed = []
    for doc, tokens in zip(docs, tok):
        dl = len(tokens)
        tf = Counter(tokens)
        vec = {}
        for term, freq in tf.items():
            vec[term] = idf.get(term, 0) * freq * (k1 + 1) / \
                        (freq + k1 * (1 - b + b * dl / avg))
        indexed.append({**doc, "_vec": vec})
    return indexed, idf


def bm25_search(query: str, indexed: list[dict], top_k: int = 3) -> list[dict]:
    tokens = tokenize(query)
    scored = sorted(
        [({**d, "score": sum(d["_vec"].get(t, 0) for t in tokens)}) for d in indexed],
        key=lambda x: x["score"],
        reverse=True,
    )
    return [d for d in scored[:top_k] if d["score"] > 0]


INDEXED, _ = build_bm25(KNOWLEDGE_BASE)


# ============================================================
# Agent 工具实现
# ============================================================
def tool_search(query: str, top_k: int = 3) -> str:
    """全文检索，返回 top-k 最相关文档的摘要"""
    results = bm25_search(query, INDEXED, top_k=top_k)
    if not results:
        return "未找到相关文档。"
    lines = [f"[{r['id']}] {r['title']}（相关度 {r['score']:.2f}）\n  {r['text'][:120]}…"
             for r in results]
    return "\n".join(lines)


def tool_get_document(doc_id: str) -> str:
    """获取指定 ID 的完整文档内容"""
    doc = KB_INDEX.get(doc_id.strip())
    if not doc:
        return f"文档 {doc_id} 不存在。可用 ID：{', '.join(KB_INDEX.keys())}"
    return f"[{doc['title']}]\n{doc['text']}"


TOOLS = {
    "search":       tool_search,
    "get_document": tool_get_document,
}

TOOL_DESC = """你可以使用以下工具：

1. search(query: str) → str
   全文搜索知识库，返回最相关的 3 篇文档摘要。
   示例：search("免费版商业使用限制")

2. get_document(doc_id: str) → str
   获取指定文档的完整内容（doc_id 格式如 kb01, kb05）。
   示例：get_document("kb05")
   适用场景：search 结果摘要不够详细时，获取完整原文。"""


# ============================================================
# Agentic RAG System Prompt
# ============================================================
AGENT_SYSTEM = f"""你是 AiSphere 的智能客服 Agent。

{TOOL_DESC}

工作流程（ReAct 循环）：
1. 分析问题 → 判断是否需要检索（简单问候/常识问题可直接回答）
2. 调用工具 → 格式：Action: tool_name(参数)
3. 等待结果 → 结果以 Observation: 开头
4. 继续推理 → 信息足够则给出最终答案，不够则继续检索
5. 多跳检索 → 用前一步结果指导下一步查询（Multi-hop）

输出格式：
Thought: 分析和推理
Action: tool_name(参数)   ← 每次只调用一个工具
（等待 Observation 后继续）
...
Thought: 已有足够信息
Answer: 最终答案（标注【来源：文档标题】）

规则：
- 只使用文档中的信息，不得凭空推测
- 若 3 次检索后仍无法回答，诚实说明并建议联系客服
- 最终答案简洁，200 字以内"""


# ============================================================
# Agentic RAG 执行引擎
# ============================================================
@dataclass
class AgentState:
    question:    str
    messages:    list = field(default_factory=list)
    steps:       int  = 0
    tool_calls:  list = field(default_factory=list)   # 记录调用历史
    context_docs: list = field(default_factory=list)   # 已检索到的文档


def parse_action(text: str) -> tuple[str, str] | None:
    """解析 'Action: tool_name(args)' 格式"""
    m = re.search(r'Action:\s*(\w+)\((.+?)\)', text, re.DOTALL)
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip().strip('"\'')


def run_agentic_rag(question: str, max_steps: int = 6) -> str:
    state = AgentState(question=question)
    state.messages = [{"role": "user", "content": question}]

    print(f"\n{'='*60}")
    print(f"问题：{question}")
    print(f"{'='*60}")

    for step in range(max_steps):
        state.steps += 1

        # ── LLM 推理 ──────────────────────────────────────────
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            system=AGENT_SYSTEM,
            messages=state.messages,
            stop_sequences=["Observation:"],   # 遇到 Observation 停止，等待工具结果
        )
        output = resp.content[0].text
        print(f"\n[Step {step + 1}]\n{output.rstrip()}")

        # ── 检查是否已有最终答案 ─────────────────────────────────
        if "Answer:" in output:
            answer = output.split("Answer:", 1)[-1].strip()
            print(f"\n{'─'*60}")
            print(f"最终答案：\n{answer}")
            print(f"{'─'*60}")
            print(f"统计：共 {state.steps} 步，调用工具 {len(state.tool_calls)} 次")
            return answer

        # ── 解析并执行工具调用 ─────────────────────────────────
        action = parse_action(output)
        if not action:
            # 没有 Action 也没有 Answer → 异常情况，要求 Agent 继续
            state.messages.append({"role": "assistant", "content": output})
            state.messages.append({"role": "user",      "content": "请继续推理或给出最终答案。"})
            continue

        tool_name, tool_args = action
        print(f"\nObservation: ", end="", flush=True)

        if tool_name in TOOLS:
            observation = TOOLS[tool_name](tool_args)
            state.tool_calls.append({"tool": tool_name, "args": tool_args})
            # 简单记录检索到的文档（用于 context 追踪）
            if tool_name == "search":
                state.context_docs.append(f"search({tool_args})")
        else:
            observation = f"工具 '{tool_name}' 不存在，可用工具：search, get_document"

        print(observation[:200] + ("…" if len(observation) > 200 else ""))

        # ── 把工具结果追加回 messages，驱动下一轮推理 ─────────
        state.messages.append({
            "role":    "assistant",
            "content": output + "Observation:",
        })
        state.messages.append({
            "role":    "user",
            "content": f" {observation}\n",
        })

    return "已达到最大推理步数，建议联系 support@aisphere.ai 获取人工支持。"


# ============================================================
# 测试：展示 Agentic RAG 的三个核心能力
# ============================================================
if __name__ == "__main__":
    print("【Agentic RAG 演示】Agent 自主决定检索策略")
    print("=" * 60)

    # ── 场景 1：简单问题，Agent 可能直接回答（无需检索）──────
    print("\n\n━━ 场景 1：简单问题 ━━")
    run_agentic_rag("AiSphere 是什么公司的产品？")

    # ── 场景 2：单跳检索 ─────────────────────────────────────
    print("\n\n━━ 场景 2：单跳检索 ━━")
    run_agentic_rag("API 调用频率超限了，返回什么错误？如何处理？")

    # ── 场景 3：多跳推理（Multi-hop）─────────────────────────
    # 需要同时知道「免费版限制」AND「商业使用条款」才能回答
    print("\n\n━━ 场景 3：多跳推理（最能体现 Agentic 价值）━━")
    run_agentic_rag(
        "我现在用免费版，想开发一个向用户收费的 SaaS 产品，"
        "请告诉我可不可以，以及如果不行应该升级到哪个套餐？"
    )

    # ── 场景 4：信息不足时主动放弃 ───────────────────────────
    print("\n\n━━ 场景 4：知识库中没有答案 ━━")
    run_agentic_rag("AiSphere 的 CEO 是谁？最近有没有融资新闻？")
