"""审计服务。

**审计要能回答的不是「发生了什么」，而是「为什么这么判」。**

只记录结论的审计（「任务失败了」）在事后排查时几乎没有价值。
有价值的是决策依据：「因为 BusinessRulePolicy 判定 12% 超过自助额度 5%，
所以要求经理审批；manager_001 于 14:32 批准，理由是客户年贡献 40 万」。

本服务保证三件事：

1. **每个关键动作都有事件**（覆盖架构文档列出的全部 15 类）；
2. **写入前统一脱敏**：个人信息 → mask，密钥 → redact。
   审计表是长期保存的，一旦写进未脱敏数据，泄漏面会随时间累积；
3. **只追加**：没有 update，没有 delete。可修改的审计等于没有审计。
"""

from __future__ import annotations

from typing import Any

from app.control.data_masking import mask_payload
from app.core.enums import ActorType, AuditEventType
from app.operations.logging import get_logger
from app.runtime.models import AuditEvent
from app.security.secrets import redact
from app.state.repositories import AuditRepository

logger = get_logger(__name__)


class AuditService:
    """审计事件写入服务。

    Args:
        repository: 审计仓库。
    """

    def __init__(self, repository: AuditRepository) -> None:
        self.repository = repository

    async def record(
        self,
        event_type: AuditEventType | str,
        *,
        actor_type: ActorType,
        actor_id: str = "",
        task_id: str | None = None,
        step_id: str | None = None,
        trace_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """写入一条审计事件。

        Args:
            event_type: 事件类型。
            actor_type: 发起者类型。**必须准确**——
                审计要能区分「这一步是人干的、程序干的、还是模型建议的」。
            actor_id: 发起者 ID。
            task_id / step_id: 关联标识。
            trace_id: 链路 ID。
            payload: 事件载荷。会先脱敏再落库。

        Returns:
            写入的审计事件。

        Note:
            脱敏顺序：先 mask（个人信息）再 redact（密钥）。
            两者规则不同、目的不同，都要跑。
        """
        safe_payload = redact(mask_payload(payload or {}))

        event = await self.repository.append(
            event_type=str(event_type),
            actor_type=str(actor_type),
            actor_id=actor_id,
            task_id=task_id,
            step_id=step_id,
            payload=safe_payload,  # type: ignore[arg-type]
            trace_id=trace_id,
        )
        # 审计与日志双写：审计表用于合规与回放，日志用于实时排查与告警。
        # 两者的保留周期和查询方式完全不同，不能互相替代。
        logger.info(
            "audit_event",
            event_type=str(event_type),
            actor_type=str(actor_type),
            actor_id=actor_id,
            task_id=task_id,
            step_id=step_id,
            trace_id=trace_id,
        )
        return event

    # ---------------------------------------------------------------- 便捷方法
    async def task_created(self, task_id: str, user_id: str, agent_id: str, trace_id: str, **extra: Any) -> AuditEvent:
        """记录任务创建。"""
        return await self.record(
            AuditEventType.TASK_CREATED,
            actor_type=ActorType.USER,
            actor_id=user_id,
            task_id=task_id,
            trace_id=trace_id,
            payload={"agent_id": agent_id, **extra},
        )

    async def llm_call(
        self,
        *,
        task_id: str,
        trace_id: str,
        started: bool,
        provider: str = "",
        model: str = "",
        prompt_snapshot: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> AuditEvent:
        """记录 LLM 调用开始 / 结束。

        Note:
            记录的是 **Prompt 快照**（结构化统计 + 已脱敏内容），
            不是完整 Prompt 原文。原因有二：
            线上 Bug 有一半出在「我以为我给了模型 X，其实没给」，
            快照足以回答这个问题；而完整原文既占空间，
            又可能包含不该长期保存的内容。

            同样地，**不记录模型的私有思维链**——
            我们只保存简洁的决策说明（reasoning_summary）。
        """
        if error:
            event_type = AuditEventType.LLM_CALL_FAILED
        elif started:
            event_type = AuditEventType.LLM_CALL_STARTED
        else:
            event_type = AuditEventType.LLM_CALL_FINISHED
        return await self.record(
            event_type,
            actor_type=ActorType.LLM,
            actor_id=provider,
            task_id=task_id,
            trace_id=trace_id,
            payload={
                "provider": provider,
                "model": model,
                "prompt_snapshot": prompt_snapshot or {},
                "usage": usage or {},
                "error": error,
            },
        )

    async def proposal_generated(
        self,
        *,
        task_id: str,
        trace_id: str,
        proposals: list[dict[str, Any]],
    ) -> AuditEvent:
        """记录 ActionProposal 生成。

        actor_type 是 LLM 而不是 AGENT：**这是模型提的建议，不是系统的决定**。
        这个区分在事后追责时至关重要。
        """
        return await self.record(
            AuditEventType.PROPOSAL_GENERATED,
            actor_type=ActorType.LLM,
            task_id=task_id,
            trace_id=trace_id,
            payload={"proposals": proposals},
        )

    async def policy_decision(
        self,
        *,
        task_id: str,
        step_id: str | None,
        trace_id: str,
        decision_payload: dict[str, Any],
    ) -> AuditEvent:
        """记录控制层裁决。

        actor_type 是 SYSTEM：**放行与否是程序的决定，不是模型的**。
        """
        return await self.record(
            AuditEventType.POLICY_DECISION,
            actor_type=ActorType.SYSTEM,
            actor_id="policy_engine",
            task_id=task_id,
            step_id=step_id,
            trace_id=trace_id,
            payload=decision_payload,
        )

    async def tool_execution(
        self,
        *,
        task_id: str,
        step_id: str,
        trace_id: str,
        tool_name: str,
        started: bool,
        payload: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """记录工具执行开始 / 结束。"""
        return await self.record(
            AuditEventType.TOOL_EXECUTION_STARTED
            if started
            else AuditEventType.TOOL_EXECUTION_FINISHED,
            actor_type=ActorType.TOOL,
            actor_id=tool_name,
            task_id=task_id,
            step_id=step_id,
            trace_id=trace_id,
            payload={"tool_name": tool_name, **(payload or {})},
        )

    async def state_transition(
        self,
        *,
        task_id: str,
        step_id: str | None,
        trace_id: str,
        scope: str,
        from_status: str,
        to_status: str,
        event: str,
        legal: bool = True,
    ) -> AuditEvent:
        """记录状态转换。

        **非法转换必须写审计**（`legal=False`）：它通常意味着代码里有并发
        或逻辑 Bug，静默吞掉等于放弃了唯一的发现机会。
        """
        return await self.record(
            AuditEventType.STATE_TRANSITION if legal else AuditEventType.ILLEGAL_STATE_TRANSITION,
            actor_type=ActorType.SYSTEM,
            actor_id="state_machine",
            task_id=task_id,
            step_id=step_id,
            trace_id=trace_id,
            payload={
                "scope": scope,
                "from": from_status,
                "to": to_status,
                "event": event,
                "legal": legal,
            },
        )

    async def reconciliation(
        self,
        *,
        task_id: str,
        step_id: str,
        trace_id: str,
        idempotency_key: str,
        outcome: str,
        external_reference_id: str | None = None,
    ) -> AuditEvent:
        """记录对账结果。

        这是超时处理里最重要的一条审计：它回答了
        「那次超时到底是成了还是没成，我们是怎么查明的」。
        """
        return await self.record(
            AuditEventType.RECONCILIATION,
            actor_type=ActorType.SYSTEM,
            actor_id="reconciler",
            task_id=task_id,
            step_id=step_id,
            trace_id=trace_id,
            payload={
                "idempotency_key": idempotency_key,
                "outcome": outcome,
                "external_reference_id": external_reference_id,
            },
        )

    async def compensation(
        self,
        *,
        task_id: str,
        step_id: str,
        trace_id: str,
        tool_name: str,
        started: bool,
        payload: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """记录补偿开始 / 结束。

        补偿有**独立的审计事件**，而不是复用工具执行事件。
        原因：补偿是一次新的业务动作，它的成败、耗时、失败原因
        都需要独立追踪。把它混进正向动作的记录里，
        事后就分不清「这条记录是发折扣还是撤折扣」。
        """
        return await self.record(
            AuditEventType.COMPENSATION_STARTED
            if started
            else AuditEventType.COMPENSATION_FINISHED,
            actor_type=ActorType.SYSTEM,
            actor_id="compensation_manager",
            task_id=task_id,
            step_id=step_id,
            trace_id=trace_id,
            payload={"tool_name": tool_name, **(payload or {})},
        )

    async def approval(
        self,
        *,
        task_id: str,
        step_id: str,
        trace_id: str,
        requested: bool,
        actor_id: str,
        payload: dict[str, Any],
    ) -> AuditEvent:
        """记录审批请求 / 决策。"""
        return await self.record(
            AuditEventType.APPROVAL_REQUESTED if requested else AuditEventType.APPROVAL_DECIDED,
            actor_type=ActorType.SYSTEM if requested else ActorType.APPROVER,
            actor_id=actor_id,
            task_id=task_id,
            step_id=step_id,
            trace_id=trace_id,
            payload=payload,
        )

    async def retry_scheduled(
        self,
        *,
        task_id: str,
        step_id: str,
        trace_id: str,
        attempt: int,
        delay_seconds: float,
        error_code: str | None,
    ) -> AuditEvent:
        """记录重试安排。"""
        return await self.record(
            AuditEventType.RETRY_SCHEDULED,
            actor_type=ActorType.SYSTEM,
            actor_id="retry_manager",
            task_id=task_id,
            step_id=step_id,
            trace_id=trace_id,
            payload={
                "attempt": attempt,
                "delay_seconds": delay_seconds,
                "error_code": error_code,
            },
        )

    async def manual_intervention(
        self,
        *,
        task_id: str,
        step_id: str | None,
        trace_id: str,
        reason: str,
        actor_id: str = "system",
    ) -> AuditEvent:
        """记录人工介入。"""
        return await self.record(
            AuditEventType.MANUAL_INTERVENTION,
            actor_type=ActorType.SYSTEM,
            actor_id=actor_id,
            task_id=task_id,
            step_id=step_id,
            trace_id=trace_id,
            payload={"reason": reason},
        )
