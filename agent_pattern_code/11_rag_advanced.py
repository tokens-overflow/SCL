"""
RAG 渐进系列 ③ · Advanced RAG（高级检索增强生成）
==================================================
在 Basic RAG 基础上引入工程化优化技术：

  ┌─────────────────────────────────────────────────────────┐
  │  技术栈                                                  │
  │  ① Query Rewriting   — LLM 改写问题，提升检索召回率      │
  │  ② Multi-Query       — 生成 3 个角度查询，取并集         │
  │  ③ HyDE              — 生成假设答案再检索，语义更精准     │
  │  ④ BM25 增强检索      — 词频优化，比 TF-IDF 更平衡       │
  │  ⑤ LLM Re-ranking    — 用 LLM 对 top-k 重新打分         │
  │  ⑥ Contextual Compression — LLM 提取块中最相关的句子    │
  │  ⑦ 置信度过滤         — 低相关度时拒绝回答               │
  └─────────────────────────────────────────────────────────┘

可通过 AdvancedRAG(strategy=...) 选择组合不同技术。

运行：python 11_rag_advanced.py
依赖：pip install anthropic
"""

import json
import math
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

SYSTEM = """你是 AiSphere 的智能客服助手。

规则：
1. 只使用「参考文档」中的信息回答，不得凭空推测
2. 回答末尾用【来源：文档标题】标注依据
3. 若文档中没有相关信息，明确说「文档中未找到相关信息」
4. 回答简洁，200 字以内"""


# ============================================================
# 基础检索层（BM25，比 TF-IDF 更平衡词频权重）
# ============================================================
def tokenize(text: str) -> list[str]:
    import re
    return re.findall(r'[一-鿿]+|[a-zA-Z0-9]+', text.lower())


def build_bm25_index(docs: list[dict], k1: float = 1.5, b: float = 0.75):
    """
    BM25 参数：
      k1 控制词频饱和度（1.2-2.0 常用），b 控制文档长度归一化（0=不归一化，1=完全归一化）
    """
    N = len(docs)
    tokenized = [tokenize(d["text"]) for d in docs]
    avg_len = sum(len(t) for t in tokenized) / max(N, 1)

    df: dict[str, int] = Counter()
    for tokens in tokenized:
        df.update(set(tokens))
    idf = {t: math.log((N - cnt + 0.5) / (cnt + 0.5) + 1) for t, cnt in df.items()}

    indexed = []
    for doc, tokens in zip(docs, tokenized):
        dl = len(tokens)
        tf = Counter(tokens)
        vec = {}
        for term, freq in tf.items():
            numerator   = freq * (k1 + 1)
            denominator = freq + k1 * (1 - b + b * dl / avg_len)
            vec[term]   = idf.get(term, 0) * numerator / denominator
        indexed.append({**doc, "bm25_vec": vec})

    return indexed, idf


def bm25_search(query: str, indexed: list[dict], idf: dict, top_k: int = 5) -> list[dict]:
    tokens = tokenize(query)
    scores = []
    for doc in indexed:
        score = sum(doc["bm25_vec"].get(t, 0) for t in tokens)
        scores.append((score, doc))
    scores.sort(key=lambda x: x[0], reverse=True)
    return [
        {**doc, "score": score}
        for score, doc in scores[:top_k]
        if score > 0
    ]


# ============================================================
# LLM 工具函数
# ============================================================
def llm(system: str, user: str, max_tokens: int = 600) -> str:
    resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return resp.content[0].text.strip()


def safe_json(text: str) -> dict | list:
    cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(cleaned)
    except Exception:
        return {}


# ============================================================
# 技术 ①：Query Rewriting（查询改写）
# 把用户口语化的问题改写成更适合检索的标准表达
# ============================================================
REWRITE_PROMPT = """你是一个搜索查询优化专家。

将用户问题改写为更适合文档检索的标准查询。要求：
- 保留核心意图
- 展开缩写（如"429" → "429 Too Many Requests 速率限制"）
- 去除口语化表达
- 输出一行，不加引号"""


def rewrite_query(question: str) -> str:
    return llm(REWRITE_PROMPT, f"原始问题：{question}", max_tokens=100)


# ============================================================
# 技术 ②：Multi-Query（多角度查询扩展）
# 从不同维度生成多个查询，取检索结果的并集，提升召回率
# ============================================================
MULTI_QUERY_PROMPT = """你是一个信息检索专家。

针对用户问题，从 3 个不同角度生成检索查询。

输出 JSON（只输出 JSON）：
{"queries": ["查询1", "查询2", "查询3"]}"""


def expand_queries(question: str) -> list[str]:
    result = safe_json(llm(MULTI_QUERY_PROMPT, f"问题：{question}", max_tokens=200))
    queries = result.get("queries", [question])
    return list(set([question] + queries))   # 原问题 + 扩展查询，去重


# ============================================================
# 技术 ③：HyDE（Hypothetical Document Embeddings）
# 让 LLM 先写一个"假设答案"，用假设答案检索比用问题本身更精准
# ============================================================
HYDE_PROMPT = """你是一个知识库文档作者。

根据用户问题，写一段可能出现在知识库中的答案文本（50字以内）。
不需要准确，只需语义接近真实答案即可。直接输出文本，不加解释。"""


def hypothetical_document(question: str) -> str:
    return llm(HYDE_PROMPT, f"问题：{question}", max_tokens=100)


# ============================================================
# 技术 ④：LLM Re-ranking（大模型重排）
# 让 LLM 对检索到的块按相关性重新打分，比向量相似度更准确
# ============================================================
RERANK_PROMPT = """你是一个相关性评分专家。

评估检索到的文档片段与用户问题的相关性，输出 0-10 的整数分。
10 = 完全相关且直接回答问题
5  = 部分相关，需要推断
0  = 不相关

只输出一个整数。"""


