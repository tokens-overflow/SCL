"""
终极综合 Agent：六大范式协同运作
===================================

场景：自动化技术研究助手
任务：给定一个技术问题，自动完成 研究→规划→执行→反思→输出 全流程

融合的范式：
┌─────────────────────────────────────────────────────┐
│  1. Self-Ask       → 问题拆解，确定研究方向          │
│  2. Plan & Execute → 规划研究步骤，有序执行          │
│  3. ReAct          → 每个执行步骤内部的工具调用       │
│  4. Multi-Agent    → 不同专家 Agent 负责不同子任务    │
│  5. Reflection     → 对输出质量进行自我审查           │
│  6. ToT（轻量版）  → 对关键决策展开多方案评估         │
└─────────────────────────────────────────────────────┘

数据流：
用户问题
    ↓ [Self-Ask] 拆解子问题
    ↓ [ToT] 评估研究方向，选最优
    ↓ [Plan & Execute] 生成执行计划
    ↓ [Multi-Agent + ReAct] 分派专家执行，工具辅助
    ↓ [Reflection] 审查输出质量
    ↓ 最终报告
"""

import json
import re
import anthropic
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.Anthropic()


# ============================================================
# 数据结构
# ============================================================
@dataclass
class ResearchState:
    """全局状态：贯穿整个 Agent 生命周期"""
    original_question: str
    sub_questions: list[str] = field(default_factory=list)
    research_direction: str = ""
    execution_plan: list[dict] = field(default_factory=list)
    step_results: list[dict] = field(default_factory=list)
    final_report: str = ""
    reflection_rounds: int = 0


# ============================================================
# 所有 Prompts
# ============================================================

# ── Self-Ask：问题拆解 ────────────────────────────────────────
DECOMPOSER_PROMPT = """你是一个研究问题分析专家。

把复杂的研究问题拆解成 2-4 个独立的子研究方向。

输出 JSON：
{
  "sub_questions": ["子问题1", "子问题2", ...],
  "core_challenge": "核心挑战是什么"
}
只输出 JSON。"""

# ── ToT：研究方向评估 ─────────────────────────────────────────
DIRECTION_EVALUATOR_PROMPT = """你是一个研究策略专家。

评估一个研究方向的价值，从 实用性/深度/可行性 三个维度打分。

输出 JSON：
{
  "score": 0-10,
  "strengths": "优势",
  "weaknesses": "劣势"
}
只输出 JSON。"""

DIRECTION_GENERATOR_PROMPT = """你是一个研究策略专家。

针对给定问题，提出 {n} 个不同的研究切入角度。每个角度要有实质差异。

输出 JSON：
{
  "directions": ["方向1", "方向2", ...]
}
只输出 JSON。"""

# ── Plan & Execute：规划 ──────────────────────────────────────
PLANNER_PROMPT = """你是一个研究规划专家。

根据研究问题和方向，制定详细的执行计划。

输出 JSON：
{
  "steps": [
    {
      "id": 1,
      "title": "步骤标题",
      "agent": "researcher/analyst/writer",
      "task": "具体任务描述",
      "depends_on": []
    }
  ]
}
只输出 JSON。"""

# ── Multi-Agent：各专家 ───────────────────────────────────────
RESEARCHER_PROMPT = """你是一个技术研究专家。

深入研究给定的技术问题，提供：
- 核心概念解释
- 现状与发展趋势
- 关键数据和案例
- 相关技术对比

内容要准确、专业、有深度。"""

ANALYST_PROMPT = """你是一个技术分析专家。

对提供的研究内容进行深度分析：
- 识别关键洞察
- 找出规律和趋势
- 提出独到见解
- 指出潜在问题和机会

分析要有逻辑，有理有据。"""

WRITER_PROMPT = """你是一个技术写作专家。

将研究和分析内容整合成结构清晰的技术报告：
- 执行摘要（2-3句）
- 核心发现（3-5条）
- 详细分析
- 结论与建议

语言专业但易读，有清晰的层次结构。"""

