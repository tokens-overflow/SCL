"""发放客户折扣（写工具 · 幂等 · 可补偿）。

这是整个骨架里最值得逐行读的工具，因为它同时演示了四件事：

1. **幂等写入**：幂等键透传给下游，下游用唯一约束去重。
   同一个键写一百次也只生效一次。
2. **超时不等于失败**：`timeout_after_commit` 故障模式下，
   写入其实已经成功，只是响应没回来。这时候直接重试会多打一次折，
   直接回滚会凭空少一笔——两个都是错的。
3. **对账**：`query_external_status()` 拿幂等键去下游查真实结果。
   **没有这个方法，一条 UNKNOWN 记录就永远查不清了。**
4. **补偿**：`compensate()` 撤销已发放的折扣。它是一个
   **新的正向业务动作**（写一条撤销记录 + 改状态），不是数据库回滚。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.actions.base import AgentTool, ToolExecutionContext, ToolExecutionResult
from app.actions.tools.fault_injection import fault_injector
from app.core.enums import ErrorCode, RiskLevel, StepType, ToolExecutionStatus
from app.core.errors import ToolTimeoutError
from app.core.ids import new_id, utcnow
from app.security.identity import PERM_DISCOUNT_APPLY
from app.state.models import CustomerORM, DiscountORM


class ApplyDiscountArgs(BaseModel):
    """`apply_discount` 的参数模型。

    Pydantic 在这里能保证的是**格式与取值范围**：
    `discount_rate` 一定是 0~1 之间的浮点数，`customer_id` 一定非空。

    它保证不了的是：这个客服有没有资格给这么大的折扣、
    这个客户是不是已经有生效折扣、要不要经理审批。
    那些是业务规则，由 :mod:`app.control` 负责。

    **结构化输出只是必要条件，不是充分条件**——
    一个格式完全合法的 30% 折扣请求，依然必须被拒绝。
    """

    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(min_length=2, max_length=32, description="客户编号")
    discount_rate: float = Field(
        gt=0.0, le=1.0, description="折扣幅度，0.1 表示优惠 10%（即打九折）"
    )
    reason: str = Field(default="", max_length=200, description="折扣原因")


class ApplyDiscountTool(AgentTool):
    """为客户发放折扣。"""

    name = "apply_discount"
    description = (
        "为指定客户创建生效折扣。discount_rate 是折扣幅度（0.1 = 打九折）。"
        "该操作会修改计费数据，属于写操作。"
    )
    # 静态风险等级是**基线**，不是最终判定。
    #
    # 「发一笔折扣」的真实风险取决于金额：5% 和 30% 完全不是一回事。
    # 所以这里声明的是「这类动作至少是 MEDIUM 风险」，
    # 而实际等级由 RiskPolicy 根据 discount_rate 等**参数**向上抬
    # （超过自助额度 → HIGH，触发审批）。
    #
    # 如果把静态等级直接写成 HIGH，那么连 1% 的折扣也要走审批——
    # 而审批疲劳会让真正重要的审批被随手点过，反而更不安全。
    risk_level = RiskLevel.MEDIUM
    required_permissions = {PERM_DISCOUNT_APPLY}
    idempotent = True
    supports_compensation = True
    step_type = StepType.WRITE
    service_id = "billing_service"
    args_model = ApplyDiscountArgs
    default_timeout_seconds = 8.0

    async def execute(
        self,
        arguments: BaseModel,
        execution_context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        """创建折扣记录。

        Raises:
            ToolTimeoutError: 模拟超时。**注意这个异常表达的是「我不知道结果」**，
                执行器会把步骤落成 TIMEOUT 并进入对账流程，
                绝不会当作失败直接回滚。
        """
        assert isinstance(arguments, ApplyDiscountArgs)
        started = utcnow()
        session = execution_context.session
        idem_key = execution_context.idempotency_key

        # —— 故障注入（仅演示用）——
        fault = fault_injector.take(self.name)

        if fault == "permanent_failure":
            return self.build_result(
                execution_context,
                status=ToolExecutionStatus.FAILED,
                error_code=ErrorCode.BUSINESS_RULE_VIOLATION,
                error_message="下游计费系统拒绝：客户账户状态不允许调整折扣",
                retryable=False,
                started_at=started,
            )
        if fault == "transient_failure":
            return self.build_result(
                execution_context,
                status=ToolExecutionStatus.FAILED,
                error_code=ErrorCode.UPSTREAM_UNAVAILABLE,
                error_message="下游计费系统暂时不可用（模拟 503）",
                retryable=True,
                started_at=started,
            )
        if fault == "crash":
            raise RuntimeError("模拟进程崩溃：写入过程中断电")
        if fault == "timeout_before_commit":
            # 超时的第一种真相：**请求根本没到达对方，写入没有发生。**
            #
            # 注意这里是在任何写入**之前**抛出的。这不只是为了方便：
            # 工具绝不能对共享会话调用 `session.rollback()`——
            # 那会连带回滚掉执行器在本次事务里写下的 IN_FLIGHT 占位记录，
            # 于是「先占位再执行」这套保护就被工具自己拆掉了。
            # 真实系统里下游是独立服务、独立事务，天然没有这个问题；
            # Demo 里下游表和框架表共用一个会话，所以必须显式守住这条纪律。
            raise ToolTimeoutError(
                "折扣写入超时，结果未知（请求未到达下游）",
                details={"idempotency_key": idem_key},
            )

        # —— 步骤 1：下游侧的幂等检查 ——
        # 真实系统里这一步发生在下游服务内部。放在这里是为了让读者看清
        # 「同一个键写一百次也只生效一次」这件事到底是怎么发生的。
        existing = await self._find_by_key(session, idem_key)
        if existing is not None:
            return self.build_result(
                execution_context,
                status=ToolExecutionStatus.SKIPPED_IDEMPOTENT,
                result=_discount_payload(existing),
                external_reference_id=existing.discount_id,
                started_at=started,
            )

        customer = await session.get(CustomerORM, arguments.customer_id)
        if customer is None:
            return self.build_result(
                execution_context,
                status=ToolExecutionStatus.FAILED,
                error_code=ErrorCode.NOT_FOUND,
                error_message=f"客户不存在：{arguments.customer_id}",
                retryable=False,
                started_at=started,
            )

        # —— 步骤 2：业务约束（下游侧）——
        # 「已有生效折扣时不能重复创建」是下游系统的硬约束。
        # 控制层也会查一遍，但下游必须自己也守住——
        # 不能假设所有调用方都守规矩。
        active = await self._find_active(session, arguments.customer_id)
        if active is not None:
            return self.build_result(
                execution_context,
                status=ToolExecutionStatus.FAILED,
                error_code=ErrorCode.DUPLICATE_ACTIVE_DISCOUNT,
                error_message=(
                    f"客户 {arguments.customer_id} 已有生效折扣 "
                    f"{active.discount_rate:.0%}，需先撤销"
                ),
                retryable=False,
                external_reference_id=active.discount_id,
                started_at=started,
            )

        # —— 步骤 3：写入 ——
        discount = DiscountORM(
            discount_id=new_id("disc"),
            customer_id=arguments.customer_id,
            discount_rate=arguments.discount_rate,
            reason=arguments.reason,
            status="ACTIVE",
            idempotency_key=idem_key,
            created_by=execution_context.user_id,
        )
        try:
            # 用 **SAVEPOINT**（嵌套事务）隔离这次插入。
            #
            # 为什么不能直接 `session.rollback()`：那会回滚整个外层事务，
            # 把执行器写下的 IN_FLIGHT 占位记录、审计事件一起清掉——
            # 恰恰是崩溃恢复最需要的那些记录。
            # savepoint 让「唯一约束冲突」这件事的回滚范围
            # 精确地限制在这一条 INSERT 上。
            async with session.begin_nested():
                session.add(discount)
                await session.flush()
        except IntegrityError:
            # 并发下另一个执行抢先用同一个键写入了。
            # 这正是数据库唯一约束的价值：应用层的「先查再写」挡不住并发。
            existing = await self._find_by_key(session, idem_key)
            if existing is not None:
                return self.build_result(
                    execution_context,
                    status=ToolExecutionStatus.SKIPPED_IDEMPOTENT,
                    result=_discount_payload(existing),
                    external_reference_id=existing.discount_id,
                    started_at=started,
                )
            raise

        if fault == "timeout_after_commit":
            # 最危险的场景：**写入已经成功，但响应没回来。**
            #
            # 如果调用方把这当成失败：库里记着「没打折」，实际已经打了。
            # 如果调用方直接重试：幸好有幂等键，第二次会命中 SKIPPED_IDEMPOTENT。
            #
            # 正确的处置是落 TIMEOUT → 拿幂等键对账 → 补写状态和 external_ref。
            #
            # 注意这里**不调用 session.commit()**：提交是外层事务边界的职责。
            # 写入已经 flush 到本事务里，后续的对账查询能看到它——
            # 这正是「写入已生效但响应没回来」要模拟的状态。
            raise ToolTimeoutError(
                "折扣写入超时，结果未知",
                details={"idempotency_key": idem_key, "hint": "写入可能已经生效，必须对账"},
            )
        return self.build_result(
            execution_context,
            status=ToolExecutionStatus.SUCCESS,
            result=_discount_payload(discount),
            external_reference_id=discount.discount_id,
            started_at=started,
        )

    async def query_external_status(
        self,
        idempotency_key: str,
        execution_context: ToolExecutionContext,
    ) -> ToolExecutionResult | None:
        """对账：拿幂等键去下游查真实执行状态。

        这是超时处理的**唯一正确出路**。查询优先级：

        1. `external_reference_id`（业务单据号）——最精确；
        2. `idempotency_key`——所以幂等键必须落库；
        3. 都查不到 → 返回「未发生且可重试」。

        Returns:
            * 查到已生效 → SUCCESS，带 `external_reference_id`，调用方只需补写状态；
            * 确认未发生 → FAILED 且 `retryable=True`，可以带同一个幂等键安全重试；
            * ``None`` 表示查无可查（本实现不会返回，真实系统可能会）。

        Note:
            对账不是一次性的：外部系统可能「稍后会成功」。
            所以真实实现应该带退避地查几轮，仍不明确才升级人工。
            本实现的下游是本地表，查询是确定的，所以一轮就够。
        """
        session = execution_context.session
        existing = await self._find_by_key(session, idempotency_key)
        if existing is not None:
            return self.build_result(
                execution_context,
                status=ToolExecutionStatus.SUCCESS,
                result=_discount_payload(existing),
                external_reference_id=existing.discount_id,
            )
        return self.build_result(
            execution_context,
            status=ToolExecutionStatus.FAILED,
            error_code=ErrorCode.UPSTREAM_UNAVAILABLE,
            error_message="对账确认：折扣未生效，可安全重试",
            retryable=True,
        )

    async def compensate(
        self,
        previous_result: ToolExecutionResult,
        execution_context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        """撤销已发放的折扣。

        **这不是数据库回滚。** 事务回滚只能撤销「我们自己这次事务里的写入」，
        而这里要撤销的是一次**已经提交、已经对外生效**的业务动作。
        所以补偿本身是一次新的写入：把折扣状态改成 REVOKED，记下撤销原因和时间。
        它有自己的幂等键、自己的状态、自己的审计记录，也可能自己失败。

        Args:
            previous_result: 需要撤销的那次执行的结果
                （`external_reference_id` 就是折扣单号）。
            execution_context: 补偿动作自己的执行上下文。

        Returns:
            补偿结果。目标折扣已不存在或已撤销时返回 SUCCESS——
            **补偿动作本身必须幂等**，因为它也会被重试。
        """
        started = utcnow()
        session = execution_context.session
        discount_id = previous_result.external_reference_id

        if not discount_id:
            # 没有单据号就无法精确撤销。宁可报错转人工，也不要「猜一条撤销」。
            return self.build_result(
                execution_context,
                status=ToolExecutionStatus.FAILED,
                error_code=ErrorCode.NOT_FOUND,
                error_message="缺少折扣单号，无法执行补偿，需人工跟进",
                retryable=False,
                started_at=started,
            )

        discount = await session.get(DiscountORM, discount_id)
        if discount is None or discount.status == "REVOKED":
            # 幂等：已经撤销过了就当成功。
            return self.build_result(
                execution_context,
                status=ToolExecutionStatus.SKIPPED_IDEMPOTENT,
                result={"discount_id": discount_id, "status": "REVOKED"},
                external_reference_id=discount_id,
                started_at=started,
            )

        discount.status = "REVOKED"
        discount.revoked_at = utcnow()
        discount.revoke_reason = f"Saga 补偿：任务 {execution_context.task_id} 后续步骤失败"
        await session.flush()

        return self.build_result(
            execution_context,
            status=ToolExecutionStatus.SUCCESS,
            result={"discount_id": discount_id, "status": "REVOKED"},
            external_reference_id=discount_id,
            started_at=started,
        )

    # ------------------------------------------------------------------ 内部
    @staticmethod
    async def _find_by_key(session, idem_key: str) -> DiscountORM | None:  # noqa: ANN001
        result = await session.execute(
            select(DiscountORM).where(DiscountORM.idempotency_key == idem_key)
        )
        return result.scalars().first()

    @staticmethod
    async def _find_active(session, customer_id: str) -> DiscountORM | None:  # noqa: ANN001
        result = await session.execute(
            select(DiscountORM)
            .where(DiscountORM.customer_id == customer_id)
            .where(DiscountORM.status == "ACTIVE")
        )
        return result.scalars().first()


def _discount_payload(discount: DiscountORM) -> dict[str, object]:
    return {
        "discount_id": discount.discount_id,
        "customer_id": discount.customer_id,
        "discount_rate": discount.discount_rate,
        "status": discount.status,
        "created_at": discount.created_at.isoformat(),
    }
