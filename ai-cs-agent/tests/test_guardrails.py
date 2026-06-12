"""代码硬护栏的单元测试：每条规则的拦截与放行。"""
import pytest

from backend.app.core.config import REFUND_AUTO_ESCALATE_THRESHOLD
from backend.app.domain.enums import OrderStatus
from backend.app.services.guardrails import (
    GuardrailViolation,
    check_address_change_allowed,
    check_cancel_allowed,
    check_refund_basic,
    refund_needs_escalation,
    require_own_order,
    require_own_user,
    require_verified,
)


class TestRequireVerified:
    def test_blocks_unverified(self, anon_state):
        with pytest.raises(GuardrailViolation, match="尚未完成身份核验"):
            require_verified(anon_state)
        assert "require_verified" in anon_state.triggered_guardrails

    def test_returns_user_id_when_verified(self, verified_state, user):
        assert require_verified(verified_state) == user.id


class TestRequireOwnUser:
    def test_blocks_other_user(self, verified_state, user):
        with pytest.raises(GuardrailViolation, match="跨用户"):
            require_own_user(verified_state, user.id + 1)

    def test_allows_own_user(self, verified_state, user):
        require_own_user(verified_state, user.id)


class TestRequireOwnOrder:
    def test_blocks_missing_order(self, verified_state):
        with pytest.raises(GuardrailViolation, match="不存在"):
            require_own_order(verified_state, None, "ORD404")

    def test_blocks_other_users_order(self, db, verified_state, make_order):
        from backend.app.domain.models import User

        other = User(name="其他人", phone="13900000002", email="other@example.com")
        db.add(other)
        db.commit()
        order = make_order(other)
        with pytest.raises(GuardrailViolation, match="不属于当前核身用户"):
            require_own_order(verified_state, order, order.order_no)

    def test_allows_own_order(self, verified_state, user, make_order):
        order = make_order(user)
        assert require_own_order(verified_state, order, order.order_no) is order


class TestAddressChange:
    def test_blocks_shipped_order(self, verified_state, user, make_order):
        order = make_order(user, status=OrderStatus.IN_TRANSIT)
        with pytest.raises(GuardrailViolation, match="运输中"):
            check_address_change_allowed(verified_state, order)

    def test_allows_pending_shipment(self, verified_state, user, make_order):
        order = make_order(user, status=OrderStatus.PENDING_SHIPMENT)
        check_address_change_allowed(verified_state, order)


class TestCancelOrder:
    def test_blocks_delivered_order(self, verified_state, user, make_order):
        order = make_order(user, status=OrderStatus.DELIVERED)
        with pytest.raises(GuardrailViolation, match="已签收"):
            check_cancel_allowed(verified_state, order)
        assert "cancel_after_shipment" in verified_state.triggered_guardrails

    def test_allows_pending_shipment(self, verified_state, user, make_order):
        order = make_order(user, status=OrderStatus.PENDING_SHIPMENT)
        check_cancel_allowed(verified_state, order)


class TestRefundBasic:
    def test_blocks_already_refunded(self, verified_state, user, make_order):
        order = make_order(user, status=OrderStatus.REFUNDED)
        with pytest.raises(GuardrailViolation, match="无法重复退款"):
            check_refund_basic(verified_state, order, 50.0)

    def test_blocks_cancelled(self, verified_state, user, make_order):
        order = make_order(user, status=OrderStatus.CANCELLED)
        with pytest.raises(GuardrailViolation, match="已取消"):
            check_refund_basic(verified_state, order, 50.0)

    def test_pending_shipment_directs_to_cancel(self, verified_state, user, make_order):
        order = make_order(user, status=OrderStatus.PENDING_SHIPMENT)
        with pytest.raises(GuardrailViolation, match="cancel_order"):
            check_refund_basic(verified_state, order, 50.0)

    def test_blocks_amount_exceeding_order(self, verified_state, user, make_order):
        order = make_order(user, amount=100.0)
        with pytest.raises(GuardrailViolation, match="超过订单实付金额"):
            check_refund_basic(verified_state, order, 100.01)

    def test_allows_valid_refund(self, verified_state, user, make_order):
        order = make_order(user, amount=100.0)
        check_refund_basic(verified_state, order, 100.0)


def test_refund_escalation_threshold():
    assert not refund_needs_escalation(REFUND_AUTO_ESCALATE_THRESHOLD)
    assert refund_needs_escalation(REFUND_AUTO_ESCALATE_THRESHOLD + 0.01)
