"""DataAccessPolicy：数据范围控制。

**RBAC 回答不了「能不能碰这一条数据」。**

「客服有 customer:read 权限」和「这个客服能读**这个**客户」是两个问题。
前者是 RBAC，后者是资源范围（有时叫 ABAC 或行级权限）。
只做 RBAC 的系统会出现这样的漏洞：
一个正常的客服，用一个正常的工具，读到了另一个大区所有客户的数据——
每一步权限校验都通过了，但结果显然是错的。

本策略实现三种范围：

* ``customer:*``       全量（管理员）
* ``customer:department`` 本部门
* ``customer:own``     仅自己名下

判定依据是**从数据库查出来的客户归属**（`business_facts`），
而不是模型说的。事实要从系统里查，不能问模型。
"""

from __future__ import annotations

from app.control.models import PolicyEvaluationRequest, PolicyEvaluationResult
from app.core.enums import RiskLevel


class DataAccessPolicy:
    """基于资源范围的数据访问控制。"""

    name = "DataAccessPolicy"

    async def evaluate(self, request: PolicyEvaluationRequest) -> PolicyEvaluationResult:
        """检查操作人是否有权访问目标数据。

        Returns:
            越界访问时 DENY。

        Note:
            拒绝话术**不区分「不存在」和「无权限」**：
            如果对不存在的客户说「客户不存在」，对存在但无权的客户说「无权访问」，
            攻击者就可以通过枚举来探测哪些客户号是真实存在的。
        """
        args = request.validated_arguments or request.proposal.arguments
        customer_id = args.get("customer_id")
        if not customer_id:
            # 不涉及具体客户资源的动作，本策略不参与判断。
            return PolicyEvaluationResult.allow(self.name)

        user = request.identity.user
        scopes = user.data_scopes

        # 管理员或全量范围：放行，但记录下来。
        # 管理员操作虽然合法，但**仍然要留痕**——高权限操作的审计价值最高。
        if user.is_admin or "customer:*" in scopes:
            return PolicyEvaluationResult.allow(
                self.name,
                metadata={
                    "scope": "all",
                    "customer_id": customer_id,
                    "note": "管理员范围访问，已记录审计",
                },
            )

        facts = request.business_facts
        customer_department = facts.get("customer_department")
        customer_owner = facts.get("customer_owner")

        if customer_department is None and customer_owner is None:
            # 查不到归属信息就无法判断范围。**默认拒绝**——
            # 「查不到就放行」是数据越权最常见的成因。
            return PolicyEvaluationResult.manual_review(
                self.name,
                "DATA_SCOPE_UNVERIFIABLE",
                "无法确认目标数据的归属范围，转人工确认",
                risk_level=RiskLevel.HIGH,
                metadata={"customer_id": customer_id},
            )

        if "customer:department" in scopes:
            if customer_department == user.department:
                return PolicyEvaluationResult.allow(
                    self.name,
                    metadata={"scope": "department", "department": user.department},
                )
            return PolicyEvaluationResult.deny(
                self.name,
                "DATA_SCOPE_VIOLATION",
                "无法访问该客户数据，请确认客户编号或联系相应负责人",
                risk_level=RiskLevel.HIGH,
                metadata={
                    "customer_id": customer_id,
                    "user_department": user.department,
                    "customer_department": customer_department,
                },
            )

        if "customer:own" in scopes:
            if customer_owner == user.user_id:
                return PolicyEvaluationResult.allow(self.name, metadata={"scope": "own"})
            return PolicyEvaluationResult.deny(
                self.name,
                "DATA_SCOPE_VIOLATION",
                "无法访问该客户数据，请确认客户编号或联系相应负责人",
                risk_level=RiskLevel.HIGH,
                metadata={"customer_id": customer_id},
            )

        # 没有声明任何数据范围 = 没有数据访问权。默认拒绝。
        return PolicyEvaluationResult.deny(
            self.name,
            "DATA_SCOPE_NOT_GRANTED",
            "当前操作人未被授予任何客户数据访问范围",
            risk_level=RiskLevel.HIGH,
            metadata={"user_id": user.user_id},
        )
