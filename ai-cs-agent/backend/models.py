"""CRM 数据模型：users / orders / refunds / tickets / chat_logs

SQLAlchemy 2.0 风格（DeclarativeBase + Mapped + mapped_column）。
"""
import enum
from datetime import datetime

from sqlalchemy import JSON, DateTime, Enum, Float, ForeignKey, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _enum_column(enum_cls):
    """让 Enum 列存储枚举的 value（小写）而不是 name（大写）。"""
    return Enum(enum_cls, values_callable=lambda x: [e.value for e in x])


class MemberLevel(str, enum.Enum):
    """会员等级"""
    BRONZE = "bronze"      # 普通会员
    SILVER = "silver"      # 银卡会员
    GOLD = "gold"          # 金卡会员
    PLATINUM = "platinum"  # 白金会员


class OrderStatus(str, enum.Enum):
    """订单状态（含物流阶段）"""
    PENDING_SHIPMENT = "pending_shipment"  # 待发货
    IN_TRANSIT = "in_transit"              # 运输中
    DELIVERED = "delivered"                # 已签收
    REFUNDED = "refunded"                  # 已退款


class RefundStatus(str, enum.Enum):
    PENDING = "pending"    # 待审核
    APPROVED = "approved"  # 已通过
    REJECTED = "rejected"  # 已驳回


class TicketPriority(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class TicketStatus(str, enum.Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    CLOSED = "closed"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64))
    phone: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(128))
    member_level: Mapped[MemberLevel] = mapped_column(
        _enum_column(MemberLevel), default=MemberLevel.BRONZE
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    orders: Mapped[list["Order"]] = relationship(back_populates="user")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    product_name: Mapped[str] = mapped_column(String(128))
    quantity: Mapped[int] = mapped_column(default=1)
    amount: Mapped[float] = mapped_column(Float)  # 订单金额（元）
    status: Mapped[OrderStatus] = mapped_column(_enum_column(OrderStatus))
    shipping_address: Mapped[str] = mapped_column(String(256))
    # 物流信息：未发货时为空
    carrier: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tracking_no: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 物流轨迹：[{"time": "...", "location": "...", "description": "..."}]
    logistics_events: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="orders")
    refunds: Mapped[list["Refund"]] = relationship(back_populates="order")


class Refund(Base):
    __tablename__ = "refunds"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    refund_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    amount: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(String(256))
    status: Mapped[RefundStatus] = mapped_column(
        _enum_column(RefundStatus), default=RefundStatus.PENDING
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    order: Mapped["Order"] = relationship(back_populates="refunds")


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ticket_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )  # 未核身用户转人工时可能没有 user_id
    summary: Mapped[str] = mapped_column(Text)
    priority: Mapped[TicketPriority] = mapped_column(
        _enum_column(TicketPriority), default=TicketPriority.MEDIUM
    )
    status: Mapped[TicketStatus] = mapped_column(
        _enum_column(TicketStatus), default=TicketStatus.OPEN
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ChatLog(Base):
    __tablename__ = "chat_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    # role: user / assistant / tool_use / tool_result / system
    role: Mapped[str] = mapped_column(String(16))
    # 文本消息存原文；工具调用/结果存 JSON 序列化字符串
    content: Mapped[str] = mapped_column(Text)
    # 工具调用相关元数据（工具名、tool_use_id 等），普通消息为空
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
