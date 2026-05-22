"""
Self-Ask / Decomposition 范式完整示例
======================================
核心思想：复杂问题递归拆解成子问题，逐个击破后组合答案
         模拟人类解决复杂问题的自然思维方式

关键设计：
1. Decomposer：判断问题是否需要拆解，输出子问题列表
2. Solver：解决单个（已足够简单的）问题
3. Combiner：把子问题答案组合成最终答案
4. 递归：子问题本身可能还需要继续拆解
"""

import json
import anthropic

client = anthropic.Anthropic()

# ============================================================
# Prompt 1：Decomposer —— 判断是否需要拆解，输出子问题
# ============================================================
DECOMPOSER_SYSTEM = """你是一个问题分析专家。

判断一个问题是否可以直接回答，还是需要先解决若干子问题。

判断标准：
- 问题涉及多个独立的知识点或步骤 → 需要拆解
- 问题需要先获取某些前置信息才能回答 → 需要拆解
- 问题简单、直接，一步能回答 → 直接回答

输出严格的 JSON：
{
  "can_answer_directly": true/false,
  "reason": "判断原因",
  "sub_questions": [
    "子问题1（如果需要拆解）",
    "子问题2",
    ...
  ],
  "combination_strategy": "如何把子问题答案组合成最终答案（如果需要拆解）"
}

子问题要求：
- 每个子问题必须独立，可以单独回答
- 子问题要足够具体，比原问题更容易回答
- 数量控制在 2-4 个

只输出 JSON，不要其他文字。
"""

# ============================================================
# Prompt 2：Solver —— 解决单个简单问题
# ============================================================
SOLVER_SYSTEM = """你是一个知识专家。

直接、准确地回答提供的问题。

要求：
- 答案要具体、有内容
- 不要废话，直接给答案
- 如果是事实类问题，给出准确信息
- 如果是分析类问题，给出有理有据的分析
"""

# ============================================================
# Prompt 3：Combiner —— 组合子问题答案
# ============================================================
COMBINER_SYSTEM = """你是一个综合分析专家。

你会收到：
1. 原始问题
2. 若干子问题及其答案
3. 组合策略

你的任务：把所有子问题的答案整合成一个完整、连贯的最终答案。

要求：
- 答案必须直接回应原始问题
- 自然整合各子问题的内容，不要机械地罗列
- 有清晰的逻辑结构
- 长度适当，不要重复已知信息
"""


# ============================================================
# Decomposition 主函数（支持递归）
# ============================================================
def decompose_and_solve(question: str, depth: int = 0, max_depth: int = 3) -> str:
    """
    递归拆解并解决问题
    - depth：当前递归深度
    - max_depth：最大递归深度，防止无限递归
    """
    indent = "  " * depth
    print(f"\n{indent}{'─'*50}")
    print(f"{indent}问题（深度{depth}）：{question}")

    # 防止过深递归
    if depth >= max_depth:
        print(f"{indent}⚠️  达到最大深度，直接回答")
        return direct_solve(question)

    # Step 1：判断是否需要拆解
    decompose_response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=600,
        system=DECOMPOSER_SYSTEM,
        messages=[{"role": "user", "content": f"问题：{question}"}]
    )

    text = decompose_response.content[0].text.strip()
    text = text.replace("```json", "").replace("```", "").strip()

    try:
        analysis = json.loads(text)
    except json.JSONDecodeError:
        print(f"{indent}JSON 解析失败，直接回答")
        return direct_solve(question)

    print(f"{indent}分析：{analysis.get('reason', '')}")

    # Case 1：可以直接回答
    if analysis.get("can_answer_directly", True):
        print(f"{indent}✅ 直接回答")
        answer = direct_solve(question)
        print(f"{indent}答案：{answer[:100]}...")
        return answer

    # Case 2：需要拆解
    sub_questions = analysis.get("sub_questions", [])
    combination_strategy = analysis.get("combination_strategy", "综合各子问题答案")

    print(f"{indent}🔀 拆解为 {len(sub_questions)} 个子问题：")
    for i, sq in enumerate(sub_questions):
        print(f"{indent}  {i+1}. {sq}")

    # Step 2：递归解决每个子问题
    sub_answers = {}
    for sq in sub_questions:
        answer = decompose_and_solve(sq, depth + 1, max_depth)
        sub_answers[sq] = answer

    # Step 3：组合所有子答案
    print(f"\n{indent}🔗 [组合答案]")
    print(f"{indent}策略：{combination_strategy}")

    sub_qa_text = "\n\n".join([
        f"子问题：{q}\n答案：{a}"
        for q, a in sub_answers.items()
    ])

    combine_response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        system=COMBINER_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"""原始问题：{question}

子问题及答案：
{sub_qa_text}

组合策略：{combination_strategy}

请综合以上内容，给出原始问题的完整答案。"""
        }]
    )

    combined_answer = combine_response.content[0].text
    print(f"{indent}组合结果：{combined_answer[:100]}...")
    return combined_answer


def direct_solve(question: str) -> str:
    """直接回答简单问题"""
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        system=SOLVER_SYSTEM,
        messages=[{"role": "user", "content": question}]
    )
    return response.content[0].text


# ============================================================
# 封装入口：带完整输出的版本
# ============================================================
def self_ask_agent(question: str) -> str:
    print(f"\n{'='*60}")
    print(f"Self-Ask Agent")
    print(f"问题：{question}")
    print(f"{'='*60}")

    answer = decompose_and_solve(question, depth=0, max_depth=3)

    print(f"\n{'='*60}")
    print(f"最终答案：\n{answer}")
    return answer


# ============================================================
# 测试
# ============================================================
if __name__ == "__main__":
    # 测试1：简单问题（不需要拆解）
    self_ask_agent("Python 的 GIL 是什么？")

    print("\n\n")

    # 测试2：需要拆解的复杂问题
    self_ask_agent(
        "我想用 Python 开发一个 Web 应用并部署到云上，需要掌握哪些技术，大概需要多长时间学习？"
    )

    print("\n\n")

    # 测试3：多层嵌套（子问题还需要拆解）
    self_ask_agent(
        "比较 React 和 Vue 在大型企业项目中的优劣，并给出技术选型建议"
    )
