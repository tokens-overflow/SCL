"""LangGraph 适配示例（**可选**，不影响核心工程运行）。

═══════════════════════════════════════════════════════════════════════════
**先说清楚这个文件的定位：**

核心工程**不依赖** LangGraph。整套编排、状态机、控制层和工具调用机制
都是自己实现的清晰 Python 代码——这样读者能看清底层原理，
也不会被某个框架的抽象和版本变更绑架。

这个文件只是展示：**如果你的团队已经在用 LangGraph，怎么把它接进来
而不牺牲控制层、幂等、审计这些企业级要素。**

没装 langgraph 时，导入本模块不会报错，`build_graph()` 会给出清晰提示。
═══════════════════════════════════════════════════════════════════════════

**关键设计：LangGraph 只负责「怎么走」，不负责「能不能做」。**

很多 LangGraph 示例把工具直接绑到模型上（`llm.bind_tools([...])`），
让模型决定调什么、然后由框架自动执行。那条路在企业场景里是走不通的——
它等于把「允不允许执行」这个问题交给了模型。

正确的接法是：把 LangGraph 的节点做成**薄封装**，
每个会产生副作用的节点内部仍然走
`PolicyEngine → ActionExecutor` 这条路。
LangGraph 得到的是流程编排能力，我们保留了所有护栏。

另外，即使用了 LangGraph 的 checkpointer，
**状态的权威来源仍然是我们自己的步骤表**——
因为对账需要幂等键和 external_reference_id，
而这些是业务语义，不是通用编排框架会替你保存的东西。
"""

from __future__ import annotations

from typing import Any, TypedDict

from app.control.models import PolicyDecision
from app.core.enums import DecisionType
from app.operations.logging import get_logger

logger = get_logger(__name__)

try:  # pragma: no cover - 可选依赖
    from langgraph.graph import END, StateGraph

    HAS_LANGGRAPH = True
except ImportError:  # pragma: no cover
    StateGraph = None  # type: ignore[assignment,misc]
    END = "__end__"  # type: ignore[assignment]
    HAS_LANGGRAPH = False


class DiscountGraphState(TypedDict, total=False):
    """LangGraph 的状态字典。

    注意它只承载**流程编排需要的最小信息**。
    真正的任务状态（步骤、幂等键、重试次数、外部凭证）
    仍然在数据库的 `task_steps` 表里——
    那才是断点续跑和对账的依据。
    """

    task_id: str
    user_id: str
    agent_id: str
    message: str
    trace_id: str
    customer_id: str
    discount_rate: float
    decision: str          # ALLOW / DENY / REQUIRE_APPROVAL / MANUAL_REVIEW
    reason: str
    result: dict[str, Any]
    outcome: str


