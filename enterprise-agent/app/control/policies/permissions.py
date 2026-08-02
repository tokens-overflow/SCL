"""权限相关策略。

两条策略，回答两个不同的问题：

* :class:`AgentPermissionPolicy`：**这个 Agent 允许碰这个工具吗？**
  （工具白名单 + Agent 风险上限）
* :class:`ToolPermissionPolicy`：**三方权限交集够不够？**
  （用户 ∩ Agent ∩ 服务账号）

必须分开的原因：它们的失败含义完全不同。

前者失败意味着「模型试图使用一个它根本不该知道的工具」——
这通常是模型幻觉或提示词注入的信号，值得单独告警。
后者失败意味着「这个人今天做不了这件事」——这是正常的业务边界，
应该给用户一个可操作的回复（比如「请联系经理」）。

如果合并成一条，这两种情况在审计里就分不开了。

**为什么权限不能只写在 Prompt 里？**

Prompt 是软约束。模型会忘、会被绕过、换个模型行为就变了，而你没有兜底。
更根本的是：Prompt 里的规则是**给模型看的建议**，
而权限判断需要的是**不可绕过的执行**。这两者不是强弱之分，是性质不同。
真正的红线必须落在这样的代码里——即使模型被彻底说服，这段代码照样拒绝。
"""

from __future__ import annotations

from app.control.models import PolicyEvaluationRequest, PolicyEvaluationResult
from app.core.enums import RiskLevel


class AgentPermissionPolicy:
    """Agent 层面的能力边界：工具白名单 + 风险上限。"""

    name = "AgentPermissionPolicy"

    async def evaluate(self, request: PolicyEvaluationRequest) -> PolicyEvaluationResult:
        """检查 Agent 是否被授权使用这个工具。

        Returns:
            工具未注册 / 不在白名单 / 超出 Agent 风险上限时 DENY。

        Note:
            **未注册工具的拒绝发生在这里，而不是等到执行时。**
            让它在控制层就被挡住，好处是这次拒绝会进入正常的
            PolicyDecision 审计流程，而不是变成一个异常堆栈。
        """
        agent = request.identity.agent

        # 第一道：工具必须已注册。模型幻觉出来的工具名在这里终结。
        if not request.tool_registered:
            return PolicyEvaluationResult.deny(
                self.name,
                "TOOL_NOT_REGISTERED",
                f"工具 {request.tool_name} 未注册，拒绝执行",
                risk_level=RiskLevel.HIGH,
                metadata={"tool_name": request.tool_name, "signal": "possible_hallucination"},
            )

        # 第二道：必须在这个 Agent 的白名单里。
        # 注意这一条**不看用户权限**：即使操作人是管理员，
        # 也不能让一个「只读查询 Agent」去执行折扣发放。
        # 我们授权给 Agent 的，只是用户权限中很小的一个子集。
        if request.tool_name not in agent.allowed_tools:
            return PolicyEvaluationResult.deny(
                self.name,
                "TOOL_NOT_IN_AGENT_WHITELIST",
                f"Agent {agent.agent_id} 未被授权使用工具 {request.tool_name}",
                risk_level=RiskLevel.HIGH,
                metadata={
                    "tool_name": request.tool_name,
                    "allowed_tools": sorted(agent.allowed_tools),
                },
            )

        # 第三道：工具风险不能超过 Agent 的风险上限。
        # 这让「给某个 Agent 降权」变成一次配置修改，而不是逐个工具去摘白名单。
        try:
            agent_max = RiskLevel(agent.max_risk_level)
        except ValueError:
            agent_max = RiskLevel.NONE
        if request.tool_risk_level.order > agent_max.order:
            return PolicyEvaluationResult.deny(
                self.name,
                "TOOL_RISK_EXCEEDS_AGENT_LIMIT",
                (
                    f"工具风险等级 {request.tool_risk_level} 超过 Agent 允许的上限 "
                    f"{agent_max}"
                ),
                risk_level=request.tool_risk_level,
            )

        return PolicyEvaluationResult.allow(self.name, risk_level=request.tool_risk_level)


class ToolPermissionPolicy:
    """三方权限交集检查：用户 ∩ Agent ∩ 服务账号。"""

    name = "ToolPermissionPolicy"

    async def evaluate(self, request: PolicyEvaluationRequest) -> PolicyEvaluationResult:
        """检查有效权限是否覆盖工具要求。

        Returns:
            权限不足时 DENY，并在 `missing_permissions` 里列出缺失项
            （**只写进审计，不返回给终端用户**——
            内部权限名对用户没有意义，还扩大了信息泄漏面）。
        """
        required = set(request.tool_required_permissions)
        if not required:
            return PolicyEvaluationResult.allow(self.name)

        identity = request.identity
        missing = identity.missing_permissions(required)

        if missing:
            return PolicyEvaluationResult.deny(
                self.name,
                "PERMISSION_INSUFFICIENT",
                # 给用户的话术保持统一且可操作，不暴露具体缺哪个权限点。
                "当前操作人权限不足以执行该动作，请联系具备相应权限的同事处理",
                required_permissions=required,
                missing_permissions=missing,
                risk_level=RiskLevel.HIGH,
                metadata={
                    "user_permissions": sorted(identity.user.permissions),
                    "agent_permissions": sorted(identity.agent.permissions),
                    "service_permissions": sorted(
                        identity.service.permissions if identity.service else []
                    ),
                    # 记录交集结果，事后能一眼看出是哪一方卡住的。
                    "effective_permissions": sorted(identity.effective_permissions()),
                },
            )

        return PolicyEvaluationResult.allow(
            self.name, required_permissions=required
        )
