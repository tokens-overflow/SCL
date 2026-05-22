"""
ReAct 范式完整示例
================
核心思想：Thought → Action → Observation 循环
         边推理边行动，直到得出最终答案

提示词约定了输出格式，宿主程序解析格式驱动循环。
"""

import re
import anthropic

client = anthropic.Anthropic()

# ============================================================
# Prompt：约定 Thought/Action/Observation/Answer 格式
# ============================================================
REACT_SYSTEM = """你是一个能使用工具解决问题的智能助手。

你必须严格按照以下格式逐步思考和行动：

Thought: 分析当前情况，决定下一步做什么
Action: tool_name(参数)
Observation: [系统填入工具返回结果]
Thought: 根据观察继续分析
Action: tool_name(参数)
Observation: [系统填入]
...（可以重复多轮）
Answer: 最终答案

可用工具：
- search(query: str) → 搜索信息，返回摘要
- calculator(expr: str) → 计算数学表达式，返回结果
- get_weather(city: str) → 获取城市天气

规则：
1. 每次只输出到 Action 这一行，等待 Observation 填入后再继续
2. 确认已有足够信息后再输出 Answer
3. 不要捏造 Observation，必须等工具返回
"""

# ============================================================
# 模拟工具实现（真实项目替换为真实 API）
# ============================================================
def search(query: str) -> str:
    mock_data = {
        "北京人口": "北京市常住人口约2185万人（2023年数据）",
        "上海人口": "上海市常住人口约2487万人（2023年数据）",
        "GDP": "2023年中国GDP约126万亿人民币",
    }
    for key, val in mock_data.items():
        if key in query:
            return val
    return f"搜索「{query}」：暂无相关结果"

def calculator(expr: str) -> str:
    try:
        result = eval(expr, {"__builtins__": {}}, {})
        return str(result)
    except Exception as e:
        return f"计算错误：{e}"

def get_weather(city: str) -> str:
    mock = {
        "北京": "北京今日晴，气温 18-28°C，微风",
        "上海": "上海今日多云，气温 20-26°C，东南风",
        "广州": "广州今日小雨，气温 24-30°C，湿度85%",
    }
    return mock.get(city, f"{city}天气暂时无法获取")

TOOLS = {
    "search": search,
    "calculator": calculator,
    "get_weather": get_weather,
}

# ============================================================
# 工具执行：解析 Action 行并调用对应工具
# ============================================================
def execute_action(action_line: str) -> str:
    """解析 'tool_name(args)' 格式并执行"""
    match = re.match(r"(\w+)\((.+)\)", action_line.strip())
    if not match:
        return "无法解析 Action 格式"

    tool_name = match.group(1)
    args = match.group(2).strip().strip('"').strip("'")

    if tool_name not in TOOLS:
        return f"未知工具：{tool_name}"

    return TOOLS[tool_name](args)

# ============================================================
# ReAct 主循环
# ============================================================
def react_agent(question: str, max_steps: int = 8) -> str:
    """
    ReAct Agent 主函数
    - 每轮只让 LLM 输出到 Action
    - 执行工具后把 Observation 追加回 prompt
    - 循环直到出现 Answer
    """
    messages = [{"role": "user", "content": question}]
    print(f"\n{'='*60}")
    print(f"问题：{question}")
    print(f"{'='*60}")

    for step in range(max_steps):
        # 调用 LLM，停在 Observation 之前
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            system=REACT_SYSTEM,
            messages=messages,
            stop_sequences=["Observation:"],  # 关键：遇到 Observation 就停
        )

        output = response.content[0].text
        print(f"\n[Step {step+1}]\n{output}")

        # 检查是否已有答案
        if "Answer:" in output:
            answer = output.split("Answer:")[-1].strip()
            print(f"\n{'='*60}")
            print(f"最终答案：{answer}")
            return answer

        # 解析 Action 并执行工具
        action_match = re.search(r"Action:\s*(.+)", output)
        if not action_match:
            break

        action_line = action_match.group(1).strip()
        observation = execute_action(action_line)
        print(f"Observation: {observation}")

        # 把这一轮的输出 + Observation 追加回 messages
        # 这就是 ReAct 的"记忆"——所有历史都在 messages 里
        messages.append({"role": "assistant", "content": output + "Observation:"})
        messages.append({"role": "user", "content": f" {observation}\n"})

    return "超过最大步数，未得到答案"


# ============================================================
# 测试
# ============================================================
if __name__ == "__main__":
    # 测试1：需要多步工具调用
    react_agent("北京和上海的人口加起来是多少？")

    # 测试2：天气 + 计算
    react_agent("北京今天适合出门吗？气温超过25度需要防晒")
