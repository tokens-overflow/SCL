"""Domain enum definitions shared by ORM models and tool schemas."""
import enum


class MemberLevel(str, enum.Enum):
    """会员等级"""

    BRONZE = "bronze"
    SILVER = "silver"
    GOLD = "gold"
    PLATINUM = "platinum"


class OrderStatus(str, enum.Enum):
    """订单状态（含物流阶段）"""

    PENDING_SHIPMENT = "pending_shipment"
    IN_TRANSIT = "in_transit"
    DELIVERED = "delivered"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"

    @property
    def label(self) -> str:
        """中文状态名，用于护栏提示与回复文案。"""
        return _ORDER_STATUS_LABELS[self]


_ORDER_STATUS_LABELS = {
    OrderStatus.PENDING_SHIPMENT: "待发货",
    OrderStatus.IN_TRANSIT: "运输中",
    OrderStatus.DELIVERED: "已签收",
    OrderStatus.REFUNDED: "已退款",
    OrderStatus.CANCELLED: "已取消",
}


class RefundStatus(str, enum.Enum):
    """退款状态"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class TicketPriority(str, enum.Enum):
    """人工工单优先级"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class TicketStatus(str, enum.Enum):
    """人工工单状态"""

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    CLOSED = "closed"
