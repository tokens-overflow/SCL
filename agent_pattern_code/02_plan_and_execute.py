"""
Plan & Execute 范式完整示例
===========================
核心思想：先整体规划，再逐步执行
         规划和执行是两个独立的 LLM 调用
         适合步骤清晰的长任务

与 ReAct 区别：
- ReAct：边想边做，灵活但容易跑偏
- Plan & Execute：先想清楚再做，稳定但不够灵活
"""

import json
import anthropic

# ─── 终端彩色输出工具（VSCode Terminal 适用）────────────────────────
class C:
    RESET="[0m"; BOLD="[1m"; DIM="[2m"
    RED="[31m"; GREEN="[32m"; YELLOW="[33m"
    BLUE="[34m"; MAGENTA="[35m"; CYAN="[36m"
    BRED="[91m"; BGREEN="[92m"; BYELLOW="[93m"
    BBLUE="[94m"; BMAGENTA="[95m"; BCYAN="[96m"

def banner(title: str, sub: str = "", width: int = 64) -> None:
    line = "═" * width
    print(f"\n{C.BCYAN}{C.BOLD}{line}")
    print(f"  {title}")
    if sub:
        print(f"  {C.DIM}{sub}{C.BCYAN}{C.BOLD}")
    print(f"{line}{C.RESET}")

def section(title: str) -> None:
    line = "─" * 60
    print(f"\n{C.CYAN}{C.BOLD}{line}\n  {title}\n{line}{C.RESET}")

def info(label: str, value: str = "") -> None:
    if value:
        print(f"  {C.BBLUE}▸{C.RESET} {C.BOLD}{label}:{C.RESET} {value}")
    else:
        print(f"  {C.BBLUE}▸{C.RESET} {label}")

def ok(msg: str) -> None:
    print(f"  {C.BGREEN}✓{C.RESET} {msg}")

def warn(msg: str) -> None:
    print(f"  {C.BYELLOW}⚠{C.RESET} {msg}")

def err(msg: str) -> None:
    print(f"  {C.BRED}✗{C.RESET} {msg}")

def show(label: str, text: str, color: str = "") -> None:
    color = color or C.YELLOW
    print(f"\n  {color}{C.BOLD}▣ {label}{C.RESET}")
    for ln in str(text).split("\n"):
        print(f"    {ln}")
# ─────────────────────────────────────────────────────────────

