"""数据脱敏与代号化单元测试。"""

from __future__ import annotations

from app.control.data_masking import (
    Tokenizer,
    mask_bank_card,
    mask_email,
    mask_id_card,
    mask_payload,
    mask_phone,
    mask_text,
)
from app.security.secrets import redact


class TestMasking:
    def test_email(self) -> None:
        assert mask_email("zhangsan@example.com") == "z*****n@example.com"
        assert mask_email("ab@x.com") == "a*@x.com"

    def test_phone(self) -> None:
        assert mask_phone("13812345678") == "138****5678"
        assert mask_phone("联系电话 13812345678 谢谢") == "联系电话 138****5678 谢谢"

    def test_id_card(self) -> None:
        assert mask_id_card("110101199001011234") == "110101********1234"

    def test_bank_card(self) -> None:
        masked = mask_bank_card("6222021234567890123")
        assert masked.startswith("6222")
        assert masked.endswith("0123")
        assert "1234567890" not in masked

    def test_mask_text_handles_long_numbers_first(self) -> None:
        """先长后短：身份证不能被手机号规则误伤。"""
        out = mask_text("身份证 110101199001011234，手机 13812345678")
        assert "110101199001011234" not in out
        assert "13812345678" not in out
        assert "110101" in out

    def test_mask_payload_recursive(self) -> None:
        payload = {"a": {"phone": "13812345678"}, "b": ["mail: x@y.com"]}
        out = mask_payload(payload)
        assert out["a"]["phone"] == "138****5678"
        assert "x@y.com" not in out["b"][0]

    def test_mask_payload_returns_new_object(self) -> None:
        src = {"phone": "13812345678"}
        mask_payload(src)
        assert src["phone"] == "13812345678"


class TestSecretRedaction:
    def test_field_name_based(self) -> None:
        out = redact({"api_key": "sk-abcdef", "openai_api_key": "x", "name": "ok"})
        assert out["api_key"] == "***REDACTED***"
        assert out["openai_api_key"] == "***REDACTED***"
        assert out["name"] == "ok"

    def test_value_pattern_based(self) -> None:
        """即使字段名没命中，值本身像密钥也要遮掉。"""
        out = redact({"note": "用这个 sk-ant-abcdefghijklmnopqrst 调用"})
        assert "sk-ant-abcdefghijklmnopqrst" not in out["note"]

    def test_nested(self) -> None:
        out = redact({"cfg": {"headers": {"authorization": "Bearer abcdefghijklmnopqrstuvwx"}}})
        assert out["cfg"]["headers"]["authorization"] == "***REDACTED***"


class TestTokenizer:
    async def test_same_value_same_token(self, session) -> None:
        """同一个值必须永远得到同一个代号，否则模型会把同一个人当成两个人。"""
        tok = Tokenizer(session)
        t1 = await tok.tokenize("person", "张三")
        t2 = await tok.tokenize("person", "张三")
        assert t1 == t2
        assert t1.startswith("PERSON_")

    async def test_different_values_different_tokens(self, session) -> None:
        tok = Tokenizer(session)
        assert await tok.tokenize("person", "张三") != await tok.tokenize("person", "李四")

    async def test_entity_type_is_part_of_salt(self, session) -> None:
        """不同类型的相同字符串不共用代号。"""
        tok = Tokenizer(session)
        assert await tok.tokenize("person", "X001") != await tok.tokenize("account", "X001")

    async def test_detokenize_requires_permission(self, session) -> None:
        """还原必须由控制层授权。这不是「反哈希」——靠的是映射表。"""
        tok = Tokenizer(session)
        token = await tok.tokenize("phone", "13812345678")
        assert await tok.detokenize(token, allowed=False) is None
        assert await tok.detokenize(token, allowed=True) == "13812345678"

    async def test_unknown_token_returns_none(self, session) -> None:
        tok = Tokenizer(session)
        assert await tok.detokenize("PERSON_NOPE", allowed=True) is None

    async def test_tokenize_customer_bundle(self, session) -> None:
        tok = Tokenizer(session)
        out = await tok.tokenize_customer("C001", "张三", "13812345678", "z@x.com")
        assert set(out) == {"name_token", "phone_token", "email_token"}
        # 代号里不含任何原始值片段
        assert all("1381" not in v and "张三" not in v for v in out.values())
