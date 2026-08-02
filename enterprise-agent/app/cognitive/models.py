"""认知层的结构化输出模型。

这些模型定义了「LLM 被允许说出口的话的形状」。

**为什么 LLM 只能产出 ActionProposal，而不是直接执行？**

因为 proposal 这个词就是全部答案：它是**建议**，不是决定。
模型擅长的是把「给客户 C001 打九折」这句模糊的自然语言，
变成 `{tool: apply_discount, customer_id: C001, discount_rate: 0.1}` 这样明确的结构。
它不擅长、也不应该负责的是：这个客服有没有资格给 10% 的折扣、
这个客户是不是已经有一个生效折扣、要不要经理审批。
那些问题**有唯一正确答案**，唯一正确答案的问题交给程序，
既更可靠也更便宜，出错时还有人能负责。

所以这里的每个模型都带着同一个约束：它只描述「模型想做什么」，
不描述「系统会不会让它做」。后者是 :mod:`app.control` 的输出。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import RiskLevel


class IntentParseResult(BaseModel):
    """意图解析结果。

    Attributes:
        intent: 归一化的意图标识，例如 ``apply_discount``。
        task_type: 任务类型，决定用哪套编排流程。
        entities: 从自然语言里抽出来的实体（客户号、折扣率等）。
            **这里的值一律要被当成「未经验证的输入」**——
            模型可能把「九折」理解成 0.9（折后价比例）而不是 0.1（折扣幅度），
            也可能把客户号抄错一位。参数的合法性由控制层的 ParameterPolicy 负责。
        confidence: 模型自评置信度。低置信度是**转人工的信号**，不是拒绝的理由。
        reasoning_summary: 简洁的决策说明。

    Note:
        `reasoning_summary` 只保存**简洁的决策说明**，不保存也不索取模型的
        私有思维链。原因：思维链的审计价值远低于它带来的存储成本与泄漏风险，
        而且它本身也不是模型真实计算过程的可靠记录。
    """

    model_config = ConfigDict(extra="forbid")

    intent: str = Field(description="归一化意图，例如 apply_discount / query_customer")
    task_type: str = Field(default="generic", description="任务类型")
    entities: dict[str, Any] = Field(default_factory=dict, description="抽取到的实体")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reasoning_summary: str = Field(default="", max_length=500)
    clarification_needed: bool = Field(
        default=False, description="信息不足以形成动作时置为 true，由 Runtime 转人工或追问"
    )


class ActionProposal(BaseModel):
    """LLM 提出的结构化动作候选。

    这是认知层与控制层之间**唯一**的数据契约。控制层只认这个结构，
    不认自由文本——因为自由文本没法做参数校验，也没法审计。

    Attributes:
        intent: 这个动作服务于什么意图。
        tool_name: 建议调用的工具名。**必须是注册表里存在的名字**，
            否则 :class:`~app.core.errors.ToolNotRegisteredError`。
        arguments: 建议的参数。**仍然是未经验证的**，
            要交给工具自己的 Pydantic 参数模型做强校验。
        reasoning_summary: 简洁的决策说明（不是思维链）。
        confidence: 置信度。
        requested_by: 谁提出的（``llm`` / ``planner`` / ``human``）。
            审计里必须能区分「模型提的」和「人提的」。
        expected_result: 模型预期的结果描述，用于 Reflection 阶段做自检对比。
        risk_hint: 模型对风险的**提示**。注意是 hint——
            真实风险等级由 RiskPolicy 判定，模型说 LOW 不代表就是 LOW。
    """

    model_config = ConfigDict(extra="forbid")

    intent: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reasoning_summary: str = Field(default="", max_length=500)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    requested_by: Literal["llm", "planner", "human", "system"] = "llm"
    expected_result: str = Field(default="", max_length=300)
    risk_hint: RiskLevel = RiskLevel.LOW

    @field_validator("tool_name")
    @classmethod
    def _tool_name_shape(cls, v: str) -> str:
        """工具名必须是简单标识符。

        这条校验挡掉的是路径穿越 / 动态导入类的攻击面：
        如果工具名可以是 ``os.system`` 或 ``../../etc/passwd``，
        而注册表实现又恰好用了动态 import，就会变成任意代码执行。
        我们既不动态 import，也在这里再挡一道——纵深防御。
        """
        if not v or not v.replace("_", "").isalnum():
            raise ValueError("tool_name 必须是字母数字下划线组成的标识符")
        return v


class PlannedStep(BaseModel):
    """执行计划中的一步。

    计划由 LLM 提出，但**步骤的登记、状态流转和恢复全部由 Runtime 负责**。
    模型只说「我建议按这三步做」，说完就没它的事了。
    """

    model_config = ConfigDict(extra="forbid")

    step_name: str = Field(description="步骤名，同时参与幂等键生成，必须稳定")
    proposal: ActionProposal
    #: 是否为关键步骤。关键步骤失败 → 整单失败；
    #: 非关键步骤失败 → 可以走 PARTIAL_SUCCESS（例如「折扣成功但通知失败」）。
    critical: bool = True
    #: 依赖的前置步骤名。用于 READY 判定和并行调度。
    depends_on: list[str] = Field(default_factory=list)


class ExecutionPlan(BaseModel):
    """LLM 产出的执行计划。

    Attributes:
        plan_summary: 一句话说明这个计划要干什么。
        steps: 步骤列表。
        confidence: 整体置信度。

    Note:
        计划里步骤的**顺序有业务含义**：不可撤回的动作（发通知、发短信）
        必须排在最后。原因见 `app/actions/compensation.py`——
        如果通知排在折扣前面，折扣失败时你已经收不回那条短信了。
        这条纪律由 :class:`~app.control.policies.business_rule.BusinessRulePolicy`
        和示例工作流共同保证，**不依赖模型自觉**。
    """

    model_config = ConfigDict(extra="forbid")

    plan_summary: str = Field(default="", max_length=300)
    steps: list[PlannedStep] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ReflectionResult(BaseModel):
    """反思 / 自检结果。

    Reflection 解决的是「模型一本正经地胡说」这一类问题——
    结构合法、规则也过了，但结论本身不合理。

    Warning:
        Reflection 是**质量手段，不是安全手段**。
        它由同一个（或另一个）模型执行，同样不可靠。
        绝不能因为「Reflection 说没问题」就跳过控制层校验。
    """

    model_config = ConfigDict(extra="forbid")

    acceptable: bool = Field(description="执行结果是否达到了预期")
    issues: list[str] = Field(default_factory=list, description="发现的问题")
    suggested_action: Literal["proceed", "retry", "escalate"] = "proceed"
    reasoning_summary: str = Field(default="", max_length=500)


class FinalReply(BaseModel):
    """给用户的最终回复。

    Attributes:
        message: 人类可读的回复正文。
        highlights: 要点列表。

    Note:
        金额、折扣率、单据号这类**事实性数字由程序套模板填入**，
        不让模型自由生成。模型负责组织语言，不负责陈述事实——
        这是「确定性逻辑不交给 LLM」在输出侧的体现。
    """

    model_config = ConfigDict(extra="forbid")

    message: str = Field(max_length=1000)
    highlights: list[str] = Field(default_factory=list)
