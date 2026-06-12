"""Service 层业务逻辑测试：核身、退款（含大额转人工）、取消订单、改地址、转人工。"""
import pytest
from sqlalchemy import select

from backend.app.domain.enums import OrderStatus, RefundStatus, TicketPriority
from backend.app.domain.models import Refund, Ticket
from backend.app.services.guardrails import GuardrailViolation
from backend.app.services.identity_service import mask_phone, verify_identity
from backend.app.services.order_service import cancel_order, update_shipping_address
from backend.app.services.refund_service import create_refund
from backend.app.services.ticket_service import escalate_to_human


def test_mask_phone():
    assert mask_phone("13900000001") == "139****0001"


class TestVerifyIdentity:
    def test_success_writes_state(self, db, anon_state, user):
        result = verify_identity(db, anon_state, user.phone)
        assert result.verified
        assert result.user_id == user.id
        assert anon_state.verified_user_id == user.id
        assert anon_state.verified_user_name == user.name

    def test_unknown_phone_fails_with_masked_number(self, db, anon_state):
        result = verify_identity(db, anon_state, "13911112222")
        assert not result.verified
        assert anon_state.verified_user_id is None
        assert "13911112222" not in result.message
        assert "139****2222" in result.message


class TestCreateRefund:
    def test_small_amount_creates_refund(self, db, verified_state, user, make_order):
        order = make_order(user, status=OrderStatus.DELIVERED, amount=89.0)
        result = create_refund(db, verified_state, order.order_no, "质量问题", 89.0)
        db.commit()

        assert result.status == "created"
        refund = db.scalar(select(Refund).where(Refund.refund_no == result.refund_no))
        assert refund is not None
        assert refund.amount == 89.0
        assert refund.status == RefundStatus.PENDING
        assert order.status == OrderStatus.REFUNDED

    def test_large_amount_escalates_without_refunding(self, db, verified_state, user, make_order):
        order = make_order(user, status=OrderStatus.DELIVERED, amount=1099.0)
        result = create_refund(db, verified_state, order.order_no, "不想要了", 1099.0)
        db.commit()

        assert result.status == "escalated"
        assert result.refund_no is None
        assert verified_state.escalated
        # 退款没执行，工单由代码直接创建——不依赖模型自觉
        assert db.scalar(select(Refund)) is None
        assert order.status == OrderStatus.DELIVERED
        ticket = db.scalar(select(Ticket).where(Ticket.ticket_no == result.ticket_no))
        assert ticket.priority == TicketPriority.HIGH
        assert order.order_no in ticket.summary


class TestCancelOrder:
    def test_cancels_pending_shipment(self, db, verified_state, user, make_order):
        order = make_order(user, status=OrderStatus.PENDING_SHIPMENT, amount=599.0)
        result = cancel_order(db, verified_state, order.order_no, "下错单了")
        db.commit()

        assert result.success
        assert result.refund_amount == 599.0
        assert order.status == OrderStatus.CANCELLED

    def test_blocks_shipped_order(self, db, verified_state, user, make_order):
        order = make_order(user, status=OrderStatus.IN_TRANSIT)
        with pytest.raises(GuardrailViolation, match="只有「待发货」订单可以取消"):
            cancel_order(db, verified_state, order.order_no, "不想要了")
        assert order.status == OrderStatus.IN_TRANSIT


class TestUpdateShippingAddress:
    def test_updates_pending_shipment(self, db, verified_state, user, make_order):
        order = make_order(user, status=OrderStatus.PENDING_SHIPMENT)
        new_address = "上海市浦东新区科技园南区 8 栋"
        result = update_shipping_address(db, verified_state, order.order_no, new_address)
        assert result.success
        assert order.shipping_address == new_address

    def test_blocks_shipped_order(self, db, verified_state, user, make_order):
        order = make_order(user, status=OrderStatus.IN_TRANSIT)
        with pytest.raises(GuardrailViolation, match="无法修改收货地址"):
            update_shipping_address(db, verified_state, order.order_no, "上海市浦东新区某地")


class TestEscalateToHuman:
    def test_creates_ticket_and_ends_session(self, db, anon_state):
        result = escalate_to_human(db, anon_state, "用户要求人工处理", TicketPriority.URGENT)
        db.commit()

        assert anon_state.escalated
        assert result.session_ended
        ticket = db.scalar(select(Ticket).where(Ticket.ticket_no == result.ticket_no))
        assert ticket.user_id is None  # 未核身也可转人工
        assert ticket.priority == TicketPriority.URGENT