# ── ReAct：工具调用（嵌套在 Multi-Agent 内部）────────────────
REACT_INNER_PROMPT = """你是一个能使用工具的研究助手。

在执行研究任务时，如果需要获取信息，使用工具：
- search(query): 搜索技术信息
- analyze(content): 深度分析内容

格式：
Thought: 分析需要什么
Action: tool_name(参数)

或直接输出最终研究结果（不需要工具时）。"""

# ── Reflection：质量审查 ──────────────────────────────────────
CRITIC_PROMPT = """你是一个严格的技术内容审查专家。

从以下维度评估报告质量：
- 准确性（内容是否正确）
- 完整性（是否覆盖关键点）
- 深度（分析是否深入）
- 可读性（表达是否清晰）

输出 JSON：
{
  "score": 0-10,
  "issues": ["问题1", "问题2"],
  "suggestions": ["建议1", "建议2"],
  "approved": true/false
}
approved=true 表示质量达标（score>=7）。只输出 JSON。"""

REVISER_PROMPT = """你是一个技术文档修订专家。

根据审查意见改进报告。直接输出改进后的完整报告。"""

# ============================================================
# 模拟工具
# ============================================================
MOCK_KNOWLEDGE = {
    "RAG": "RAG（检索增强生成）是结合检索系统和生成模型的架构，通过向量数据库检索相关文档注入 prompt，解决知识截止和幻觉问题。2023-2024年成为 LLM 应用主流架构。",
    "Agent": "AI Agent 是能自主规划和执行任务的 LLM 应用，核心组件包括：感知（输入处理）、规划（ReAct/ToT等）、记忆（上下文+外部存储）、工具调用（API/代码执行）。",
    "向量数据库": "向量数据库（如 Pinecone、Weaviate、Chroma）专门存储高维向量，支持相似度搜索（ANN），是 RAG 系统的核心存储层。",
    "Prompt Engineering": "Prompt Engineering 是设计和优化 LLM 输入的技术，核心技巧：Few-shot、Chain-of-Thought、ReAct、System/User 角色分离等。",
    "Fine-tuning": "Fine-tuning 通过在特定数据集上继续训练，把特定领域知识或行为模式烧进模型权重，适合知识相对固定、需要高频使用的场景。",
}

def mock_search(query: str) -> str:
    for key, val in MOCK_KNOWLEDGE.items():
        if key in query:
            return val
    return f"关于「{query}」：这是一个活跃发展的技术领域，近年来随着大模型的发展得到广泛应用。"

def mock_analyze(content: str) -> str:
    return f"深度分析「{content[:30]}...」：识别出3个关键趋势，2个潜在风险点，建议关注技术演进方向和实际落地挑战。"

TOOLS = {"search": mock_search, "analyze": mock_analyze}


# ============================================================
# 各模块实现
# ============================================================

def llm(system: str, user: str, max_tokens: int = 800) -> str:
    """统一的 LLM 调用封装"""
    resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}]
    )
    return resp.content[0].text


def parse_json(text: str) -> dict:
    """安全解析 JSON"""
    text = text.strip().replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except:
        return {}


# ── Module 1: Self-Ask 问题拆解 ───────────────────────────────
def module_decompose(state: ResearchState) -> ResearchState:
    print(f"\n{'─'*60}")
    print(f"📊 [Module 1] Self-Ask：问题拆解")
    print(f"{'─'*60}")

    result = parse_json(llm(DECOMPOSER_PROMPT, f"研究问题：{state.original_question}"))
    state.sub_questions = result.get("sub_questions", [state.original_question])
    core_challenge = result.get("core_challenge", "")

    print(f"核心挑战：{core_challenge}")
    print(f"子问题（{len(state.sub_questions)}个）：")
    for i, sq in enumerate(state.sub_questions):
        print(f"  {i+1}. {sq}")

    return state


