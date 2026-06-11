"""CRM ORM models: users / orders / refunds / tickets / chat_logs."""
from datetime import datetime

from sqlalchemy import JSON, DateTime, Enum, Float, ForeignKey, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from backend.app.domain.enums import (
    MemberLevel,
    OrderStatus,
    RefundStatus,
    TicketPriority,
    TicketStatus,
)


class Base(DeclarativeBase):
    pass


def _enum_column(enum_cls):
    """Store enum values, not enum names."""
    return Enum(enum_cls, values_callable=lambda x: [e.value for e in x])


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
    amount: Mapped[float] = mapped_column(Float)
    status: Mapped[OrderStatus] = mapped_column(_enum_column(OrderStatus))
    shipping_address: Mapped[str] = mapped_column(String(256))
    carrier: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tracking_no: Mapped[str | None] = mapped_column(String(32), nullable=True)
    logistics_events: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship(back_populates="orders")
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

    order: Mapped[Order] = relationship(back_populates="refunds")


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ticket_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
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
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
