"""Admin 接口：实时查看 CRM 数据表。"""
from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from backend.app.db.session import session_scope
from backend.app.domain.models import ChatLog, Order, Refund, Ticket, User

router = APIRouter()

ADMIN_TABLES = {
    "users": (User, ["id", "name", "phone", "email", "member_level", "created_at"]),
    "orders": (Order, ["id", "order_no", "user_id", "product_name", "quantity",
                       "amount", "status", "shipping_address", "carrier",
                       "tracking_no", "created_at", "updated_at"]),
    "refunds": (Refund, ["id", "refund_no", "order_id", "user_id", "amount",
                         "reason", "status", "created_at"]),
    "tickets": (Ticket, ["id", "ticket_no", "user_id", "summary", "priority",
                         "status", "created_at"]),
    "chat_logs": (ChatLog, ["id", "session_id", "role", "content", "created_at"]),
}


@router.get("/api/admin/{table}")
def admin_table(table: str, limit: int = 100):
    if table not in ADMIN_TABLES:
        raise HTTPException(404, f"未知表：{table}，可选：{list(ADMIN_TABLES)}")
    model, columns = ADMIN_TABLES[table]
    with session_scope() as db:
        rows = db.scalars(select(model).order_by(model.id.desc()).limit(limit)).all()
    result = []
    for row in rows:
        item = {}
        for col in columns:
            value = getattr(row, col)
            if hasattr(value, "value"):       # 枚举
                value = value.value
            elif hasattr(value, "strftime"):  # 时间
                value = value.strftime("%Y-%m-%d %H:%M:%S")
            elif isinstance(value, str) and len(value) > 120:
                value = value[:120] + "…"
            item[col] = value
        result.append(item)
    return {"table": table, "columns": columns, "rows": result}
