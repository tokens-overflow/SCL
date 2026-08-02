"""工具抽象。

**为什么 LLM 不能直接执行工具？**

因为「提出动作」和「允许执行」是两件完全不同的事：

* 提出动作是**认知问题**——把模糊的自然语言变成明确的候选动作，没有唯一正确答案，
  正是模型擅长的。
* 允许执行是**执行决策**——这个用户有没有权限、参数是否合法、是否需要审批、
  是否可以重试。这些问题**有唯一正确答案**，而且出错之后需要有人负责。

有唯一答案、且出错要有人负责的问题，必须交给程序。所以这个模块里的
`AgentTool.execute()` 永远只被 :class:`~app.actions.executor.ActionExecutor` 调用，
而 Executor 只接受**已经通过控制层的** `PolicyDecision.validated_arguments`。
模型手里从头到尾没有一个能直接触发副作用的入口。

工具契约里几个字段值得单独说明：

* ``idempotent``：写操作**必须**为 True，否则重试就是重复副作用。
* ``supports_compensation``：不可补偿的动作（已发出的短信）要排在链路最后。
* ``query_external_status()``：对账接口。**没有它，一条 UNKNOWN 记录就永远查不清了。**
"""

from __future__ import annotations

import abc
from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import RiskLevel, StepType, ToolExecutionStatus
from app.core.ids import utcnow