# ── Module 2: ToT 研究方向选择 ────────────────────────────────
def module_select_direction(state: ResearchState) -> ResearchState:
    print(f"\n{'─'*60}")
    print(f"🌳 [Module 2] Tree of Thoughts：选择最优研究方向")
    print(f"{'─'*60}")

    # 生成 3 个候选方向
    gen_result = parse_json(llm(
        DIRECTION_GENERATOR_PROMPT.format(n=3),
        f"研究问题：{state.original_question}\n子问题：{state.sub_questions}"
    ))
    directions = gen_result.get("directions", [state.original_question])

    print(f"候选研究方向：")
    scores = []
    for i, direction in enumerate(directions):
        eval_result = parse_json(llm(
            DIRECTION_EVALUATOR_PROMPT,
            f"问题：{state.original_question}\n研究方向：{direction}"
        ))
        score = eval_result.get("score", 5.0)
        scores.append((direction, score, eval_result))
        print(f"  方向{i+1}（{score:.1f}分）：{direction[:60]}...")
        print(f"    优势：{eval_result.get('strengths', '')}")

    # 选最高分
    best = max(scores, key=lambda x: x[1])
    state.research_direction = best[0]
    print(f"\n✅ 选定方向（{best[1]:.1f}分）：{state.research_direction}")

    return state


# ── Module 3: Plan & Execute 生成计划 ────────────────────────
def module_plan(state: ResearchState) -> ResearchState:
    print(f"\n{'─'*60}")
    print(f"📋 [Module 3] Plan & Execute：制定研究计划")
    print(f"{'─'*60}")

    plan_result = parse_json(llm(
        PLANNER_PROMPT,
        f"研究问题：{state.original_question}\n研究方向：{state.research_direction}\n子问题：{state.sub_questions}",
        max_tokens=1000
    ))
    state.execution_plan = plan_result.get("steps", [])

    print(f"执行计划（{len(state.execution_plan)}步）：")
    for step in state.execution_plan:
        agent = step.get('agent', 'researcher')
        print(f"  Step {step['id']} [{agent}]：{step.get('title', '')}")

    return state


# ── Module 4: Multi-Agent + ReAct 执行 ───────────────────────
AGENT_SYSTEMS = {
    "researcher": RESEARCHER_PROMPT,
    "analyst":    ANALYST_PROMPT,
    "writer":     WRITER_PROMPT,
}

def react_tool_call(task: str) -> str:
    """ReAct 内循环：处理工具调用"""
    messages = [{"role": "user", "content": task}]

    for _ in range(3):
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            system=REACT_INNER_PROMPT,
            messages=messages,
            stop_sequences=["Action:"],
        )
        output = resp.content[0].text

        # 没有工具调用，直接返回
        if "Action:" not in output and resp.stop_reason == "end_turn":
            return output

        # 有工具调用
        action_match = re.search(r"Action:\s*(\w+)\((.+?)\)", output + "Action:")
        if not action_match:
            return output

        tool_name = action_match.group(1)
        tool_args = action_match.group(2).strip().strip('"\'')

        if tool_name in TOOLS:
            observation = TOOLS[tool_name](tool_args)
        else:
            observation = f"工具 {tool_name} 不存在"

        messages.append({"role": "assistant", "content": output + f"Action: {tool_name}({tool_args})"})
        messages.append({"role": "user", "content": f"Observation: {observation}"})

    return messages[-1]["content"] if messages else task


