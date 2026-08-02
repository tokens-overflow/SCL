"""身份解析服务。

把「三个 ID」变成「一个可用于判断的 ResolvedIdentity」。

单独抽出来的原因：三方身份的解析在多个地方需要（创建任务时、
每一步执行前、审批时），而每次都手写三行 await 很容易出现
「某处忘了带服务账号」这种不易察觉的漏洞——
一旦漏了服务账号，权限交集就变成了两方交集，边界被悄悄放宽。
"""

from __future__ import annotations

from app.security.identity import (
    AgentIdentity,
    IdentityProvider,
    ResolvedIdentity,
    ServiceIdentity,
    UserIdentity,
    default_identity_provider,
)


class AuthorizationService:
    """身份解析与权限查询。

    Args:
        provider: 身份提供方，缺省用 Mock 实现。
            真实环境替换为 OIDC / LDAP / 内部 IAM，本类代码不用改。
    """

    def __init__(self, provider: IdentityProvider | None = None) -> None:
        self.provider = provider or default_identity_provider

    async def resolve(
        self,
        user_id: str,
        agent_id: str,
        service_id: str | None = None,
    ) -> ResolvedIdentity:
        """解析三方身份。

        Args:
            user_id: 用户 ID。
            agent_id: Agent ID。
            service_id: 工具背后的服务账号 ID。
                **不传就意味着不参与交集**，所以调用方在评估具体工具时
                必须传——这是权限边界完整性的关键。

        Returns:
            :class:`ResolvedIdentity`。
        """
        user: UserIdentity = await self.provider.get_user(user_id)
        agent: AgentIdentity = await self.provider.get_agent(agent_id)
        service: ServiceIdentity | None = None
        if service_id:
            service = await self.provider.get_service(service_id)
        return ResolvedIdentity(user=user, agent=agent, service=service)

    async def with_service(
        self,
        identity: ResolvedIdentity,
        service_id: str,
    ) -> ResolvedIdentity:
        """在已解析的身份上替换服务账号。

        用于同一个任务里逐个工具评估：用户和 Agent 不变，
        但每个工具背后的服务账号不同，交集要按工具重新算。
        """
        service = await self.provider.get_service(service_id)
        return identity.model_copy(update={"service": service})

    async def is_approver(self, user_id: str, approver_role: str) -> bool:
        """判断某个用户是否具备指定审批角色。

        Args:
            user_id: 审批人 ID。
            approver_role: 需要的角色，如 ``cs_manager``。

        Returns:
            是否有权审批。

        Note:
            **审批人不能是发起人**这条规则不在这里判断，
            而在 :class:`~app.control.approval_gate.ApprovalGate` 里——
            那是一条流程规则（四眼原则），不是身份规则。
        """
        user = await self.provider.get_user(user_id)
        return approver_role in user.roles or user.is_admin


#: 默认实例。
default_authorization_service = AuthorizationService()
