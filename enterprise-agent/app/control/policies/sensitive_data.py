"""SensitiveDataPolicy：敏感数据出站检查。

这条策略拦的是一类很隐蔽的问题：**动作参数里夹带了不该出现的敏感信息**。

典型场景：模型把用户输入里的手机号、身份证号原样抄进了工具参数，
于是这些信息会随着工具调用被写进日志、审计、下游系统。
每一步单独看都是「正常的数据传递」，合起来就是一次个人信息扩散。

拦截规则刻意保守：

* 参数里出现完整身份证 / 银行卡 → **DENY**。这两类信息在业务参数里
  几乎不可能有正当理由出现，而泄漏代价极高。
* 参数里出现手机号 / 邮箱 → 放行但标记。业务上确实可能需要
  （比如更新联系方式），所以不拦，但要在审计里留痕。
* 自由文本字段超长 → 标记。超长自由文本是夹带内容的常见载体。
"""

from __future__ import annotations

import re
from typing import Any

from app.control.models import PolicyEvaluationRequest, PolicyEvaluationResult
from app.core.enums import RiskLevel

_ID_CARD = re.compile(r"(?<!\d)\d{6}(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)")
_BANK_CARD = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

#: 自由文本字段的长度上限。超过就标记为可疑。
MAX_FREE_TEXT_LENGTH = 500


class SensitiveDataPolicy:
    """检查动作参数中的敏感信息。"""

    name = "SensitiveDataPolicy"

    async def evaluate(self, request: PolicyEvaluationRequest) -> PolicyEvaluationResult:
        """扫描参数中的敏感信息。

        Returns:
            发现证件号 / 银行卡号时 DENY；
            发现手机号 / 邮箱时 ALLOW 但在 metadata 中标记，供审计与后续脱敏使用。
        """
        args = request.validated_arguments or request.proposal.arguments
        findings: dict[str, list[str]] = {}

        for field, value in _walk(args):
            if not isinstance(value, str):
                continue
            if _ID_CARD.search(value):
                findings.setdefault("id_card", []).append(field)
            if _BANK_CARD.search(value):
                findings.setdefault("bank_card", []).append(field)
            if _PHONE.search(value):
                findings.setdefault("phone", []).append(field)
            if _EMAIL.search(value):
                findings.setdefault("email", []).append(field)
            if len(value) > MAX_FREE_TEXT_LENGTH:
                findings.setdefault("oversized_text", []).append(field)

        # 硬拦截：证件号和银行卡号不应该出现在业务动作参数里。
        blocking = {k: v for k, v in findings.items() if k in ("id_card", "bank_card")}
        if blocking:
            return PolicyEvaluationResult.deny(
                self.name,
                "SENSITIVE_DATA_IN_ARGUMENTS",
                "动作参数中包含证件号或银行卡号，出于合规要求拒绝执行",
                risk_level=RiskLevel.CRITICAL,
                metadata={
                    # 只记录**字段名**，绝不把命中的原文写进审计——
                    # 那等于把泄漏从一个地方复制到另一个地方。
                    "blocking_fields": {k: sorted(set(v)) for k, v in blocking.items()},
                },
            )

        if findings:
            return PolicyEvaluationResult.allow(
                self.name,
                risk_level=RiskLevel.MEDIUM,
                metadata={
                    "pii_fields": {k: sorted(set(v)) for k, v in findings.items()},
                    "note": "参数含个人信息，写入审计与日志前必须脱敏",
                },
            )

        return PolicyEvaluationResult.allow(self.name)


def _walk(node: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """把嵌套结构展平成 ``(字段路径, 值)`` 列表。"""
    out: list[tuple[str, Any]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            out.extend(_walk(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            out.extend(_walk(v, f"{prefix}[{i}]"))
    else:
        out.append((prefix or "(root)", node))
    return out
