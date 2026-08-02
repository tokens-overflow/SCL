"""上下文模型。

**Context 不等于长期记忆。** 它是「这一次模型调用桌面上摊开的材料」。
给少了模型只能猜；给多了关键项被淹没，成本还翻倍——而且成本是按每次调用
重新喂一遍算的，上下文会随步数越来越长，所以一次任务里模型调用的次数，
几乎直接决定了它的成本和延迟。

因此 `AgentContext` 被设计成**结构化的、可裁剪的**对象，而不是一坨拼接好的字符串：

* 结构化 → 可以按需只渲染其中几块（例如生成回复时不需要工具清单）；
* 可裁剪 → `recent_steps` 只保留最近 N 条，`knowledge` 有条数上限；
* 可审计 → 送进模型的 Prompt 快照可以完整落盘，事后能回答
  「我以为我给了模型 X，到底给没给」。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.ids import utcnow
from app.security.identity import ResolvedIdentity


class ToolSummary(BaseModel):
    """给 LLM 看的工具摘要。

    注意这里**只有名字、描述、参数结构和风险等级**，没有实现细节，
    也没有「需要哪些权限」——权限是控制层的事，写进 Prompt 只会有两个后果：
    要么模型自作聪明地替我们做权限判断（不可靠），
    要么把内部权限结构泄漏出去（不必要）。
    """

    name: str
    description: str
    risk_level: str
    arguments_schema: dict[str, Any] = Field(default_factory=dict)
    idempotent: bool = True


class StepSummary(BaseModel):
    """最近步骤的执行结果摘要。

    只放「模型下一步决策真正需要的东西」：步骤名、状态、简化后的输出、错误码。
    完整的 input/output 快照留在状态层，需要时可回放，但不进上下文——
    那是纯粹的 token 浪费。
    """

    step_name: str
    status: str
    summary: str = ""
    error_code: str | None = None


class RetrievedDocument(BaseModel):
    """检索回来的业务知识片段。

    Attributes:
        doc_id: 文档标识。
        title: 标题。
        content: 正文（已脱敏）。
        score: 相关度分值。
        source: 来源标注，用于让模型在回答时可以引用出处，也便于事后核查。
    """

    doc_id: str
    title: str
    content: str
    score: float = 0.0
    source: str = "internal"


class MemoryItem(BaseModel):
    """历史记忆条目。

    Attributes:
        memory_id: 记忆标识。
        scope: 作用域，如 ``"user"`` / ``"customer"``。
        content: 摘要内容（**是摘要，不是原始对话**：
            原始对话既贵又容易把旧的错误结论带到新任务里）。
        created_at: 写入时间。
    """

    memory_id: str
    scope: str
    content: str
    created_at: datetime = Field(default_factory=utcnow)


class AgentContext(BaseModel):
    """一次 Agent 运行所需的完整上下文。

    这个对象贯穿认知层、控制层、行动层：

    * 认知层用它组装 Prompt；
    * 控制层用它取身份、风险提示；
    * 行动层用它取 trace_id、task_id 写审计。

    它是**不可变语义**的（每一步会重建一个新的），
    所以不会出现「某个模块偷偷改了上下文导致后面行为漂移」。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ---- 标识 ----
    task_id: str
    trace_id: str
    user_id: str
    agent_id: str

    # ---- 身份 ----
    identity: ResolvedIdentity

    # ---- 当前输入 ----
    #: 已经过净化与脱敏的用户输入。**原始输入保存在状态层，不进模型上下文。**
    user_input: str = ""
    task_type: str = "generic"
    task_status: str = "CREATED"
    current_step: str | None = None

    # ---- 材料 ----
    recent_steps: list[StepSummary] = Field(default_factory=list)
    available_tools: list[ToolSummary] = Field(default_factory=list)
    knowledge: list[RetrievedDocument] = Field(default_factory=list)
    memories: list[MemoryItem] = Field(default_factory=list)

    # ---- 风险与合规提示 ----
    #: 给模型的软性提示（例如「该客户为 VIP」）。
    #: **软性提示不能代替控制层校验**——模型可能忽略它，控制层不会。
    risk_hints: list[str] = Field(default_factory=list)
    compliance_notes: list[str] = Field(default_factory=list)

    # ---- 脱敏 ----
    #: 本次上下文中出现的代号（如 ``PERSON_8F29A1``）。
    #: 映射表**不在这里**，只在企业内部安全边界内，模型永远拿不到。
    tokens_in_context: list[str] = Field(default_factory=list)

    # ---- 元信息 ----
    created_at: datetime = Field(default_factory=utcnow)
    extra: dict[str, Any] = Field(default_factory=dict)

    def render_system_prompt(self) -> str:
        """把上下文渲染成系统提示词。

        Returns:
            结构化的中文系统提示。

        Note:
            提示词里会写「哪些动作需要审批」，但这**只是给模型的提示**，
            让它别提出注定被拒的方案、少走一轮无效往返。
            真正的审批判定在 `app.control` 里，**Prompt 改坏了也不会导致越权**。
        """
        lines: list[str] = [
            "你是一个企业级业务 Agent 的认知模块。",
            "",
            "# 你的职责边界（非常重要）",
            "- 你只负责【理解意图】和【提出结构化动作建议】。",
            "- 你【不能】决定某个动作是否被允许执行，也【不能】直接执行任何动作。",
            "- 权限、金额上限、是否需要审批，全部由后端控制层判定，你的判断仅供参考。",
            "- 只能从下方【可用工具】列表中选择工具，不得臆造工具名或参数。",
            "",
            "# 身份摘要",
            f"- 操作人角色：{'/'.join(sorted(self.identity.user.roles)) or '未知'}",
            f"- 所属部门：{self.identity.user.department}",
            f"- Agent：{self.agent_id}（{self.identity.agent.description}）",
            "",
            "# 当前任务",
            f"- 任务 ID：{self.task_id}",
            f"- 任务类型：{self.task_type}",
            f"- 任务状态：{self.task_status}",
        ]

        if self.available_tools:
            lines += ["", "# 可用工具"]
            for tool in self.available_tools:
                lines.append(
                    f"- {tool.name}（风险等级 {tool.risk_level}）：{tool.description}"
                )
                if tool.arguments_schema:
                    fields = ", ".join(sorted(tool.arguments_schema.get("properties", {})))
                    lines.append(f"  参数：{fields}")

        if self.recent_steps:
            lines += ["", "# 最近步骤结果"]
            for step in self.recent_steps:
                err = f"（错误码 {step.error_code}）" if step.error_code else ""
                lines.append(f"- {step.step_name}: {step.status}{err} {step.summary}")

        if self.knowledge:
            lines += ["", "# 相关业务知识"]
            for doc in self.knowledge:
                lines.append(f"- [{doc.title}] {doc.content}")

        if self.memories:
            lines += ["", "# 历史记忆摘要"]
            for mem in self.memories:
                lines.append(f"- ({mem.scope}) {mem.content}")

        if self.risk_hints:
            lines += ["", "# 风险提示（仅供参考，最终判定由控制层执行）"]
            lines += [f"- {hint}" for hint in self.risk_hints]

        if self.compliance_notes:
            lines += ["", "# 合规提示"]
            lines += [f"- {note}" for note in self.compliance_notes]

        if self.tokens_in_context:
            lines += [
                "",
                "# 关于代号",
                "- 上下文中形如 PERSON_XXXX / ACCOUNT_XXXX 的是脱敏代号。",
                "- 请在回复中【原样使用代号】，不要尝试猜测或还原其真实值。",
            ]

        return "\n".join(lines)

    def prompt_snapshot(self) -> dict[str, Any]:
        """生成 Prompt 快照，用于审计与事后回放。

        线上 Bug 有一半出在「我以为我给了模型 X，其实没给」。
        所以每次 LLM 调用都要把**实际组装出来的上下文**落盘。

        Returns:
            结构化快照。注意这里存的是**统计信息 + 已脱敏内容**，
            不存任何未脱敏的个人信息。
        """
        return {
            "task_id": self.task_id,
            "trace_id": self.trace_id,
            "task_type": self.task_type,
            "user_input_length": len(self.user_input),
            "tool_names": [t.name for t in self.available_tools],
            "recent_step_count": len(self.recent_steps),
            "knowledge_doc_ids": [d.doc_id for d in self.knowledge],
            "memory_ids": [m.memory_id for m in self.memories],
            "risk_hints": self.risk_hints,
            "tokens_in_context": self.tokens_in_context,
        }
