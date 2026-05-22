"""
RAG 渐进系列 ① · Naive RAG（文档填充法 / Stuffing）
=====================================================
最原始的 RAG：把所有文档直接拼接成 prompt 的一部分。
不做任何检索，依靠 LLM 的长上下文处理能力。

适用场景：
  · 文档极少（< 20 篇短文档）
  · 快速原型验证
  · 上下文窗口足够容纳所有文档

局限：
  · 文档多时超出上下文窗口
  · 大量无关内容 → 精度下降 + API 成本高
  · 无法扩展到大型知识库

运行：python 09_rag_naive.py
依赖：pip install anthropic
"""

import anthropic

client = anthropic.Anthropic()


# ============================================================
# 共用知识库：AiSphere 虚拟 AI 平台
# （09 ~ 12 四个 RAG 示例均使用同一知识库）
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


# ============================================================
# System Prompt（四个示例共用）
# ============================================================
SYSTEM = """你是 AiSphere 的智能客服助手。

规则：
1. 只使用「参考文档」中的信息回答，不得凭空推测
2. 回答末尾用【来源：文档标题】标注依据
3. 若文档中没有相关信息，明确说「文档中未找到相关信息」
4. 回答简洁，200 字以内"""


# ============================================================
# Naive RAG：直接把所有文档拼进 prompt
# ============================================================
def naive_rag(question: str) -> str:
    # 把整个知识库拼成一个字符串
    all_docs = "\n\n".join([
        f"【{doc['title']}】\n{doc['text']}"
        for doc in KNOWLEDGE_BASE
    ])

    print(f"\n{'='*60}")
    print(f"问题：{question}")
    print(f"塞入文档：{len(KNOWLEDGE_BASE)} 篇  |  总字符数：{len(all_docs)}")
    print(f"{'='*60}")

    resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=600,
        system=SYSTEM,
        messages=[{
            "role": "user",
            "content": f"参考文档：\n{all_docs}\n\n问题：{question}"
        }]
    )
    answer = resp.content[0].text
    print(f"回答：\n{answer}")
    return answer


# ============================================================
# 测试
# ============================================================
if __name__ == "__main__":
    print("【Naive RAG 演示】把所有文档塞进 prompt")
    print(f"知识库规模：{len(KNOWLEDGE_BASE)} 篇文档  "
          f"总字符：{sum(len(d['text']) for d in KNOWLEDGE_BASE)}")

    naive_rag("免费版可以用于商业项目吗？")
    naive_rag("遇到 429 错误应该怎么处理？")
    naive_rag("专业版和企业版的 SLA 保障分别是多少？")
