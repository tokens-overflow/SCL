"""Mock LLM Provider（默认实现）。

**为什么 Mock Provider 是这个骨架里最重要的 Provider 之一？**

1. **零外部依赖启动**：`git clone` 之后不配任何 API Key 就能跑通完整流程。
   一个必须先申请 API Key 才能看到效果的骨架，学习成本会高一个数量级。

2. **确定性测试**：测试不能依赖真实模型。真实模型每次输出都可能不同，
   于是「测试挂了」这件事就失去了信号价值——你分不清是代码错了还是模型飘了。
   Mock Provider 对同样的输入永远给同样的结构化输出，
   这样单元测试测的才是**我们的编排逻辑**，而不是模型的心情。

3. **故障注入**：可以精确地制造「模型输出格式不合法」「模型幻觉出未注册工具」
   「模型建议了越权动作」这些场景，用来验证控制层是否真的兜住了。

Mock 的解析逻辑是**规则化**的（正则 + 中文数字表）。这本身也是一个教学点：
很多线上「Agent」在业务跑顺之后会发现根本不需要模型——规则就够了。
有稳定契约的话，这种降级是无痛的。
"""

from __future__ import annotations

import re
from typing import Any, TypeVar

from pydantic import BaseModel

from app.cognitive.models import (
    ActionProposal,
    ExecutionPlan,
    FinalReply,
    IntentParseResult,
    PlannedStep,
    ReflectionResult,
)
from app.context.models import AgentContext
from app.core.enums import RiskLevel
from app.core.errors import LLMOutputInvalidError, LLMProviderError
from app.llm.base import BaseLLMProvider, LLMMessage, LLMUsage

ResponseModelT = TypeVar("ResponseModelT", bound=BaseModel)


# --------------------------------------------------------------------------------------
# 中文折扣表达的解析规则
# --------------------------------------------------------------------------------------
_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

#: 「打九折」= 按原价的 90% 收费 = 折扣幅度 10%。
#: 这是一个非常经典的歧义点：中文的「九折」说的是**折后比例**，
#: 而系统里的 discount_rate 通常指**折扣幅度**。
#: 把这个转换放在 Mock Provider（认知层）里是对的——它属于「语义理解」。
#: 但转换结果仍然要过控制层的上下限校验，因为理解错了照样会越界。
_ZHE_PATTERN = re.compile(r"打?\s*([一二两三四五六七八九十\d]+(?:\.\d+)?)\s*折")
_PERCENT_OFF_PATTERN = re.compile(r"(?:优惠|减|降|折扣|打折)\s*(\d+(?:\.\d+)?)\s*[%％]")
_PERCENT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*[%％]")
_CUSTOMER_PATTERN = re.compile(r"\b([Cc]\d{3,})\b")


def _cn_number(token: str) -> float | None:
    """把中文/阿拉伯数字串转成浮点数。"""
    token = token.strip()
    try:
        return float(token)
    except ValueError:
        pass
    if token == "十":
        return 10.0
    if len(token) == 1 and token in _CN_DIGITS:
        return float(_CN_DIGITS[token])
    if len(token) == 2 and token[0] in _CN_DIGITS and token[1] in _CN_DIGITS:
        # 「九五」折 → 9.5 折
        return float(_CN_DIGITS[token[0]]) + float(_CN_DIGITS[token[1]]) / 10
    return None


def parse_discount_rate(text: str) -> float | None:
    """从自然语言中解析折扣幅度（0~1）。

    Args:
        text: 用户输入，例如「给客户 C001 打九折」。

    Returns:
        折扣幅度。「九折」→ ``0.1``，「打 8.5 折」→ ``0.15``，
        「优惠 30%」→ ``0.3``。解析不出来返回 ``None``。

    Note:
        返回 ``None`` 时**不要猜一个默认值**。宁可让流程停下来问人，
        也不要静默地给客户打一个谁也没要求过的折扣。
    """
    m = _PERCENT_OFF_PATTERN.search(text)
    if m:
        return round(float(m.group(1)) / 100, 4)

    m = _ZHE_PATTERN.search(text)
    if m:
        zhe = _cn_number(m.group(1))
        if zhe is not None:
            # 「九折」：9 → 0.9 折后比例 → 0.1 折扣幅度
            ratio = zhe / 10 if zhe > 1 else zhe
            return round(max(0.0, 1 - ratio), 4)

    m = _PERCENT_PATTERN.search(text)
    if m:
        return round(float(m.group(1)) / 100, 4)

    return None


def parse_customer_id(text: str) -> str | None:
    """从自然语言中解析客户号（形如 C001）。"""
    m = _CUSTOMER_PATTERN.search(text)
    return m.group(1).upper() if m else None


