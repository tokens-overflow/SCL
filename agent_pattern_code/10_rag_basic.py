"""
RAG 渐进系列 ② · Basic RAG（标准检索增强生成）
================================================
在 Naive RAG 基础上引入真正的"检索"环节：
  1. 文档分块（Chunking）—— 长文档切成小段
  2. TF-IDF 向量化 ——— 纯 Python 实现，零外部依赖
  3. 相似度检索 ———— 余弦相似度，取 top-k
  4. 上下文生成 ———— 只把相关块送给 LLM

对比 Naive RAG 的改进：
  · prompt 长度从「全部文档」降到「top-k 相关块」
  · 减少噪音 → 精度提升
  · 可扩展到数千文档（检索仍 O(n)，可优化为索引）

局限：
  · TF-IDF 对语义相似（同义词）无能为力
  · 没有查询改写 → 措辞不同可能漏掉相关文档
  · 没有重排 → top-1 不一定是最相关的

运行：python 10_rag_basic.py
依赖：pip install anthropic
"""

import math
import anthropic
from collections import Counter

client = anthropic.Anthropic()


# ============================================================
# 知识库（同 09_rag_naive.py，此处保持独立可运行）
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
# Step 1：文档分块（Chunking）
# 把长文档按句号/换行切成更小的段落，提升检索粒度
# ============================================================
def chunk_document(doc: dict, chunk_size: int = 150, overlap: int = 30) -> list[dict]:
    """
    将文档切成固定长度的块（字符数），相邻块有重叠。
    生产中通常按句子或段落切分；这里用字符数便于演示。
    """
    text = doc["text"]
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append({
            "doc_id":    doc["id"],
            "doc_title": doc["title"],
            "chunk_id":  f"{doc['id']}_c{len(chunks)}",
            "text":      text[start:end],
        })
        if end == len(text):
            break
        start += chunk_size - overlap   # overlap 保证上下文连续
    return chunks


def build_chunks(kb: list[dict]) -> list[dict]:
    all_chunks = []
    for doc in kb:
        all_chunks.extend(chunk_document(doc))
    return all_chunks


# ============================================================
# Step 2：TF-IDF 向量化（纯 Python，零外部依赖）
# 生产中替换为 text-embedding-3-small 等真实 embedding API
# ============================================================
def tokenize(text: str) -> list[str]:
    """
    简单分词：小写化 + 按空白/标点分割。
    生产中对中文应使用 jieba / pkuseg 等分词工具。
    """
    import re
    # 保留中文字符、字母、数字
    tokens = re.findall(r'[一-鿿]+|[a-zA-Z0-9]+', text.lower())
    return tokens


def build_index(chunks: list[dict]) -> tuple[list[dict], dict]:
    """
    构建 TF-IDF 索引。
    返回：(带 tfidf_vec 的 chunks 列表, idf 字典)
    """
    N = len(chunks)
    tokenized = [tokenize(c["text"]) for c in chunks]

    # IDF：log(N / (1 + df))
    df: dict[str, int] = Counter()
    for tokens in tokenized:
        df.update(set(tokens))
    idf = {term: math.log(N / (1 + cnt)) for term, cnt in df.items()}

    # TF-IDF 向量（稀疏 dict）
    indexed = []
    for chunk, tokens in zip(chunks, tokenized):
        tf = Counter(tokens)
        total = max(len(tokens), 1)
        vec = {t: (c / total) * idf.get(t, 0) for t, c in tf.items()}
        indexed.append({**chunk, "tfidf_vec": vec})

    return indexed, idf


def query_vec(query: str, idf: dict) -> dict[str, float]:
    tokens = tokenize(query)
    tf = Counter(tokens)
    total = max(len(tokens), 1)
    return {t: (c / total) * idf.get(t, 0) for t, c in tf.items()}


def cosine_sim(v1: dict, v2: dict) -> float:
    common = set(v1) & set(v2)
    if not common:
        return 0.0
    dot   = sum(v1[t] * v2[t] for t in common)
    norm1 = math.sqrt(sum(x * x for x in v1.values()))
    norm2 = math.sqrt(sum(x * x for x in v2.values()))
    return dot / (norm1 * norm2) if norm1 * norm2 > 0 else 0.0


# ============================================================
# Step 3：检索 top-k 相关块
# ============================================================
def retrieve(
    query: str,
    indexed_chunks: list[dict],
    idf: dict,
    top_k: int = 3,
    score_threshold: float = 0.01,
) -> list[dict]:
    """
    返回 top-k 相关块，按相似度降序排列。
    score_threshold 过滤掉完全不相关的块。
    """
    qv = query_vec(query, idf)
    scored = [
        {**chunk, "score": cosine_sim(qv, chunk["tfidf_vec"])}
        for chunk in indexed_chunks
    ]
    scored.sort(key=lambda x: x["score"], reverse=True)
    return [c for c in scored[:top_k] if c["score"] >= score_threshold]


# ============================================================
# Step 4：生成答案（Generate）
# ============================================================
def generate(question: str, retrieved: list[dict]) -> str:
    if not retrieved:
        return "抱歉，知识库中未找到与您问题相关的信息。"

    context = "\n\n".join([
        f"[{c['doc_title']}]（相似度 {c['score']:.3f}）\n{c['text']}"
        for c in retrieved
    ])

    resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=600,
        system=SYSTEM,
        messages=[{
            "role": "user",
            "content": f"参考文档：\n{context}\n\n问题：{question}"
        }]
    )
    return resp.content[0].text


# ============================================================
# Basic RAG 主函数
# ============================================================
def basic_rag(question: str, top_k: int = 3) -> str:
    print(f"\n{'='*60}")
    print(f"问题：{question}")
    print(f"{'='*60}")

    # 检索
    retrieved = retrieve(question, INDEXED_CHUNKS, IDF, top_k=top_k)

    print(f"检索到 {len(retrieved)} 个相关块：")
    for c in retrieved:
        print(f"  [{c['score']:.3f}] {c['doc_title']} — {c['text'][:50]}…")

    # 生成
    answer = generate(question, retrieved)
    print(f"\n回答：\n{answer}")
    return answer


# ============================================================
# 初始化索引（模块加载时构建一次）
# ============================================================
_CHUNKS       = build_chunks(KNOWLEDGE_BASE)
INDEXED_CHUNKS, IDF = build_index(_CHUNKS)


# ============================================================
# 测试
# ============================================================
if __name__ == "__main__":
    print("【Basic RAG 演示】TF-IDF 检索 + 分块")
    print(f"知识库：{len(KNOWLEDGE_BASE)} 篇文档 → {len(_CHUNKS)} 个块")

    basic_rag("免费版可以用于商业项目吗？")
    basic_rag("遇到 429 错误应该怎么处理？")
    basic_rag("Fine-tuning 最少需要多少训练数据？")
    basic_rag("专业版和企业版的 SLA 分别是多少？")