def build_graph(orchestrator: Any) -> Any:
    """构造一个演示用的 LangGraph 流程图。

    Args:
        orchestrator: 已装配好的 :class:`~app.runtime.orchestrator.Orchestrator`。
            **所有副作用都通过它发生**，LangGraph 节点自己不碰工具。

    Returns:
        编译后的 LangGraph 应用。

    Raises:
        RuntimeError: 未安装 langgraph。这是**可选**能力，
            核心工程不装它也能完整运行。

    图结构::

        parse_intent → policy_gate ─┬─ ALLOW            → execute → finalize → END
                                    ├─ REQUIRE_APPROVAL → wait_approval      → END
                                    ├─ DENY             → reject             → END
                                    └─ MANUAL_REVIEW    → escalate           → END

    注意 `policy_gate` 是一个**独立节点**，不是模型的一次工具选择。
    这就是把控制层显式建模进流程图的样子。
    """
    if not HAS_LANGGRAPH:
        raise RuntimeError(
            "本示例需要 langgraph（可选依赖）：pip install langgraph。\n"
            "核心工程不依赖它——不装也能完整运行全部功能与测试。"
        )

    async def parse_intent(state: DiscountGraphState) -> DiscountGraphState:
        """认知节点：调用 LLM 解析意图。

        这一步是 LangGraph 里**唯一**该出现模型的地方之一。
        """
        from app.llm.mock_provider import parse_customer_id, parse_discount_rate

        message = state["message"]
        return {
            **state,
            "customer_id": parse_customer_id(message) or "",
            "discount_rate": parse_discount_rate(message) or 0.0,
        }

    async def policy_gate(state: DiscountGraphState) -> DiscountGraphState:
        """控制层节点。

        **这个节点的存在本身就是重点**：在 LangGraph 的图里，
        「能不能做」是一个显式的、必经的节点，
        而不是隐藏在 `llm.bind_tools()` 背后的自动行为。
        """
        from app.cognitive.models import ActionProposal
        from app.core.enums import RiskLevel

        proposal = ActionProposal(
            intent="apply_discount",
            tool_name="apply_discount",
            arguments={
                "customer_id": state.get("customer_id", ""),
                "discount_rate": state.get("discount_rate", 0.0),
                "reason": "LangGraph 流程发起",
            },
            confidence=0.9,
            risk_hint=RiskLevel.MEDIUM,
        )
        # 复用同一套 PolicyEngine——控制层不因为换了编排框架而改变。
        from app.runtime.models import TaskStep

        step = TaskStep(
            step_id="lg_step", task_id=state["task_id"], step_name="apply_discount"
        )
        decision: PolicyDecision = await orchestrator._evaluate(  # noqa: SLF001 - 示例代码
            await orchestrator.task_repo.require_task(state["task_id"]), step, proposal
        )
        return {
            **state,
            "decision": str(decision.decision),
            "reason": decision.human_readable_reason,
        }

    def route(state: DiscountGraphState) -> str:
        """条件分支：按控制层裁决决定下一个节点。"""
        return {
            str(DecisionType.ALLOW): "execute",
            str(DecisionType.REQUIRE_APPROVAL): "wait_approval",
            str(DecisionType.DENY): "reject",
            str(DecisionType.MANUAL_REVIEW): "escalate",
        }.get(state.get("decision", ""), "escalate")

    async def execute(state: DiscountGraphState) -> DiscountGraphState:
        """行动节点：**仍然走 Orchestrator**，享受幂等 / 重试 / 对账 / 审计。"""
        task = await orchestrator.resume_task(state["task_id"])
        return {**state, "outcome": str(task.status), "result": task.result_payload or {}}

    async def wait_approval(state: DiscountGraphState) -> DiscountGraphState:
        """审批挂起节点。

        图在这里**结束本次执行**，等审批回调后重新进入。
        这和崩溃恢复是同一件事：把任务冻在某一步，等外部条件满足再继续。
        """
        return {**state, "outcome": "WAITING_APPROVAL"}

    async def reject(state: DiscountGraphState) -> DiscountGraphState:
        """拒绝节点。"""
        return {**state, "outcome": "FAILED"}

    async def escalate(state: DiscountGraphState) -> DiscountGraphState:
        """转人工节点。"""
        return {**state, "outcome": "MANUAL_REVIEW"}

    async def finalize(state: DiscountGraphState) -> DiscountGraphState:
        """收尾节点。"""
        return state

    graph = StateGraph(DiscountGraphState)
    graph.add_node("parse_intent", parse_intent)
    graph.add_node("policy_gate", policy_gate)
    graph.add_node("execute", execute)
    graph.add_node("wait_approval", wait_approval)
    graph.add_node("reject", reject)
    graph.add_node("escalate", escalate)
    graph.add_node("finalize", finalize)

    graph.set_entry_point("parse_intent")
    graph.add_edge("parse_intent", "policy_gate")
    graph.add_conditional_edges(
        "policy_gate",
        route,
        {
            "execute": "execute",
            "wait_approval": "wait_approval",
            "reject": "reject",
            "escalate": "escalate",
        },
    )
    graph.add_edge("execute", "finalize")
    graph.add_edge("finalize", END)
    graph.add_edge("wait_approval", END)
    graph.add_edge("reject", END)
    graph.add_edge("escalate", END)

    logger.info("langgraph_demo_graph_built", node_count=7)
    return graph.compile()


#: 给读者的对照说明：本项目自研实现与 LangGraph 的职责对应关系。
#: 看这张表最重要的一个结论是：**控制层没有对应项**——
#: 通用编排框架不会替你做权限、业务规则和审批判断，那必须自己写。
EQUIVALENCE_NOTES = """
| 本项目                          | LangGraph 对应物        | 说明                                   |
|--------------------------------|------------------------|----------------------------------------|
| Orchestrator._drive()          | StateGraph 执行循环     | 流程推进                                |
| TaskStateMachine               | 节点与边                | 我们的迁移表是显式断言，图的边是隐式约束 |
| task_steps 表                  | Checkpointer           | **不能互相替代**：对账要幂等键和外部凭证 |
| PolicyEngine                   | （无对应物）            | 权限/业务规则/审批必须自己实现          |
| ActionExecutor（幂等+对账）     | （无对应物）            | 框架不会替你做幂等和外部状态对账         |
| RecoveryService                | 部分对应 Checkpointer   | 但「悬挂步骤对账」仍需自己写            |
"""