class MockLLMProvider(BaseLLMProvider):
    """规则驱动的 Mock Provider。

    Attributes:
        force_error: 若不为 ``None``，每次调用直接抛出该异常。
            用于测试「LLM 挂了之后系统怎么办」。
        force_payload: 若不为 ``None``，直接用这个 dict 去构造 response_model。
            用于精确构造「模型幻觉出未注册工具」等场景。
        latency_ms: 模拟延迟，用于测试超时路径。
    """

    name = "mock"

    def __init__(
        self,
        *,
        force_error: Exception | None = None,
        force_payload: dict[str, Any] | None = None,
        latency_ms: int = 0,
        model_name: str = "mock-deterministic-1",
    ) -> None:
        super().__init__()
        self.force_error = force_error
        self.force_payload = force_payload
        self.latency_ms = latency_ms
        self.model_name = model_name
        #: 调用计数，测试里用来断言「LLM 只在该调用的时候被调用了」。
        #: 这个断言比看上去重要：**恢复流程里一次模型调用都不应该有**。
        self.call_count = 0

    async def _complete(
        self,
        messages: list[LLMMessage],
        context: AgentContext,
        *,
        json_mode: bool = False,
    ) -> tuple[str, LLMUsage]:
        """Mock 不走文本补全路径，由 :meth:`generate_structured` 直接构造对象。"""
        raise NotImplementedError("MockLLMProvider 直接重写了 generate_structured / generate_text")

    async def generate_structured(
        self,
        messages: list[LLMMessage],
        response_model: type[ResponseModelT],
        context: AgentContext,
    ) -> ResponseModelT:
        """按 response_model 的类型返回确定性的结构化结果。

        Args:
            messages: 消息列表（Mock 只用其中的用户输入，其余用于审计快照）。
            response_model: 期望的输出结构。
            context: Agent 上下文。

        Returns:
            `response_model` 实例。

        Raises:
            LLMProviderError: 当 `force_error` 被设置时。
            LLMOutputInvalidError: 当 `force_payload` 不满足目标结构时——
                这正是我们希望测到的路径：**格式不合法必须报错，不能猜**。
        """
        self.call_count += 1
        if self.force_error is not None:
            raise self.force_error

        usage = LLMUsage(
            prompt_tokens=sum(len(m.content) // 4 for m in messages),
            completion_tokens=64,
            total_tokens=sum(len(m.content) // 4 for m in messages) + 64,
            model=self.model_name,
            provider=self.name,
        )
        self._record(context, usage, 1, response_model)

        if self.force_payload is not None:
            try:
                return response_model.model_validate(self.force_payload)
            except Exception as exc:  # noqa: BLE001 - 统一转换成框架异常
                raise LLMOutputInvalidError(
                    "注入的 mock payload 不符合目标结构",
                    details={"response_model": response_model.__name__, "error": str(exc)},
                ) from exc

        user_text = context.user_input or _last_user_message(messages)

        if response_model is IntentParseResult:
            return self._parse_intent(user_text)  # type: ignore[return-value]
        if response_model is ExecutionPlan:
            return self._build_plan(user_text, context)  # type: ignore[return-value]
        if response_model is ActionProposal:
            plan = self._build_plan(user_text, context)
            if not plan.steps:
                raise LLMOutputInvalidError("无法从输入中形成任何动作建议")
            return plan.steps[0].proposal  # type: ignore[return-value]
        if response_model is ReflectionResult:
            return ReflectionResult(  # type: ignore[return-value]
                acceptable=True,
                suggested_action="proceed",
                reasoning_summary="所有关键步骤均已按预期完成。",
            )
        if response_model is FinalReply:
            return FinalReply(  # type: ignore[return-value]
                message=self._compose_reply(context),
                highlights=[s.step_name for s in context.recent_steps],
            )

        raise LLMProviderError(
            f"MockLLMProvider 未实现对 {response_model.__name__} 的支持",
            details={"response_model": response_model.__name__},
        )

    async def generate_text(
        self,
        messages: list[LLMMessage],
        context: AgentContext,
    ) -> str:
        """返回一段确定性的文本回复。"""
        self.call_count += 1
        if self.force_error is not None:
            raise self.force_error
        usage = LLMUsage(model=self.model_name, provider=self.name)
        self._record(context, usage, 1, None)
        return self._compose_reply(context)

    # ------------------------------------------------------------------ 内部规则
    def _parse_intent(self, text: str) -> IntentParseResult:
        """从用户输入解析意图与实体。"""
        customer_id = parse_customer_id(text)
        discount_rate = parse_discount_rate(text)
        wants_notify = any(kw in text for kw in ("通知", "告知", "发消息", "notify"))

        # 退款类请求：故意识别成一个当前 Agent 无权处理的意图，
        # 用于演示「模型提出越权动作 → 控制层拒绝」的场景。
        if any(kw in text for kw in ("退款", "退钱", "refund")):
            return IntentParseResult(
                intent="issue_refund",
                task_type="refund_request",
                entities={"customer_id": customer_id} if customer_id else {},
                confidence=0.82,
                reasoning_summary="用户表达了退款诉求。",
            )

        if discount_rate is not None:
            entities: dict[str, Any] = {"discount_rate": discount_rate, "notify": wants_notify}
            if customer_id:
                entities["customer_id"] = customer_id
            return IntentParseResult(
                intent="apply_discount",
                task_type="discount_request",
                entities=entities,
                confidence=0.91,
                reasoning_summary=(
                    f"识别为折扣申请，客户 {customer_id or '未指明'}，"
                    f"折扣幅度 {discount_rate:.0%}。"
                ),
                clarification_needed=customer_id is None,
            )

        if customer_id:
            return IntentParseResult(
                intent="query_customer",
                task_type="customer_query",
                entities={"customer_id": customer_id},
                confidence=0.86,
                reasoning_summary="识别为客户信息查询。",
            )

        return IntentParseResult(
            intent="unknown",
            task_type="generic",
            entities={},
            confidence=0.2,
            reasoning_summary="无法从输入中识别出明确的业务意图。",
            clarification_needed=True,
        )

    def _build_plan(self, text: str, context: AgentContext) -> ExecutionPlan:
        """根据意图生成执行计划。

        注意计划中的**步骤顺序**：查询 → 折扣 → 通知。
        通知排在最后不是随手写的——它是**不可撤回**的动作。
        如果通知排在折扣前面，折扣失败时那条已经发出去的短信收不回来。
        「不可补偿的动作排在链路最后」是 Saga 设计的一条硬纪律。
        """
        intent = self._parse_intent(text)
        steps: list[PlannedStep] = []

        if intent.intent == "issue_refund":
            steps.append(
                PlannedStep(
                    step_name="issue_refund",
                    proposal=ActionProposal(
                        intent="issue_refund",
                        # 故意提出一个未注册 / 不在白名单的工具名，
                        # 用于验收场景七：模型提出越权动作，控制层必须拒绝。
                        tool_name="refund_payment",
                        arguments={"customer_id": intent.entities.get("customer_id", "")},
                        reasoning_summary="用户要求退款。",
                        confidence=intent.confidence,
                        expected_result="退款成功",
                        risk_hint=RiskLevel.HIGH,
                    ),
                )
            )
            return ExecutionPlan(
                plan_summary="处理退款请求", steps=steps, confidence=intent.confidence
            )

        customer_id = str(intent.entities.get("customer_id") or "")

        if intent.intent in ("apply_discount", "query_customer") and customer_id:
            steps.append(
                PlannedStep(
                    step_name="query_customer",
                    proposal=ActionProposal(
                        intent="query_customer",
                        tool_name="query_customer",
                        arguments={"customer_id": customer_id},
                        reasoning_summary="先查询客户信息以确认资格与等级。",
                        confidence=0.95,
                        expected_result="返回客户等级与当前折扣状态",
                        risk_hint=RiskLevel.NONE,
                    ),
                    critical=True,
                )
            )

        if intent.intent == "apply_discount" and customer_id:
            rate = float(intent.entities.get("discount_rate") or 0.0)
            steps.append(
                PlannedStep(
                    step_name="apply_discount",
                    proposal=ActionProposal(
                        intent="apply_discount",
                        tool_name="apply_discount",
                        arguments={
                            "customer_id": customer_id,
                            "discount_rate": rate,
                            "reason": "客服代客户申请折扣",
                        },
                        reasoning_summary=f"用户请求为客户 {customer_id} 设置 {rate:.0%} 折扣。",
                        confidence=intent.confidence,
                        expected_result="折扣记录创建成功并生效",
                        # 模型的风险提示仅供参考，真实等级由 RiskPolicy 判定。
                        risk_hint=RiskLevel.MEDIUM if rate > 0.05 else RiskLevel.LOW,
                    ),
                    critical=True,
                    depends_on=["query_customer"],
                )
            )

            if intent.entities.get("notify"):
                steps.append(
                    PlannedStep(
                        step_name="send_notification",
                        proposal=ActionProposal(
                            intent="notify_customer",
                            tool_name="send_notification",
                            arguments={
                                "customer_id": customer_id,
                                "channel": "sms",
                                "template": "discount_applied",
                            },
                            reasoning_summary="用户要求通知客户折扣已生效。",
                            confidence=0.9,
                            expected_result="通知发送成功",
                            risk_hint=RiskLevel.LOW,
                        ),
                        # 关键设计：通知是**非关键步骤**。
                        # 折扣成功但通知失败时，任务应落 PARTIAL_SUCCESS，
                        # 绝不能因为通知失败就去撤销折扣——那是两回事。
                        critical=False,
                        depends_on=["apply_discount"],
                    )
                )

        return ExecutionPlan(
            plan_summary=f"处理 {intent.intent} 请求",
            steps=steps,
            confidence=intent.confidence,
        )

    def _compose_reply(self, context: AgentContext) -> str:
        """根据已完成步骤组装人类可读回复。

        注意这里**只描述状态，不编造数字**。
        真实的金额、折扣率由 Runtime 从状态层取出后套模板填入。
        """
        if not context.recent_steps:
            return "任务已受理，但暂无可汇报的执行结果。"
        lines = ["已处理完成，以下是各步骤结果："]
        for step in context.recent_steps:
            mark = "✅" if step.status == "SUCCESS" else "⚠️"
            lines.append(f"{mark} {step.step_name}：{step.status} {step.summary}".rstrip())
        return "\n".join(lines)


def _last_user_message(messages: list[LLMMessage]) -> str:
    for msg in reversed(messages):
        if msg.role == "user":
            return msg.content
    return ""