def safe_json(text: str) -> dict:
    """容错 JSON 解析：剥除 markdown 代码块，提取内嵌 JSON 对象"""
    import re as _re
    cleaned = text.strip()
    cleaned = _re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
    cleaned = _re.sub(r"\n?```\s*$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        m = _re.search(r"(\{[\s\S]*?\}|\[[\s\S]*?\])", cleaned)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        warn(f"JSON 解析失败 (前80字): {cleaned[:80]}")
        return {}



client = anthropic.Anthropic()

# ============================================================
# Prompt 1：Planner —— 负责把任务拆成步骤
# ============================================================
PLANNER_SYSTEM = """你是一个任务规划专家。

你的工作是把用户的复杂任务拆解成具体、可执行的步骤列表。

要求：
1. 每个步骤必须是独立可执行的最小单元
2. 步骤之间有明确的依赖关系（后面的步骤可以用前面的结果）
3. 步骤数量控制在 3-7 个
4. 每个步骤说明：做什么、用什么工具、期望得到什么

输出严格的 JSON 格式：
{
  "goal": "任务总目标",
  "steps": [
    {
      "id": 1,
      "description": "步骤描述",
      "tool": "工具名称（search/calculator/write/analyze）",
      "input": "工具输入内容",
      "expected_output": "期望得到什么"
    }
  ]
}

只输出 JSON，不要任何其他文字。
"""

# ============================================================
# Prompt 2：Executor —— 负责执行单个步骤
# ============================================================
EXECUTOR_SYSTEM = """你是一个任务执行专家。

你会收到：
1. 整体任务目标
2. 当前需要执行的步骤
3. 前面步骤的执行结果（上下文）

你的工作是专注执行当前步骤，输出高质量的结果。

要求：
1. 只做当前步骤要求的事，不要超出范围
2. 结果要具体、可用，不要泛泛而谈
3. 如果需要用到前面步骤的结果，明确引用
4. 输出格式：直接输出步骤结果，不需要解释过程
"""

# ============================================================
# Prompt 3：Replanner —— 遇到意外时重新规划
# ============================================================
REPLANNER_SYSTEM = """你是一个任务规划专家。

原计划执行过程中遇到了问题，需要你根据当前情况调整剩余计划。

输出严格的 JSON 格式（只包含剩余步骤）：
{
  "steps": [
    {
      "id": "步骤编号（从当前编号继续）",
      "description": "调整后的步骤描述",
      "tool": "工具名称",
      "input": "工具输入",
      "expected_output": "期望输出"
    }
  ]
}

只输出 JSON，不要任何其他文字。
"""

# ============================================================
# 模拟工具
# ============================================================
def mock_search(query: str) -> str:
    data = {
        "Python 最新版本": "Python 3.12.3（2024年4月发布），主要改进：更好的错误提示、更快的速度",
        "Python 特性": "Python 特性：简洁语法、动态类型、强大标准库、广泛的第三方包生态",
        "Python 应用场景": "Python 广泛用于：数据科学、机器学习、Web开发、自动化脚本、科学计算",
    }
    for key, val in data.items():
        if key in query:
            return val
    return f"搜索 '{query}' 的结果：相关技术资料已找到"

def mock_analyze(content: str) -> str:
    return f"分析结果：对「{content[:30]}...」的分析——内容结构清晰，涵盖核心要点，建议补充实际案例"

def mock_write(instruction: str) -> str:
    return f"根据指令「{instruction[:40]}...」生成的内容：[这里是生成的高质量文本内容，包含引言、正文、总结三部分]"

def mock_calculator(expr: str) -> str:
    try:
        return str(eval(expr, {"__builtins__": {}}, {}))
    except:
        return "计算完成"

TOOLS = {
    "search": mock_search,
    "analyze": mock_analyze,
    "write": mock_write,
    "calculator": mock_calculator,
}

def execute_tool(tool: str, input_text: str) -> str:
    if tool in TOOLS:
        return TOOLS[tool](input_text)
    return f"执行步骤：{input_text}"

# ============================================================
# Plan & Execute 主流程
# ============================================================
def plan_and_execute(task: str) -> str:
    banner('Plan & Execute · 先规划后执行', f'任务: {task}')

    section("📋 规划阶段")
    plan_response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=PLANNER_SYSTEM,
        messages=[{"role": "user", "content": task}]
    )

    plan_text = plan_response.content[0].text.strip()
    plan = safe_json(plan_text)

    info('目标', plan['goal'])
    info('步骤数', str(len(plan['steps'])))
    for step in plan["steps"]:
        info(f"  Step {step['id']}", step['description'])

    section("⚙️  执行阶段")
    results = []

    steps = plan["steps"]
    i = 0
    while i < len(steps):
        step = steps[i]
        print(f"\n[Step {step['id']}] {step['description']}")

        context = ""
        if results:
            context = "前面步骤的执行结果：\n"
            for r in results:
                context += f"- Step {r['id']}（{r['description']}）：{r['result']}\n"

        executor_prompt = f"""整体目标：{plan['goal']}

{context}
当前步骤：{step['description']}
工具：{step['tool']}
输入：{step['input']}
期望输出：{step['expected_output']}

请执行当前步骤。"""

        tool_result = execute_tool(step["tool"], step["input"])

        exec_response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            system=EXECUTOR_SYSTEM,
            messages=[{
                "role": "user",
                "content": executor_prompt + f"\n\n工具返回：{tool_result}"
            }]
        )

        step_result = exec_response.content[0].text
        show('步骤结果', step_result)

        results.append({
            "id": step["id"],
            "description": step["description"],
            "result": step_result
        })

        if "失败" in step_result or "错误" in step_result:
            print(f"\n⚠️  Step {step['id']} 遇到问题，重新规划剩余步骤...")
            remaining_steps = plan_and_replan(task, results, steps[i+1:])
            steps = steps[:i+1] + remaining_steps
            print(f"重新规划后剩余 {len(remaining_steps)} 个步骤")

        i += 1

    print("\n📝 [汇总阶段]")
    summary_response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        system="你是一个善于总结的助手，根据各步骤执行结果给出完整的最终答案。",
        messages=[{
            "role": "user",
            "content": f"任务：{task}\n\n各步骤结果：\n" +
                       "\n".join([f"Step {r['id']}（{r['description']}）：{r['result']}"
                                  for r in results]) +
                       "\n\n请综合以上结果给出完整答案。"
        }]
    )

    final_answer = summary_response.content[0].text
    print(f"\n{'='*60}")
    print(f"最终答案：\n{final_answer}")
    return final_answer


def plan_and_replan(task: str, completed: list, remaining: list) -> list:
    """遇到问题时重新规划剩余步骤"""
    replan_response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        system=REPLANNER_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"""原任务：{task}

已完成步骤：
{json.dumps(completed, ensure_ascii=False, indent=2)}

原剩余步骤（需要调整）：
{json.dumps(remaining, ensure_ascii=False, indent=2)}

请根据当前情况调整剩余计划。"""
        }]
    )
    text = replan_response.content[0].text.strip()
    return safe_json(text).get("steps", remaining)


# ============================================================
# 测试
# ============================================================
if __name__ == "__main__":
    plan_and_execute("写一篇关于 Python 编程语言的技术介绍文章，要求包含最新版本特性和应用场景")
