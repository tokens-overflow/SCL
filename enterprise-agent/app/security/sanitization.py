"""输入净化与提示词注入防护。

这一层解决的问题和数据脱敏不同：

* **脱敏**（`app/control/data_masking.py`）关心的是「不该让模型看到的东西别看到」。
* **净化**（本模块）关心的是「用户输入里可能夹带指令，别让它冒充系统指令」。

需要说明的是：净化是**纵深防御的一层，不是唯一防线**。
真正的红线永远落在控制层的代码里——即使模型被彻底说服要去执行 30% 的折扣，
Control 层照样会拒绝。净化只是降低模型被带偏的概率，减少无谓的失败路径。
"""

from __future__ import annotations

import re

#: 常见的提示词注入话术。这个列表**不可能穷尽**，所以它只用于打标记和降权，
#: 绝不用于「命中就放行/不命中就信任」这种二元判断。
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ignore\s+(all\s+)?(previous|above)\s+instructions", re.I),
    re.compile(r"disregard\s+(all\s+)?(previous|prior)\s+", re.I),
    re.compile(r"忽略(以上|之前|前面)(的)?(所有)?(指令|要求|提示)"),
    re.compile(r"你(现在)?(是|扮演)(一个)?(系统|管理员|开发者)"),
    re.compile(r"(system|developer)\s*(prompt|message)\s*[:：]", re.I),
    re.compile(r"你(有|拥有)(所有|全部|最高)权限"),
    re.compile(r"(不需要|无需|跳过)(任何)?(审批|权限|校验|检查)"),
    re.compile(r"</?(system|assistant|instructions?)>", re.I),
)

#: 控制字符：直接删掉。它们对业务无意义，却能用来构造视觉欺骗。
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: 单次用户输入的长度上限。超长输入既烧 token，也是塞注入内容的常见手法。
MAX_INPUT_LENGTH = 4000


class SanitizationResult:
    """净化结果。

    Attributes:
        text: 净化后的文本，可以安全地放进 Context。
        suspicious: 是否检测到疑似注入话术。
        matched_patterns: 命中的模式描述，仅用于审计和风险加权。
        truncated: 是否发生了截断。
    """

    __slots__ = ("text", "suspicious", "matched_patterns", "truncated")

    def __init__(
        self,
        text: str,
        suspicious: bool,
        matched_patterns: list[str],
        truncated: bool,
    ) -> None:
        self.text = text
        self.suspicious = suspicious
        self.matched_patterns = matched_patterns
        self.truncated = truncated

    def to_dict(self) -> dict[str, object]:
        """序列化为可写入审计的字典（不含原文，避免二次泄漏）。"""
        return {
            "suspicious": self.suspicious,
            "matched_patterns": self.matched_patterns,
            "truncated": self.truncated,
            "length": len(self.text),
        }


def sanitize_user_input(raw: str) -> SanitizationResult:
    """净化用户自由文本输入。

    做四件事：

    1. 删除控制字符；
    2. 截断超长输入；
    3. 标记疑似提示词注入（**只标记不拦截**——拦截会误伤正常业务表达，
       而且真正的防线在控制层）；
    4. 折叠连续空白，降低「用大量空行把系统提示挤出上下文窗口」这类手法的效果。

    Args:
        raw: 原始用户输入。

    Returns:
        :class:`SanitizationResult`。调用方应把 ``suspicious=True`` 作为
        风险加权信号传给 RiskPolicy，而不是直接拒绝请求。
    """
    text = _CONTROL_CHARS.sub("", raw or "")
    text = re.sub(r"[ \t]{4,}", "   ", text)
    text = re.sub(r"\n{4,}", "\n\n", text).strip()

    truncated = False
    if len(text) > MAX_INPUT_LENGTH:
        text = text[:MAX_INPUT_LENGTH]
        truncated = True

    matched = [p.pattern for p in _INJECTION_PATTERNS if p.search(text)]
    return SanitizationResult(
        text=text,
        suspicious=bool(matched),
        matched_patterns=matched,
        truncated=truncated,
    )


def wrap_untrusted(text: str, *, source: str = "user_input") -> str:
    """把不可信文本包进显式边界标记里。

    这样做的目的是让模型能区分「系统给的指令」和「用户说的话」。
    再次强调：**这只是提示，不是强制**。模型完全可能忽略边界。
    真正阻止越权的是控制层，不是这对标签。

    Args:
        text: 不可信文本。
        source: 来源标注，出现在边界标记里。

    Returns:
        带边界标记的文本块。
    """
    fence = f"<<<{source.upper()}"
    # 防止用户在输入里伪造闭合标记来「越狱」出边界。
    safe = text.replace(fence, "").replace(f"{fence}_END", "")
    return f"{fence}\n{safe}\n{fence}_END"