def rerank(question: str, chunks: list[dict]) -> list[dict]:
    """用 LLM 对每个块打分并重排（生产中用 cross-encoder 模型更高效）"""
    scored = []
    for chunk in chunks:
        score_str = llm(
            RERANK_PROMPT,
            f"问题：{question}\n\n文档片段：{chunk['text']}",
            max_tokens=10,
        )
        try:
            llm_score = float(score_str.strip())
        except ValueError:
            llm_score = chunk.get("score", 0) * 10
        scored.append({**chunk, "rerank_score": llm_score})

    scored.sort(key=lambda x: x["rerank_score"], reverse=True)
    return scored


# ============================================================
# 技术 ⑤：Contextual Compression（上下文压缩）
# 从每个检索块中提取与问题最直接相关的句子，减少噪音
# ============================================================
COMPRESS_PROMPT = """你是一个信息提取专家。

从文档片段中提取与问题直接相关的句子（保持原文，不改写）。
若整段都相关则完整返回。若完全不相关输出"[无相关内容]"。
只输出提取的文本，不加说明。"""


def compress_chunk(question: str, chunk: dict) -> dict:
    compressed = llm(
        COMPRESS_PROMPT,
        f"问题：{question}\n\n文档片段：{chunk['text']}",
        max_tokens=200,
    )
    if "[无相关内容]" in compressed:
        return None
    return {**chunk, "text": compressed}


# ============================================================
# Advanced RAG 主流程（可配置技术组合）
# ============================================================
@dataclass
class RAGConfig:
    top_k:            int   = 5      # 初始检索数量
    final_k:          int   = 3      # 重排后保留数量
    use_rewrite:      bool  = True   # 是否改写查询
    use_multi_query:  bool  = True   # 是否多角度查询
    use_hyde:         bool  = False  # 是否 HyDE（多一次 LLM 调用）
    use_rerank:       bool  = True   # 是否 LLM 重排
    use_compression:  bool  = False  # 是否上下文压缩（多次 LLM 调用）
    min_rerank_score: float = 3.0    # 重排分数低于此值拒绝回答


def advanced_rag(question: str, cfg: RAGConfig = RAGConfig()) -> str:
    print(f"\n{'='*60}")
    print(f"问题：{question}")
    print(f"{'='*60}")

    # ── 查询准备阶段 ──────────────────────────────────────────
    queries = [question]

    if cfg.use_rewrite:
        rewritten = rewrite_query(question)
        print(f"  改写查询：{rewritten}")
        queries = [rewritten]

    if cfg.use_multi_query:
        expanded = expand_queries(queries[0])
        queries = expanded
        print(f"  多角度查询（{len(queries)} 个）：{queries}")

    if cfg.use_hyde:
        hypo = hypothetical_document(question)
        queries.append(hypo)
        print(f"  HyDE 假设文档：{hypo[:60]}…")

    # ── 检索阶段（多查询取并集）────────────────────────────────
    seen_ids: set[str] = set()
    all_chunks: list[dict] = []
    for q in queries:
        results = bm25_search(q, INDEXED, IDF, top_k=cfg.top_k)
        for r in results:
            cid = r["id"]
            if cid not in seen_ids:
                seen_ids.add(cid)
                all_chunks.append(r)

    print(f"\n  初始检索：{len(all_chunks)} 个文档（{len(queries)} 个查询取并集）")

    # ── 重排阶段 ──────────────────────────────────────────────
    if cfg.use_rerank and all_chunks:
        print(f"  LLM 重排中…")
        all_chunks = rerank(question, all_chunks)
        top_chunks = [c for c in all_chunks[:cfg.final_k]
                      if c["rerank_score"] >= cfg.min_rerank_score]
        print(f"  重排后 top-{cfg.final_k}（过滤分数<{cfg.min_rerank_score}）：")
        for c in top_chunks:
            print(f"    [{c['rerank_score']:.0f}/10] {c['title']} — {c['text'][:50]}…")
    else:
        top_chunks = all_chunks[:cfg.final_k]

    if not top_chunks:
        return "抱歉，知识库中未找到足够相关的信息来回答您的问题。"

    # ── 上下文压缩 ────────────────────────────────────────────
    if cfg.use_compression:
        print(f"  上下文压缩中…")
        compressed = [compress_chunk(question, c) for c in top_chunks]
        top_chunks = [c for c in compressed if c is not None]

    # ── 生成答案 ──────────────────────────────────────────────
    context = "\n\n".join([
        f"[{c['title']}]\n{c['text']}"
        for c in top_chunks
    ])

    resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=600,
        system=SYSTEM,
        messages=[{"role": "user", "content": f"参考文档：\n{context}\n\n问题：{question}"}],
    )
    answer = resp.content[0].text
    print(f"\n  回答：\n{answer}")
    return answer


# ============================================================
# 初始化索引
# ============================================================
INDEXED, IDF = build_bm25_index(KNOWLEDGE_BASE)


# ============================================================
# 测试
# ============================================================
if __name__ == "__main__":
    print("【Advanced RAG 演示】BM25 + 查询改写 + 多查询 + LLM 重排")

    # 默认配置：改写 + 多查询 + 重排
    cfg = RAGConfig(use_hyde=False, use_compression=False)

    advanced_rag("免费版能用来做商业项目吗？", cfg)
    advanced_rag("API 请求太频繁报错了怎么办？", cfg)
    advanced_rag("我想微调模型，最少需要多少数据？", cfg)

    print("\n\n── HyDE 开启演示 ──")
    hyde_cfg = RAGConfig(use_hyde=True, use_compression=False)
    advanced_rag("数据会被用来训练模型吗？", hyde_cfg)
