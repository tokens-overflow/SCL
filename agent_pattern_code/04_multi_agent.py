"""
Multi-Agent 范式完整示例
========================
核心思想：多个专职 Agent 协同完成复杂任务

两种组织方式：
1. Pipeline（流水线）：A → B → C，输出链式传递
2. Orchestrator + Workers：总调度 + 专项执行

关键：每个 Agent 都有独立的 system prompt，职责单一
"""

import json
import anthropic

client = anthropic.Anthropic()

# ============================================================
# 方式一：Pipeline 流水线
# 场景：自动写作流水线（研究 → 写作 → 校对 → 优化）
# ============================================================

RESEARCH_AGENT_SYSTEM = """你是一个信息研究专家。

你的唯一职责：收到一个主题，输出该主题的核心要点、关键数据和重要信息。

输出格式：
## 核心要点
- [要点1]
- [要点2]
...

## 关键数据/事实
- [数据1]
- [数据2]
...

## 延伸角度
- [可以深入探讨的方向]
...

只做研究，不做写作。输出信息密度要高，简洁准确。
"""

WRITER_AGENT_SYSTEM = """你是一个技术写作专家。

你的唯一职责：根据提供的研究素材，写出结构清晰、逻辑流畅的文章。

要求：
- 有清晰的开头、正文、结尾结构
- 语言专业但不枯燥
- 自然融入研究素材中的数据和要点
- 不要凭空添加研究素材中没有的内容

只做写作，不做校对和优化。
"""

EDITOR_AGENT_SYSTEM = """你是一个专业编辑。

你的唯一职责：校对并改进文章，提升可读性和专业性。

检查项：
1. 语句是否通顺，有无语病
2. 段落之间是否有自然过渡
3. 专业术语使用是否准确
4. 是否有冗余表达

直接输出修改后的完整文章，不需要列出修改说明。
"""

SEO_AGENT_SYSTEM = """你是一个内容优化专家。

你的唯一职责：为文章添加标题优化、摘要和关键词。

输出格式：
## 优化标题
[吸引眼球且准确的标题]

## 一句话摘要
[50字以内的核心摘要]

## 关键词
[5-8个关键词，逗号分隔]

## 正文
[原文内容保持不变，直接输出]
"""


def pipeline_agent(topic: str) -> str:
    """
    流水线：研究 → 写作 → 编辑 → SEO优化
    每个 Agent 的输出是下一个的输入
    """
    print(f"\n{'='*60}")
    print(f"流水线任务：{topic}")
    print(f"{'='*60}")

    # Stage 1：研究
    print("\n📚 [Stage 1: Research Agent]")
    research = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        system=RESEARCH_AGENT_SYSTEM,
        messages=[{"role": "user", "content": f"研究主题：{topic}"}]
    ).content[0].text
    print(f"研究结果（前150字）：{research[:150]}...")

    # Stage 2：写作（把研究结果传给 Writer）
    print("\n✍️  [Stage 2: Writer Agent]")
    article = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1200,
        system=WRITER_AGENT_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"主题：{topic}\n\n研究素材：\n{research}\n\n请根据以上素材写一篇文章。"
        }]
    ).content[0].text
    print(f"初稿（前150字）：{article[:150]}...")

    # Stage 3：编辑（把文章传给 Editor）
    print("\n📝 [Stage 3: Editor Agent]")
    edited = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1200,
        system=EDITOR_AGENT_SYSTEM,
        messages=[{"role": "user", "content": f"请校对并改进以下文章：\n\n{article}"}]
    ).content[0].text
    print(f"编辑后（前150字）：{edited[:150]}...")

    # Stage 4：SEO优化（把编辑后的文章传给 SEO Agent）
    print("\n🚀 [Stage 4: SEO Agent]")
    final = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        system=SEO_AGENT_SYSTEM,
        messages=[{"role": "user", "content": f"请为以下文章进行SEO优化：\n\n{edited}"}]
    ).content[0].text

    print(f"\n{'='*60}")
    print("最终输出：")
    print(final)
    return final


# ============================================================
# 方式二：Orchestrator + Workers
# 场景：代码项目助手（分析 → 编码 → 测试 → 文档）
# ============================================================

