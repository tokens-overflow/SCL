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
