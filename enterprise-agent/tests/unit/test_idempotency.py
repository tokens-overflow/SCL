"""幂等键与去重逻辑单元测试。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.errors import IdempotencyConflictError
from app.core.ids import arguments_hash, build_idempotency_key, canonical_json
from app.state.repositories import ToolExecutionRepository


class TestCanonicalJson:
    def test_key_order_does_not_matter(self) -> None:
        assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})

    def test_number_normalization(self) -> None:
        """328 与 328.00 必须等价，否则同一个动作会被算成两笔。"""
        assert canonical_json({"amount": 328}) == canonical_json({"amount": 328.00})
        assert canonical_json({"amount": 328}) == canonical_json({"amount": Decimal("328.000")})

    def test_string_trim(self) -> None:
        assert canonical_json({"id": " C001 "}) == canonical_json({"id": "C001"})

    def test_none_is_not_missing(self) -> None:
        """{"a": None} 与 {} 语义不同，不能合并。"""
        assert canonical_json({"a": None}) != canonical_json({})

    def test_nested_normalization(self) -> None:
        left = canonical_json({"o": {"b": 2.0, "a": " x "}})
        right = canonical_json({"o": {"a": "x", "b": 2}})
        assert left == right


class TestIdempotencyKey:
    def test_same_action_same_key(self) -> None:
        """同一个动作在不同时刻提出，必须得到同一个键。

        这是「按动作生成而不是按请求生成」的核心验证：
        模型可能在第 3 步和第 7 步提出同一个动作。
        """
        args = {"customer_id": "C001", "discount_rate": 0.05}
        k1 = build_idempotency_key(task_id="t1", step_name="apply", tool_name="apply_discount", arguments=args)
        k2 = build_idempotency_key(task_id="t1", step_name="apply", tool_name="apply_discount", arguments=dict(reversed(list(args.items()))))
        assert k1 == k2

    def test_different_arguments_different_key(self) -> None:
        base = dict(task_id="t1", step_name="apply", tool_name="apply_discount")
        k1 = build_idempotency_key(**base, arguments={"customer_id": "C001", "discount_rate": 0.05})
        k2 = build_idempotency_key(**base, arguments={"customer_id": "C001", "discount_rate": 0.10})
        assert k1 != k2

    def test_compensation_suffix_isolates_keys(self) -> None:
        """补偿动作有自己的幂等键，不与正向动作冲突。"""
        base = dict(task_id="t1", step_name="apply", tool_name="apply_discount", arguments={"a": 1})
        assert build_idempotency_key(**base) != build_idempotency_key(**base, suffix="comp")

    def test_different_task_different_key(self) -> None:
        base = dict(step_name="apply", tool_name="apply_discount", arguments={"a": 1})
        assert build_idempotency_key(task_id="t1", **base) != build_idempotency_key(task_id="t2", **base)


class TestToolExecutionRepository:
    async def test_reserve_is_idempotent(self, session) -> None:
        repo = ToolExecutionRepository(session)
        args = {"customer_id": "C001"}
        first, is_new_1 = await repo.reserve(
            task_id="t1", step_id="s1", tool_name="apply_discount",
            idempotency_key="key-1", arguments=args, arguments_hash=arguments_hash(args),
        )
        second, is_new_2 = await repo.reserve(
            task_id="t1", step_id="s1", tool_name="apply_discount",
            idempotency_key="key-1", arguments=args, arguments_hash=arguments_hash(args),
        )
        assert is_new_1 is True
        assert is_new_2 is False
        assert first.execution_id == second.execution_id

    async def test_same_key_different_arguments_is_rejected(self, session) -> None:
        """同一幂等键但参数不同**必须拒绝**。

        如果返回旧结果，第二笔业务会被静默吞掉，而没有任何人知道。
        """
        repo = ToolExecutionRepository(session)
        a1 = {"customer_id": "C001", "discount_rate": 0.05}
        a2 = {"customer_id": "C001", "discount_rate": 0.30}
        await repo.reserve(
            task_id="t1", step_id="s1", tool_name="apply_discount",
            idempotency_key="key-2", arguments=a1, arguments_hash=arguments_hash(a1),
        )
        with pytest.raises(IdempotencyConflictError) as exc:
            await repo.reserve(
                task_id="t1", step_id="s1", tool_name="apply_discount",
                idempotency_key="key-2", arguments=a2, arguments_hash=arguments_hash(a2),
            )
        assert exc.value.retryable is False