ORCHESTRATOR_SYSTEM = """你是一个项目协调专家（Orchestrator）。

你负责把复杂的开发任务分配给合适的专家 Worker 来完成。

可用的 Worker：
- code_analyzer：分析代码需求和架构，输出技术方案
- coder：根据方案编写具体代码
- tester：为代码编写测试用例
- documenter：为代码编写文档和注释

你的工作流程：
1. 分析任务，决定调用哪个 Worker
2. 给 Worker 明确的指令
3. 收到结果后，决定下一步
4. 所有步骤完成后输出 DONE

每次只调用一个 Worker，输出严格的 JSON：
{
  "thinking": "当前分析和决策过程",
  "action": "call_worker" 或 "done",
  "worker": "worker名称（action为call_worker时必填）",
  "instruction": "给worker的具体指令（action为call_worker时必填）",
  "final_output": "最终总结（action为done时必填）"
}

只输出 JSON，不要其他文字。
"""

CODE_ANALYZER_SYSTEM = """你是一个代码架构分析专家。

根据需求描述，输出：
1. 技术方案选型
2. 核心数据结构设计
3. 主要函数/类的接口设计
4. 实现注意事项

简洁专业，不写具体代码实现。
"""

CODER_SYSTEM = """你是一个资深软件工程师。

根据技术方案，编写完整、可运行的代码。

要求：
- 代码完整，可以直接运行
- 有适当的注释
- 遵循最佳实践
- 处理边界情况和错误
"""

TESTER_SYSTEM = """你是一个测试工程师。

为给定的代码编写全面的测试用例：
- 单元测试（正常情况）
- 边界条件测试
- 异常情况测试

使用 pytest 风格，代码可以直接运行。
"""

DOCUMENTER_SYSTEM = """你是一个技术文档工程师。

为代码编写清晰的文档：
- 函数/类的 docstring
- 使用示例
- 参数说明
- 返回值说明

直接输出带文档的完整代码。
"""

WORKERS = {
    "code_analyzer": CODE_ANALYZER_SYSTEM,
    "coder": CODER_SYSTEM,
    "tester": TESTER_SYSTEM,
    "documenter": DOCUMENTER_SYSTEM,
}


def orchestrator_agent(task: str, max_steps: int = 6) -> str:
    """
    Orchestrator + Workers 模式
    Orchestrator 动态决定调用哪个 Worker 和顺序
    """
    print(f"\n{'='*60}")
    print(f"Orchestrator 任务：{task}")
    print(f"{'='*60}")

    # 存储所有 Worker 的执行结果
    history = []
    orchestrator_messages = [{"role": "user", "content": f"开发任务：{task}"}]

    for step in range(max_steps):
        print(f"\n[Step {step + 1}] Orchestrator 决策中...")

        # Orchestrator 决定下一步
        orch_response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            system=ORCHESTRATOR_SYSTEM,
            messages=orchestrator_messages
        )

        orch_text = orch_response.content[0].text.strip()
        orch_text = orch_text.replace("```json", "").replace("```", "").strip()

        try:
            decision = json.loads(orch_text)
        except json.JSONDecodeError:
            print(f"JSON 解析失败：{orch_text[:100]}")
            break

        print(f"思考：{decision.get('thinking', '')[:100]}...")

        # 检查是否完成
        if decision.get("action") == "done":
            print(f"\n✅ Orchestrator 宣告完成！")
            final = decision.get("final_output", "任务完成")
            print(f"\n{'='*60}")
            print(f"最终总结：{final}")
            return final

        # 调用指定的 Worker
        worker_name = decision.get("worker")
        instruction = decision.get("instruction", "")

        if worker_name not in WORKERS:
            print(f"未知 Worker：{worker_name}")
            break

        print(f"→ 调用 Worker：{worker_name}")
        print(f"  指令：{instruction[:80]}...")

        worker_response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1200,
            system=WORKERS[worker_name],
            messages=[{"role": "user", "content": instruction}]
        )
        worker_result = worker_response.content[0].text
        print(f"  结果（前100字）：{worker_result[:100]}...")

        # 记录结果
        history.append({
            "step": step + 1,
            "worker": worker_name,
            "instruction": instruction,
            "result": worker_result
        })

        # 把 Worker 结果反馈给 Orchestrator（追加到消息历史）
        orchestrator_messages.append({
            "role": "assistant",
            "content": orch_text
        })
        orchestrator_messages.append({
            "role": "user",
            "content": f"Worker [{worker_name}] 执行完成，结果如下：\n\n{worker_result}\n\n请决定下一步。"
        })

    return "达到最大步数"


# ============================================================
# 测试
# ============================================================
if __name__ == "__main__":
    print("\n\n========== 测试1：Pipeline 流水线 ==========")
    pipeline_agent("大型语言模型的 Prompt Engineering 技术")

    print("\n\n========== 测试2：Orchestrator + Workers ==========")
    orchestrator_agent("实现一个 LRU Cache（最近最少使用缓存），支持 get 和 put 操作")
