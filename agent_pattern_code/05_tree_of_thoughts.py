"""
Tree of Thoughts 范式完整示例
==============================
核心思想：不走单条路径，展开多分支，评分选最优
         适合需要探索解空间的复杂推理问题

三个核心操作：
1. Expand：从当前节点展开多个候选下一步
2. Evaluate：对每个候选打分
3. Select：选择最优分支继续探索

搜索策略：
- BFS（广度优先）：逐层展开，适合浅层问题
- DFS（深度优先）：沿最优路径深入，适合深层推理
- Beam Search：每层保留 top-k，平衡探索与利用
"""

import json
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

# ============================================================
# 数据结构
# ============================================================
@dataclass
class ThoughtNode:
    thought: str           # 这一步的思考内容
    score: float = 0.0     # 评分（0-10）
    depth: int = 0         # 当前深度
    parent: str = ""       # 父节点 thought（用于追溯路径）
    children: list = field(default_factory=list)


# ============================================================
# Prompt 1：Expand —— 展开候选思路
# ============================================================
EXPAND_SYSTEM = """你是一个深度推理专家。

你会收到：
1. 原始问题
2. 目前的推理路径（从起点到当前节点）

你的任务：从当前推理状态出发，提出 {n} 个不同的、有价值的下一步推理方向。

要求：
- 每个方向必须有实质差异（不能是同一思路的换句话说）
- 每个方向要具体，可以继续推进
- 覆盖不同角度：不同假设、不同方法、不同切入点

输出严格的 JSON：
{{
  "candidates": [
    {{"thought": "第一个方向的具体推理内容"}},
    {{"thought": "第二个方向的具体推理内容"}},
    ...
  ]
}}

只输出 JSON，不要其他文字。
"""

# ============================================================
# Prompt 2：Evaluate —— 评估思路质量
# ============================================================
EVALUATE_SYSTEM = """你是一个推理质量评估专家。

你会收到一个推理步骤，需要从以下维度评估其质量：

【逻辑性】推理是否合乎逻辑，有无跳跃或矛盾
【相关性】是否切中问题核心，有无跑题
【深度】是否有实质性推进，还是原地打转
【可行性】这条路径是否能最终得出答案

输出严格的 JSON：
{
  "score": 0-10的浮点数,
  "reasoning": "评分理由（一句话）",
  "promising": true/false  // 是否值得继续探索
}

只输出 JSON，不要其他文字。
"""

# ============================================================
# Prompt 3：Conclude —— 基于最优路径得出答案
# ============================================================
CONCLUDE_SYSTEM = """你是一个推理总结专家。

你会收到：
1. 原始问题
2. 经过探索找到的最优推理路径

请基于这条推理路径，给出完整、清晰的最终答案。

要求：
- 答案要完整，不能只是重复推理过程
- 如果推理路径有不足，可以在答案中补充
- 格式清晰，有结论
"""


# ============================================================
# ToT 核心：BFS + Beam Search
# ============================================================
def tree_of_thoughts(
    problem: str,
    branches: int = 3,    # 每次展开几个分支
    depth: int = 2,       # 探索深度
    beam_width: int = 2   # 每层保留几个最优节点
) -> str:
    print(f"\n{'='*60}")
    print(f"问题：{problem}")
    print(f"配置：branches={branches}, depth={depth}, beam_width={beam_width}")
    print(f"{'='*60}")

    # 初始节点：空思路，从问题出发
    current_beam = [ThoughtNode(thought=f"开始分析问题：{problem}", depth=0, score=5.0)]

    all_nodes = []  # 记录所有探索过的节点

    for d in range(depth):
        print(f"\n🌳 [深度 {d+1}/{depth}]")
        next_beam = []

        for node in current_beam:
            print(f"\n  当前节点（分数 {node.score:.1f}）：{node.thought[:60]}...")

            # Step 1：展开候选分支
            candidates = expand_node(problem, node, branches)
            print(f"  展开了 {len(candidates)} 个候选分支")

            # Step 2：评估每个候选
            for candidate in candidates:
                score_info = evaluate_node(problem, node, candidate)
                child_node = ThoughtNode(
                    thought=candidate,
                    score=score_info["score"],
                    depth=d + 1,
                    parent=node.thought
                )
                node.children.append(child_node)
                next_beam.append(child_node)
                all_nodes.append(child_node)
                print(f"  候选（分数 {score_info['score']:.1f}）：{candidate[:50]}... | {score_info['reasoning']}")

        # Step 3：Beam Search —— 只保留 top-k
        next_beam.sort(key=lambda x: x.score, reverse=True)
        current_beam = next_beam[:beam_width]

        print(f"\n  ✂️  剪枝后保留 {len(current_beam)} 个节点：")
        for node in current_beam:
            print(f"  ★ 分数 {node.score:.1f}：{node.thought[:60]}...")

    # Step 4：找到最优路径
    best_node = max(current_beam, key=lambda x: x.score)
    best_path = reconstruct_path(best_node, all_nodes + [ThoughtNode(
        thought=f"开始分析问题：{problem}", depth=0, score=5.0
    )])

    print(f"\n🏆 最优路径（共 {len(best_path)} 步）：")
    for i, thought in enumerate(best_path):
        print(f"  Step {i}: {thought[:80]}...")

    # Step 5：基于最优路径得出答案
    print(f"\n💡 [得出最终答案]")
    final_answer = conclude(problem, best_path)

    print(f"\n{'='*60}")
    print(f"最终答案：\n{final_answer}")
    return final_answer


def expand_node(problem: str, node: ThoughtNode, n: int) -> list[str]:
    """展开节点，生成 n 个候选下一步"""
    system = EXPAND_SYSTEM.format(n=n)

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        system=system,
        messages=[{
            "role": "user",
            "content": f"原始问题：{problem}\n\n当前推理：{node.thought}\n\n请展开 {n} 个不同的下一步推理方向。"
        }]
    )

    text = response.content[0].text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        result = json.loads(text)
        return [c["thought"] for c in result.get("candidates", [])]
    except:
        return [f"继续推理：{problem}"]


def evaluate_node(problem: str, parent: ThoughtNode, candidate: str) -> dict:
    """评估候选节点的质量"""
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=200,
        system=EVALUATE_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"原始问题：{problem}\n\n前一步推理：{parent.thought}\n\n候选下一步：{candidate}\n\n请评估这一步的质量。"
        }]
    )

    text = response.content[0].text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except:
        return {"score": 5.0, "reasoning": "评估异常", "promising": True}


def conclude(problem: str, path: list[str]) -> str:
    """基于最优路径得出最终答案"""
    path_text = "\n".join([f"Step {i}: {thought}" for i, thought in enumerate(path)])

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        system=CONCLUDE_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"原始问题：{problem}\n\n最优推理路径：\n{path_text}\n\n请给出最终答案。"
        }]
    )
    return response.content[0].text


def reconstruct_path(node: ThoughtNode, all_nodes: list[ThoughtNode]) -> list[str]:
    """从叶节点回溯到根节点，重建完整推理路径"""
    path = [node.thought]
    current = node

    while current.parent:
        parent_node = next(
            (n for n in all_nodes if n.thought == current.parent),
            None
        )
        if not parent_node:
            break
        path.insert(0, parent_node.thought)
        current = parent_node

    return path


# ============================================================
# 测试
# ============================================================
if __name__ == "__main__":
    # 适合 ToT 的问题：需要多角度探索的复杂推理
    tree_of_thoughts(
        problem="一家初创公司应该先做产品还是先找融资？从创始人视角分析这个决策。",
        branches=3,
        depth=2,
        beam_width=2
    )
