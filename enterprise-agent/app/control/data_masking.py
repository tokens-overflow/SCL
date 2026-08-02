"""数据脱敏与代号化。

**为什么敏感信息必须在调用模型前脱敏？**

因为一旦数据离开企业内网进入模型供应商，你就失去了对它的控制权：
它可能被记录、被缓存、被用于分析，甚至在某些配置下被用于训练。
「事后删除」在这种场景下是没有意义的承诺。所以纪律是——
**能不出去的，就不要出去。**

本模块提供两类能力，用途完全不同，不要混用：

1. **遮盖（masking）**：`138****5678`。不可逆，给人看的。
   用于日志、审计、给客服看的界面。

2. **代号化（tokenization）**：`PERSON_8F29A1`。可逆，但**只能通过映射表还原**。
   用于送进模型的上下文。模型看到的是代号，返回的也是代号，
   由内部程序按权限还原。

关于「不可逆」的三点澄清（这是最容易被搞混的地方）：

* **哈希通常不可逆**：`sha256(手机号)` 无法还原成手机号。
  但它也**不是好的脱敏方案**——手机号空间只有约 10^11，
  彩虹表几分钟就能穷举。哈希在这里只用来做「同值同码」的索引，不作为保护手段。
* **可恢复代号依赖映射表**：`PERSON_8F29A1` 能还原，
  靠的不是什么「反哈希」，而是数据库里存着一行 `PERSON_8F29A1 → 13812345678`。
* **模型不应该接触映射表**：映射表是企业内部安全边界内的资产。
  代号化的全部安全价值，都建立在「模型拿不到映射表」这一点上。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.state.models import TokenMappingORM

# --------------------------------------------------------------------------------------
# 一、遮盖（不可逆，给人看）
# --------------------------------------------------------------------------------------
_EMAIL_RE = re.compile(r"([\w.+-]+)@([\w-]+\.[\w.-]+)")
_PHONE_CN_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_ID_CARD_RE = re.compile(r"(?<!\d)(\d{6})(\d{8})(\d{3}[\dXx])(?!\d)")
_BANK_CARD_RE = re.compile(r"(?<!\d)(\d{4})(\d{8,11})(\d{4})(?!\d)")


def mask_email(value: str) -> str:
    """遮盖邮箱：``zhangsan@example.com`` → ``z***n@example.com``。

    保留首尾字符和完整域名：足以让人确认「是不是这个人」，
    但不足以直接拿去发垃圾邮件。
    """

    def _repl(m: re.Match[str]) -> str:
        local, domain = m.group(1), m.group(2)
        if len(local) <= 2:
            masked = local[0] + "*"
        else:
            masked = f"{local[0]}{'*' * min(len(local) - 2, 5)}{local[-1]}"
        return f"{masked}@{domain}"

    return _EMAIL_RE.sub(_repl, value)


def mask_phone(value: str) -> str:
    """遮盖手机号：``13812345678`` → ``138****5678``。"""
    return _PHONE_CN_RE.sub(lambda m: f"{m.group(1)[:3]}****{m.group(1)[-4:]}", value)


def mask_id_card(value: str) -> str:
    """遮盖身份证号：``110101199001011234`` → ``110101********1234``。

    保留前 6 位（地区码）和后 4 位：地区码对业务分析有用，
    中间的出生日期是最敏感的部分，必须遮掉。
    """
    return _ID_CARD_RE.sub(lambda m: f"{m.group(1)}{'*' * 8}{m.group(3)[-4:]}", value)


def mask_bank_card(value: str) -> str:
    """遮盖银行卡号：``6222021234567890123`` → ``6222***********0123``。

    **日志里绝对不允许出现完整卡号**，这是 PCI-DSS 的硬要求，
    也是本项目日志规范里明确列出的禁止项之一。
    """
    return _BANK_CARD_RE.sub(
        lambda m: f"{m.group(1)}{'*' * len(m.group(2))}{m.group(3)}", value
    )


def mask_text(value: str) -> str:
    """对一段自由文本做全类型遮盖。

    顺序有讲究：先长后短。身份证（18 位）和银行卡（16~19 位）都是长数字串，
    如果先跑手机号规则，可能会把卡号中间的 11 位误当成手机号。
    """
    if not value:
        return value
    out = mask_id_card(value)
    out = mask_bank_card(out)
    out = mask_phone(out)
    out = mask_email(out)
    return out


def mask_payload(payload: Any) -> Any:
    """递归遮盖结构中的个人信息，返回新对象。

    用于写审计和日志前的统一处理。

    Note:
        这个函数处理的是**个人信息**；密钥类信息由
        :func:`app.security.secrets.redact` 处理。
        两者目的不同（防个人信息外泄 vs 防密钥泄露），
        刻意分开实现，各自的规则也可以独立演进。
    """
    if isinstance(payload, dict):
        return {k: mask_payload(v) for k, v in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [mask_payload(v) for v in payload]
    if isinstance(payload, str):
        return mask_text(payload)
    return payload


# --------------------------------------------------------------------------------------
# 二、代号化（可逆，但只能通过映射表；给模型看）
# --------------------------------------------------------------------------------------
def _value_hash(entity_type: str, value: str) -> str:
    """计算值指纹，用于「同一个值永远得到同一个代号」。

    加上 entity_type 作为盐的一部分，避免不同类型的相同字符串
    （比如某个客户号恰好等于某个订单号）共用一个代号。
    """
    return hashlib.sha256(f"{entity_type}:{value}".encode()).hexdigest()


def _token_from_hash(entity_type: str, value_hash: str) -> str:
    """由指纹生成人类可读的代号，例如 ``PERSON_8F29A1``。"""
    return f"{entity_type.upper()}_{value_hash[:6].upper()}"


class Tokenizer:
    """代号化服务。

    Warning:
        映射表（:class:`~app.state.models.TokenMappingORM`）
        **必须留在企业内部安全边界内**。
        它绝不能出现在给模型的上下文里，也不应该出现在任何对外接口的响应里。
        代号化的全部安全价值都建立在这一点上。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def tokenize(self, entity_type: str, value: str) -> str:
        """把敏感值转成代号。

        Args:
            entity_type: 实体类型，如 ``"person"`` / ``"account"`` / ``"phone"``。
            value: 原始值。

        Returns:
            形如 ``PERSON_8F29A1`` 的代号。**同一个值永远得到同一个代号**——
            否则模型会把同一个人在两轮对话里当成两个人。
        """
        if not value:
            return ""
        vhash = _value_hash(entity_type, value)
        token = _token_from_hash(entity_type, vhash)

        existing = await self.session.execute(
            select(TokenMappingORM)
            .where(TokenMappingORM.entity_type == entity_type)
            .where(TokenMappingORM.value_hash == vhash)
        )
        row = existing.scalars().first()
        if row is not None:
            return row.token

        mapping = TokenMappingORM(
            token=token,
            entity_type=entity_type,
            original_value=value,
            value_hash=vhash,
        )
        self.session.add(mapping)
        try:
            await self.session.flush()
        except IntegrityError:  # pragma: no cover - 并发下的兜底
            await self.session.rollback()
        return token

    async def detokenize(self, token: str, *, allowed: bool) -> str | None:
        """把代号还原为原始值。

        Args:
            token: 代号。
            allowed: 调用方是否有权还原。**这个参数必须由控制层计算后传入**，
                不能在这个方法内部自行判断——权限判定的上下文（用户、Agent、
                数据范围）不属于脱敏组件的职责。

        Returns:
            原始值；无权或代号不存在时返回 ``None``。

        Note:
            这**不是**「反哈希」。哈希本身不可逆；
            这里能还原完全是因为映射表里存着原值。
            如果映射表被删除，这些代号就永远无法还原了——
            这也是代号化相对哈希的代价：它引入了一个必须被保护和备份的资产。
        """
        if not allowed:
            return None
        row = await self.session.get(TokenMappingORM, token)
        return row.original_value if row else None

    async def tokenize_customer(self, customer_id: str, name: str, phone: str, email: str) -> dict[str, str]:
        """把一个客户的敏感字段整体代号化。

        Returns:
            字段名 → 代号 的映射。上下文里只放这些代号。
        """
        result: dict[str, str] = {}
        if name:
            result["name_token"] = await self.tokenize("person", f"{customer_id}:{name}")
        if phone:
            result["phone_token"] = await self.tokenize("phone", phone)
        if email:
            result["email_token"] = await self.tokenize("email", email)
        return result