class ToolExecutionContext(BaseModel):
    """一次工具执行所需的运行时上下文。

    工具拿到的是**这个对象**，而不是整个 AgentContext——
    工具不需要知道 Prompt 长什么样、检索到了什么知识。
    最小知识原则，也让工具更容易被单独测试。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    task_id: str
    step_id: str
    step_name: str
    execution_id: str
    #: 幂等键。**工具必须把它透传给下游系统**，
    #: 下游的唯一约束才是「至多一次副作用」的物理保证。
    idempotency_key: str
    trace_id: str = ""
    user_id: str = ""
    agent_id: str = ""
    attempt: int = 1
    timeout_seconds: float = 10.0
    #: 数据库会话。Demo 里工具直接读写演示业务表；
    #: 真实项目里这里应该是下游服务的 client。
    session: Any = None
    #: 附加参数（例如故障注入开关）。
    extra: dict[str, Any] = Field(default_factory=dict)


class ToolExecutionResult(BaseModel):
    """工具执行结果。

    这是行动层向上返回的统一契约。**它必须能表达「我不知道」**——
    这就是 `status` 里有 ``TIMEOUT`` 和 ``UNKNOWN`` 的原因。
    一个只能表达成功/失败的结果类型，会逼着调用方在超时时二选一，
    而两个选择都可能是错的。
    """

    model_config = ConfigDict(from_attributes=True)

    tool_name: str
    execution_id: str
    status: ToolExecutionStatus
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    #: 是否可重试。**由工具自己声明**——只有它知道这次失败的性质。
    retryable: bool = False
    #: 外部系统返回的凭证（流水号、单据号）。对账的钥匙之一。
    external_reference_id: str | None = None
    idempotency_key: str = ""
    started_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None

    @property
    def succeeded(self) -> bool:
        """是否明确成功（含幂等命中）。"""
        return self.status in (
            ToolExecutionStatus.SUCCESS,
            ToolExecutionStatus.SKIPPED_IDEMPOTENT,
        )

    @property
    def unresolved(self) -> bool:
        """结果是否未知（必须走对账，既不能当成功也不能当失败）。"""
        return self.status in (ToolExecutionStatus.TIMEOUT, ToolExecutionStatus.UNKNOWN)

    def summarize(self) -> str:
        """生成一句话摘要，用于回填上下文与最终汇总。"""
        if self.succeeded:
            ref = f"（凭证 {self.external_reference_id}）" if self.external_reference_id else ""
            return f"执行成功{ref}"
        if self.unresolved:
            return "执行结果未知，等待对账"
        return f"执行失败：{self.error_message or self.error_code or '未知原因'}"


class AgentTool(abc.ABC):
    """工具基类。

    子类必须声明的类属性构成了工具的**能力契约**。这些信息会被：

    * 控制层用来做权限与风险判定（`required_permissions`、`risk_level`）；
    * 执行器用来决定重试与对账策略（`idempotent`、`supports_compensation`）；
    * 认知层用来渲染工具清单（`name`、`description`、`args_model`）。

    Attributes:
        name: 工具名。**必须与注册表中的键一致**，也参与幂等键生成。
        description: 给 LLM 看的说明。写清楚「什么时候该用」比「怎么用」更重要。
        risk_level: 风险等级。决定是否需要审批。
        required_permissions: 调用所需权限。会与「用户 ∩ Agent ∩ 服务账号」比对。
        idempotent: 是否幂等。**写工具必须为 True。**
        supports_compensation: 是否支持补偿。
        step_type: 步骤类型，决定是否需要幂等键与对账。
        service_id: 背后的服务账号，参与三方权限交集。
        args_model: 参数的 Pydantic 模型。
            **禁止把未经验证的字典直接传进工具**——
            这是「结构化输出只是必要条件」在工具侧的落点。
        default_timeout_seconds: 默认超时。
    """

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    risk_level: ClassVar[RiskLevel] = RiskLevel.LOW
    required_permissions: ClassVar[set[str]] = set()
    idempotent: ClassVar[bool] = True
    supports_compensation: ClassVar[bool] = False
    step_type: ClassVar[StepType] = StepType.READ
    service_id: ClassVar[str] = ""
    args_model: ClassVar[type[BaseModel]]
    default_timeout_seconds: ClassVar[float] = 10.0

    @abc.abstractmethod
    async def execute(
        self,
        arguments: BaseModel,
        execution_context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        """执行工具。

        Args:
            arguments: **已经通过 `args_model` 校验**的参数对象。
                注意类型标注是 `BaseModel` 而不是 `dict`——
                这在类型层面就杜绝了「把模型吐出来的原始字典直接塞进去」。
            execution_context: 执行上下文，含幂等键、trace_id 等。

        Returns:
            :class:`ToolExecutionResult`。

        Note:
            实现者的三条纪律：

            1. 写操作**必须**把 `execution_context.idempotency_key` 透传给下游；
            2. 失败时**必须**如实声明 `retryable`，不要一律填 True；
            3. 拿不准结果时**必须**返回 UNKNOWN，而不是猜一个 FAILED。
        """

    async def compensate(
        self,
        previous_result: ToolExecutionResult,
        execution_context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        """补偿（撤销）此前成功的执行。

        Args:
            previous_result: 需要被撤销的那次执行的结果。
            execution_context: 补偿动作自己的执行上下文
                （**有自己的幂等键**，通常是原键 + ``:comp`` 后缀）。

        Returns:
            补偿动作的执行结果。

        Raises:
            NotImplementedError: 默认实现。**不可逆的动作就应该在这里报错**，
                而不是假装补偿成功——「不能简单假设所有动作都可逆」。

        Note:
            补偿是一个**新的正向业务动作**，不是数据库回滚。
            它有自己的状态、自己的幂等键、自己的审计记录，也可能自己失败。
        """
        raise NotImplementedError(
            f"工具 {self.name} 不支持补偿：该动作不可逆，失败时需要人工跟进"
        )

    async def query_external_status(
        self,
        idempotency_key: str,
        execution_context: ToolExecutionContext,
    ) -> ToolExecutionResult | None:
        """对账：查询外部系统里这个幂等键的真实执行状态。

        **这是超时处理里最关键的一个方法。**

        超时的真相有三种：请求没到达、已经成功但响应丢了、对方还在处理。
        两种错误处置都会出事：

        * 当成功处理 → 如果其实没执行，库里记着「已完成」，钱还在，
          用户投诉时你查不出问题。
        * 当失败并回滚 → 如果其实已经执行了，你凭空少一笔，
          而且回滚动作本身可能再制造一次不一致。

        唯一正确的处置是拿幂等键去问外部系统。

        Args:
            idempotency_key: 待查询的幂等键。
            execution_context: 执行上下文。

        Returns:
            * 查到已成功 → 返回带 `external_reference_id` 的 SUCCESS 结果；
            * 查到确实没发生 → 返回 FAILED 且 `retryable=True`（可安全重试）；
            * **查无可查 → 返回 ``None``**，调用方应升级人工，而不是猜。

        Note:
            默认实现返回 ``None``。只读工具不需要对账（重试无副作用），
            但**所有写工具都必须实现这个方法**。
        """
        return None

    def build_result(
        self,
        execution_context: ToolExecutionContext,
        *,
        status: ToolExecutionStatus,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        retryable: bool = False,
        external_reference_id: str | None = None,
        started_at: datetime | None = None,
    ) -> ToolExecutionResult:
        """构造结果对象的辅助方法，保证字段填写一致。"""
        return ToolExecutionResult(
            tool_name=self.name,
            execution_id=execution_context.execution_id,
            status=status,
            result=result,
            error_code=error_code,
            error_message=error_message,
            retryable=retryable,
            external_reference_id=external_reference_id,
            idempotency_key=execution_context.idempotency_key,
            started_at=started_at or utcnow(),
            completed_at=utcnow(),
        )

    @classmethod
    def describe(cls) -> dict[str, Any]:
        """返回工具的自描述信息。

        用于 ``GET /tools`` 与给 LLM 渲染工具清单。
        **注意这里不包含 `required_permissions`**——
        权限是控制层的事，写进给模型的清单里既没用也不安全。
        """
        return {
            "name": cls.name,
            "description": cls.description,
            "risk_level": str(cls.risk_level),
            "idempotent": cls.idempotent,
            "supports_compensation": cls.supports_compensation,
            "step_type": str(cls.step_type),
            "arguments_schema": cls.args_model.model_json_schema(),
        }