def module_execute(state: ResearchState) -> ResearchState:
    print(f"\n{'─'*60}")
    print(f"⚙️  [Module 4] Multi-Agent + ReAct：执行研究计划")
    print(f"{'─'*60}")

    context = f"研究问题：{state.original_question}\n研究方向：{state.research_direction}\n"

    for step in state.execution_plan:
        step_id   = step.get("id", "?")
        title     = step.get("title", "")
        agent_key = step.get("agent", "researcher")
        task_desc = step.get("task", title)

        print(f"\n  [Step {step_id}] {title} → Agent: {agent_key}")

        # 已完成步骤的结果作为上下文
        if state.step_results:
            prev = "\n".join([f"Step {r['id']}（{r['title']}）：{r['result'][:200]}"
                              for r in state.step_results])
            context_with_prev = context + f"\n前置结果：\n{prev}\n\n当前任务：{task_desc}"
        else:
            context_with_prev = context + f"\n当前任务：{task_desc}"

        # 先用 ReAct 工具增强，再交给专家 Agent 整理
        raw_info = react_tool_call(context_with_prev)

        system = AGENT_SYSTEMS.get(agent_key, RESEARCHER_PROMPT)
        result = llm(system, f"{context_with_prev}\n\n工具收集到的信息：{raw_info}\n\n请完成当前任务。", max_tokens=1000)

        state.step_results.append({"id": step_id, "title": title, "agent": agent_key, "result": result})
        print(f"  完成（{len(result)}字）：{result[:80]}...")

    return state


# ── Module 5: Reflection 质量审查 ────────────────────────────
def module_reflect(state: ResearchState) -> ResearchState:
    print(f"\n{'─'*60}")
    print(f"🔍 [Module 5] Reflection：质量审查与改进")
    print(f"{'─'*60}")

    # 先拼出初版报告
    writer_result = next(
        (r["result"] for r in state.step_results if r["agent"] == "writer"),
        "\n\n".join([r["result"] for r in state.step_results])
    )
    report = writer_result

    for i in range(3):
        print(f"\n  第 {i+1} 轮审查")

        critic_result = parse_json(llm(
            CRITIC_PROMPT,
            f"原始问题：{state.original_question}\n\n报告内容：\n{report}",
            max_tokens=600
        ))

        score    = critic_result.get("score", 5)
        issues   = critic_result.get("issues", [])
        approved = critic_result.get("approved", False)

        print(f"  评分：{score}/10")
        if issues:
            print(f"  问题：{'; '.join(issues[:2])}")

        state.reflection_rounds += 1

        if approved or score >= 7:
            print(f"  ✅ 审查通过（第 {i+1} 轮）")
            break

        suggestions = critic_result.get("suggestions", [])
        print(f"  改进建议：{'; '.join(suggestions[:2])}")
        report = llm(
            REVISER_PROMPT,
            f"原始问题：{state.original_question}\n\n当前报告：\n{report}\n\n审查意见：\n{json.dumps(critic_result, ensure_ascii=False)}",
            max_tokens=1500
        )
        print(f"  已改进（{len(report)}字）")

    state.final_report = report
    return state


# ============================================================
# 主入口：综合 Agent
# ============================================================
def ultimate_agent(question: str) -> str:
    print(f"\n{'='*60}")
    print(f"🚀 Ultimate Agent 启动")
    print(f"问题：{question}")
    print(f"{'='*60}")
    print(f"""
范式组合：
  Self-Ask       → 问题拆解
  Tree of Thoughts → 研究方向选择
  Plan & Execute  → 制定执行计划
  Multi-Agent    → 专家分工执行
  ReAct          → 工具增强信息获取
  Reflection     → 质量审查迭代
""")

    # 初始化全局状态
    state = ResearchState(original_question=question)

    # ── 依次执行各模块 ──────────────────────────────────────
    state = module_decompose(state)         # Self-Ask
    state = module_select_direction(state)  # Tree of Thoughts
    state = module_plan(state)              # Plan & Execute（规划）
    state = module_execute(state)           # Multi-Agent + ReAct（执行）
    state = module_reflect(state)           # Reflection（质量审查）

    # ── 输出最终报告 ─────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"📄 最终研究报告")
    print(f"问题：{question}")
    print(f"Reflection 轮数：{state.reflection_rounds}")
    print(f"{'='*60}")
    print(state.final_report)

    return state.final_report


# ============================================================
# 测试
# ============================================================
if __name__ == "__main__":
    ultimate_agent(
        "RAG 和 Fine-tuning 各自适合什么场景？企业在构建 AI 应用时如何选型？"
    )
