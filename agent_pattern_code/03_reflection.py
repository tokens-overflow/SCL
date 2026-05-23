"""
Reflection 范式完整示例
=======================
核心思想：Generator 生成 → Critic 批评 → 循环改进
         通过自我审查提升输出质量

变体：
1. 单模型 Reflection：同一模型扮演两个角色
2. 双模型 Reflection：不同模型，Critic 可以更强
3. 多维度 Reflection：从多个角度分别审查
"""

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


client = anthropic.Anthropic()

# ============================================================
# Prompt 1：Generator —— 生成初始内容
# ============================================================
GENERATOR_SYSTEM = """你是一个专业的技术文档写作专家。

根据用户的需求，生成高质量的技术内容。

要求：
- 内容准确、专业
- 结构清晰，有逻辑层次
- 包含具体示例
- 语言简洁，避免冗余
"""

# ============================================================
# Prompt 2：Critic —— 多维度批评审查
# ============================================================
CRITIC_SYSTEM = """你是一个严格的技术内容审查专家。

你需要从以下维度审查内容，给出具体、可操作的改进建议：

【准确性】技术内容是否正确，有无错误或误导
【完整性】是否覆盖了关键点，有无重要遗漏
【清晰度】表达是否清晰，读者能否理解
【实用性】是否有足够的具体示例，是否可以直接应用
【结构性】逻辑结构是否合理，层次是否清晰

审查格式：
## 总体评分
[1-10分] 当前内容质量评分

## 各维度评价
- 准确性：[评价]
- 完整性：[评价]
- 清晰度：[评价]
- 实用性：[评价]
- 结构性：[评价]

## 具体改进建议
1. [具体问题] → [具体改进方案]
2. [具体问题] → [具体改进方案]
...

## 结论
如果内容已经足够好（评分 ≥ 8），在最后一行输出：<APPROVED/>
否则说明最关键的改进点。
"""

# ============================================================
# Prompt 3：Reviser —— 根据 feedback 改进
# ============================================================
REVISER_SYSTEM = """你是一个专业的技术文档修订专家。

你会收到：
1. 原始需求
2. 当前版本的内容
3. 审查专家给出的改进建议

你的工作是：
- 认真阅读每条改进建议
- 有选择性地采纳（不合理的建议可以忽略，但要说明原因）
- 输出改进后的完整内容

输出格式：
## 修订说明
[简要说明采纳了哪些建议，忽略了哪些及原因]

## 修订后内容
[完整的修订后内容]
"""

# ============================================================
# 基础 Reflection：单模型
# ============================================================
def reflection_agent(task: str, max_iterations: int = 3) -> str:
    banner('Reflection · Generator → Critic → Reviser', f'任务: {task}')

    section("📝 初始生成")
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        system=GENERATOR_SYSTEM,
        messages=[{"role": "user", "content": task}]
    )
    draft = response.content[0].text
    show('初始草稿', draft)

    for iteration in range(max_iterations):
        section(f'🔍 Reflection 第 {iteration+1} 轮')

        critic_response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            system=CRITIC_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"原始需求：{task}\n\n当前内容：\n{draft}"
            }]
        )
        feedback = critic_response.content[0].text
        show('审查意见', feedback)

        if "<APPROVED/>" in feedback:
            print(f"\n✅ 第 {iteration + 1} 轮通过审查！")
            break

        section("✏️  根据反馈改进")
        revise_response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            system=REVISER_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"""原始需求：{task}

当前版本：
{draft}

审查意见：
{feedback}

请输出改进后的完整内容。"""
            }]
        )
        draft = revise_response.content[0].text
        show('改进后内容', draft)

    show('最终输出', draft, C.BGREEN)
    return draft


# ============================================================
# 进阶：双模型 Reflection（Critic 用更强的模型）
# ============================================================
def dual_model_reflection(task: str, max_iterations: int = 2) -> str:
    banner('双模型 Reflection · Sonnet ⇔ Opus', f'任务: {task}')

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        system=GENERATOR_SYSTEM,
        messages=[{"role": "user", "content": task}]
    )
    draft = response.content[0].text

    for i in range(max_iterations):
        section(f'🔍 双模型 Reflection 第 {i+1} 轮')
        info('Critic模型', 'claude-opus-4-20250514（更严格）')

        critic_response = client.messages.create(
            model="claude-opus-4-20250514",
            max_tokens=800,
            system=CRITIC_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"原始需求：{task}\n\n当前内容：\n{draft}"
            }]
        )
        feedback = critic_response.content[0].text

        if "<APPROVED/>" in feedback:
            ok('Opus 审查通过！')
            break

        revise_response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            system=REVISER_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"需求：{task}\n\n当前版本：{draft}\n\n审查意见：{feedback}"
            }]
        )
        draft = revise_response.content[0].text

    return draft


# ============================================================
# 测试
# ============================================================
if __name__ == "__main__":
    reflection_agent(
        "写一个 Python 装饰器的技术说明，面向有一年经验的开发者，要包含原理、语法和实际用例"
    )
