"""单号生成测试：当月序号递增、跨月从 0001 重新计数。"""
from datetime import datetime

from backend.app.domain.models import Refund, Ticket
from backend.app.services.numbers import next_refund_no, next_ticket_no


def _ym() -> str:
    return f"{datetime.now():%Y%m}"


def test_first_refund_no(db):
    assert next_refund_no(db) == f"RFD{_ym()}0001"


def test_refund_no_increments_within_month(db, user, make_order):
    order = make_order(user)
    db.add(Refund(refund_no=f"RFD{_ym()}0007", order_id=order.id,
                  user_id=user.id, amount=1.0, reason="测试"))
    db.commit()
    assert next_refund_no(db) == f"RFD{_ym()}0008"


def test_refund_no_resets_across_months(db, user, make_order):
    order = make_order(user)
    # 上个月（或任意旧月份）的单号不影响当月计数
    db.add(Refund(refund_no="RFD2025120042", order_id=order.id,
                  user_id=user.id, amount=1.0, reason="测试"))
    db.commit()
    assert next_refund_no(db) == f"RFD{_ym()}0001"


def test_ticket_no_increments_within_month(db, user):
    db.add(Ticket(ticket_no=f"TKT{_ym()}0003", user_id=user.id, summary="测试工单"))
    db.commit()
    assert next_ticket_no(db) == f"TKT{_ym()}0004"
