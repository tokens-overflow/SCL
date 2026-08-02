"""身份与权限模型。

**核心设计：有效权限是三者的交集，不是并集。**

    有效权限 = 用户允许做的事 ∩ Agent 被授权做的事 ∩ 工具服务账号允许做的事

为什么必须是交集：

* 只看用户权限 → 管理员随口一句话，Agent 就能做任何管理员能做的事。
  但我们授权给 Agent 的，通常只是管理员权限中很小的一个子集。
* 只看 Agent 权限 → 普通客服借 Agent 的手越权，Agent 成了提权工具。
* 不看服务账号 → Agent 有权限、用户也有权限，但底层服务账号根本连不上那个系统，
  于是失败发生在最深的地方，错误信息也最难解释。

三者取交集之后，任何一方收紧都能立刻生效，这是最小权限原则的自然落地。

另外注意：这里实现的是 **Mock Identity Provider**。真实环境应替换为 OIDC / LDAP /
内部 IAM，但 `IdentityProvider` 这个接口不用变——这正是把身份查询抽象成接口的意义。
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import PermissionDeniedError


class UserIdentity(BaseModel):
    """用户身份。

    Attributes:
        user_id: 用户唯一标识。
        display_name: 展示名。
        roles: RBAC 角色集合。
        permissions: 直接授予的权限（通常由角色展开而来）。
        data_scopes: 基于资源范围的权限。例如 ``{"customer:own"}`` 表示只能访问
            自己名下的客户；``{"customer:*"}`` 表示全量。**RBAC 只回答「能不能做这类事」，
            回答不了「能不能碰这一条数据」，所以必须有独立的数据范围维度。**
        is_admin: 是否管理员。
        department: 所属部门，用于数据范围判定。
    """

    model_config = ConfigDict(frozen=True)

    user_id: str
    display_name: str = ""
    roles: frozenset[str] = Field(default_factory=frozenset)
    permissions: frozenset[str] = Field(default_factory=frozenset)
    data_scopes: frozenset[str] = Field(default_factory=frozenset)
    is_admin: bool = False
    department: str = "default"


class AgentIdentity(BaseModel):
    """Agent 身份。

    Agent 是一个**独立的主体**，不是用户的影子。它有自己的权限边界和工具白名单。

    Attributes:
        agent_id: Agent 唯一标识。
        display_name: 展示名。
        permissions: Agent 被授权的权限集合。
        allowed_tools: 工具白名单。**不在白名单里的工具，即使已注册、
            即使用户有权限，也不允许这个 Agent 调用**——这是防「模型偷偷换一个
            高权限工具」的关键一道闸。
        max_risk_level: 该 Agent 允许自主触达的最高风险等级（字符串形式，
            由 RiskPolicy 解释）。
        description: 用途说明，会出现在给 LLM 的上下文里。
    """

    model_config = ConfigDict(frozen=True)

    agent_id: str
    display_name: str = ""
    permissions: frozenset[str] = Field(default_factory=frozenset)
    allowed_tools: frozenset[str] = Field(default_factory=frozenset)
    max_risk_level: str = "MEDIUM"
    description: str = ""


class ServiceIdentity(BaseModel):
    """工具 / 服务账号身份。

    每个工具背后都有一个真实的服务账号（数据库账号、下游 API 的 client）。
    它的权限是**独立于用户和 Agent** 的第三个维度：即使用户和 Agent 都同意，
    服务账号连不上、或者只读，这个动作就是做不了的。

    Attributes:
        service_id: 服务账号标识。
        permissions: 服务账号被授予的权限。
        read_only: 是否只读账号。只读账号绝不允许执行写工具。
    """

    model_config = ConfigDict(frozen=True)

    service_id: str
    permissions: frozenset[str] = Field(default_factory=frozenset)
    read_only: bool = False


class ResolvedIdentity(BaseModel):
    """一次调用中三种身份的组合视图。

    这是控制层真正拿来做判断的对象。它把「三者交集」这个规则固化成一个方法，
    避免各个策略各写一份、写歪一份。
    """

    model_config = ConfigDict(frozen=True)

    user: UserIdentity
    agent: AgentIdentity
    service: ServiceIdentity | None = None

    def effective_permissions(self) -> frozenset[str]:
        """计算有效权限 = 用户 ∩ Agent ∩ 服务账号。

        Returns:
            三者的交集。若未指定服务账号（例如纯计算步骤），只取用户 ∩ Agent。

        Note:
            管理员在这里**不享受特权**：`is_admin` 只在数据范围策略里放宽资源边界，
            不会让他绕过 Agent 的工具白名单。理由很简单——
            我们授权给 Agent 的只是管理员权限的一个子集，
            不能因为「操作人是管理员」就把 Agent 的边界一起放开。
        """
        effective = self.user.permissions & self.agent.permissions
        if self.service is not None:
            effective = effective & self.service.permissions
        return frozenset(effective)

    def missing_permissions(self, required: frozenset[str] | set[str]) -> frozenset[str]:
        """返回缺失的权限集合。

        Args:
            required: 目标动作要求的权限。

        Returns:
            `required` 中不在有效权限里的部分。空集表示权限足够。
        """
        return frozenset(set(required) - self.effective_permissions())

    def require(self, required: frozenset[str] | set[str]) -> None:
        """断言权限足够，不足则抛异常。

        Raises:
            PermissionDeniedError: 权限不足。异常 details 里带上缺失项，
                写进审计供事后排查；**对外返回的话术要统一**，不要泄漏内部权限名。
        """
        missing = self.missing_permissions(required)
        if missing:
            raise PermissionDeniedError(
                "有效权限不足（用户 ∩ Agent ∩ 服务账号）",
                details={
                    "missing_permissions": sorted(missing),
                    "user_id": self.user.user_id,
                    "agent_id": self.agent.agent_id,
                },
            )

    def summary_for_llm(self) -> dict[str, object]:
        """生成给 LLM 看的身份摘要。

        **只给角色和可用工具，不给完整权限列表。** 原因有两个：
        1. 模型不需要靠权限列表做判断——权限判断是控制层的事；
        2. 权限列表本身是内部结构信息，喂给模型等于扩大了泄漏面。
        """
        return {
            "user_role": sorted(self.user.roles) or ["user"],
            "user_department": self.user.department,
            "agent_id": self.agent.agent_id,
            "agent_description": self.agent.description,
            "allowed_tool_count": len(self.agent.allowed_tools),
        }


class IdentityProvider(Protocol):
    """身份提供方接口。

    真实环境接 OIDC / LDAP / 内部 IAM；Demo 用 :class:`MockIdentityProvider`。
    接口保持稳定，替换实现时业务代码零改动。
    """

    async def get_user(self, user_id: str) -> UserIdentity:
        """按 ID 获取用户身份。"""
        ...

    async def get_agent(self, agent_id: str) -> AgentIdentity:
        """按 ID 获取 Agent 身份。"""
        ...

    async def get_service(self, service_id: str) -> ServiceIdentity:
        """按 ID 获取服务账号身份。"""
        ...


# --------------------------------------------------------------------------------------
# 演示用的权限常量。集中定义，避免各处硬编码字符串拼写不一致。
# --------------------------------------------------------------------------------------
PERM_CUSTOMER_READ = "customer:read"
PERM_DISCOUNT_APPLY = "discount:apply"
PERM_DISCOUNT_APPLY_HIGH = "discount:apply:high"   # 大额折扣，仅经理及以上
PERM_DISCOUNT_REVOKE = "discount:revoke"
PERM_NOTIFICATION_SEND = "notification:send"
PERM_REFUND_ISSUE = "refund:issue"                  # 故意不授予客服，用于演示「权限不足」场景


class MockIdentityProvider:
    """内存版身份提供方，用于 Demo 与测试。

    预置了三种典型身份，正好覆盖验收场景：

    * ``agent_user_001`` 普通客服：能查客户、能打折、能发通知，**不能**退款。
    * ``manager_001`` 经理：多一个 ``discount:apply:high``，可以审批大额折扣。
    * ``admin_001`` 管理员：全量数据范围。
    """

    def __init__(self) -> None:
        self._users: dict[str, UserIdentity] = {
            "user_001": UserIdentity(
                user_id="user_001",
                display_name="普通客服 · 小张",
                roles=frozenset({"cs_agent"}),
                permissions=frozenset(
                    {
                        PERM_CUSTOMER_READ,
                        PERM_DISCOUNT_APPLY,
                        PERM_NOTIFICATION_SEND,
                    }
                ),
                # 只能访问自己部门的客户：RBAC 之外的资源范围维度。
                data_scopes=frozenset({"customer:department"}),
                department="cs_north",
            ),
            "manager_001": UserIdentity(
                user_id="manager_001",
                display_name="客服经理 · 老李",
                roles=frozenset({"cs_agent", "cs_manager"}),
                permissions=frozenset(
                    {
                        PERM_CUSTOMER_READ,
                        PERM_DISCOUNT_APPLY,
                        PERM_DISCOUNT_APPLY_HIGH,
                        PERM_DISCOUNT_REVOKE,
                        PERM_NOTIFICATION_SEND,
                    }
                ),
                data_scopes=frozenset({"customer:department"}),
                department="cs_north",
            ),
            "admin_001": UserIdentity(
                user_id="admin_001",
                display_name="系统管理员",
                roles=frozenset({"admin"}),
                permissions=frozenset(
                    {
                        PERM_CUSTOMER_READ,
                        PERM_DISCOUNT_APPLY,
                        PERM_DISCOUNT_APPLY_HIGH,
                        PERM_DISCOUNT_REVOKE,
                        PERM_NOTIFICATION_SEND,
                        PERM_REFUND_ISSUE,
                    }
                ),
                data_scopes=frozenset({"customer:*"}),
                is_admin=True,
                department="hq",
            ),
        }

        self._agents: dict[str, AgentIdentity] = {
            "discount_agent": AgentIdentity(
                agent_id="discount_agent",
                display_name="客户折扣 Agent",
                permissions=frozenset(
                    {
                        PERM_CUSTOMER_READ,
                        PERM_DISCOUNT_APPLY,
                        PERM_DISCOUNT_APPLY_HIGH,
                        PERM_NOTIFICATION_SEND,
                    }
                ),
                # 注意这里**没有** refund_payment：
                # 即使管理员本人有退款权限，这个 Agent 也不能退款。
                allowed_tools=frozenset(
                    {"query_customer", "apply_discount", "send_notification"}
                ),
                max_risk_level="HIGH",
                description="处理客户折扣申请，可查询客户、发放折扣、发送通知。不处理退款。",
            ),
            "readonly_agent": AgentIdentity(
                agent_id="readonly_agent",
                display_name="只读查询 Agent",
                permissions=frozenset({PERM_CUSTOMER_READ}),
                allowed_tools=frozenset({"query_customer"}),
                max_risk_level="LOW",
                description="只读 Agent，仅用于查询客户信息。",
            ),
        }

        self._services: dict[str, ServiceIdentity] = {
            "crm_service": ServiceIdentity(
                service_id="crm_service",
                permissions=frozenset({PERM_CUSTOMER_READ}),
                read_only=True,
            ),
            "billing_service": ServiceIdentity(
                service_id="billing_service",
                permissions=frozenset(
                    {PERM_DISCOUNT_APPLY, PERM_DISCOUNT_APPLY_HIGH, PERM_DISCOUNT_REVOKE}
                ),
            ),
            "notification_service": ServiceIdentity(
                service_id="notification_service",
                permissions=frozenset({PERM_NOTIFICATION_SEND}),
            ),
        }

    async def get_user(self, user_id: str) -> UserIdentity:
        """按 ID 获取用户身份，未知用户降级为「零权限用户」。

        为什么未知用户不抛异常而是返回零权限：这样控制层会以「权限不足」
        统一拒绝，路径唯一、审计一致，避免出现「用户不存在」和「无权限」
        两种不同话术泄漏用户是否存在。
        """
        return self._users.get(
            user_id,
            UserIdentity(user_id=user_id, display_name="未知用户", roles=frozenset()),
        )

    async def get_agent(self, agent_id: str) -> AgentIdentity:
        """按 ID 获取 Agent 身份，未知 Agent 降级为「零权限 Agent」。"""
        return self._agents.get(
            agent_id,
            AgentIdentity(agent_id=agent_id, display_name="未知 Agent", max_risk_level="NONE"),
        )

    async def get_service(self, service_id: str) -> ServiceIdentity:
        """按 ID 获取服务账号身份，未知服务账号降级为「零权限只读账号」。"""
        return self._services.get(
            service_id, ServiceIdentity(service_id=service_id, read_only=True)
        )


#: 进程级默认身份提供方。生产环境应通过依赖注入替换。
default_identity_provider = MockIdentityProvider()
