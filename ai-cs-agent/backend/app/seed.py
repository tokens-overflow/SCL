"""造数脚本：15 个用户、50 个订单（覆盖各种状态）、退款单和历史工单。

用法：
    uv run python -m backend.app.seed
"""
import random
from datetime import datetime, timedelta

from backend.app.db.session import engine, get_session
from backend.app.domain.enums import (
    MemberLevel,
    OrderStatus,
    RefundStatus,
    TicketPriority,
    TicketStatus,
)
from backend.app.domain.models import Base, Order, Refund, Ticket, User

random.seed(42)  # 可复现

USERS = [
    ("张伟", "13800000001", MemberLevel.PLATINUM),
    ("王芳", "13800000002", MemberLevel.GOLD),
    ("李娜", "13800000003", MemberLevel.GOLD),
    ("刘强", "13800000004", MemberLevel.SILVER),
    ("陈静", "13800000005", MemberLevel.SILVER),
    ("杨洋", "13800000006", MemberLevel.SILVER),
    ("赵敏", "13800000007", MemberLevel.BRONZE),
    ("黄磊", "13800000008", MemberLevel.BRONZE),
    ("周杰", "13800000009", MemberLevel.BRONZE),
    ("吴婷", "13800000010", MemberLevel.GOLD),
    ("徐磊", "13800000011", MemberLevel.BRONZE),
    ("孙丽", "13800000012", MemberLevel.SILVER),
    ("马超", "13800000013", MemberLevel.BRONZE),
    ("朱琳", "13800000014", MemberLevel.PLATINUM),
    ("胡军", "13800000015", MemberLevel.BRONZE),
]

PRODUCTS = [
    ("无线蓝牙耳机 Pro", 399.0),
    ("便携咖啡机", 268.0),
    ("机械键盘 87 键", 459.0),
    ("智能手环 6 代", 199.0),
    ("保温杯 500ml", 89.0),
    ("电动牙刷套装", 159.0),
    ("筋膜枪 Mini", 329.0),
    ("行李箱 20 寸", 599.0),
    ("空气炸锅 4L", 349.0),
    ("羊毛围巾", 129.0),
    ("降噪头戴耳机", 899.0),
    ("智能体脂秤", 99.0),
    ("便携显示器 15.6 寸", 1099.0),
    ("桌面加湿器", 69.0),
    ("跑步鞋 轻量款", 459.0),
]

CITIES = ["北京市朝阳区", "上海市浦东新区", "广州市天河区", "深圳市南山区",
          "杭州市西湖区", "成都市武侯区", "武汉市洪山区", "南京市鼓楼区"]
STREETS = ["幸福路 12 号", "科技园南区 8 栋", "解放大道 506 号", "望江路 99 号",
           "梅花山庄 3 期 5-302", "翠湖天地 18 栋 1201", "东湖路 77 号", "中山北路 200 号"]

CARRIERS = ["顺丰速运", "中通快递", "圆通速递", "京东物流"]

REASONS = ["商品质量问题", "尺寸不合适", "与描述不符", "不想要了", "收到时已损坏"]

TICKET_SAMPLES = [
    ("用户反馈耳机左耳无声，要求换货", TicketPriority.HIGH, TicketStatus.CLOSED),
    ("咨询发票开具流程", TicketPriority.LOW, TicketStatus.CLOSED),
    ("物流长时间未更新，用户催促", TicketPriority.MEDIUM, TicketStatus.CLOSED),
    ("大额退款申请，需人工审核", TicketPriority.HIGH, TicketStatus.IN_PROGRESS),
    ("用户投诉客服响应慢", TicketPriority.URGENT, TicketStatus.OPEN),
    ("会员积分未到账", TicketPriority.MEDIUM, TicketStatus.OPEN),
]


def make_logistics_events(status: OrderStatus, created: datetime) -> list[dict] | None:
    """根据订单状态生成物流轨迹。"""
    if status in (OrderStatus.PENDING_SHIPMENT, OrderStatus.CANCELLED):
        return None
    city = random.choice(CITIES).split("市")[0] + "市"
    events = [
        {"time": (created + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M"),
         "location": "广州转运中心", "description": "商家已发货，快件已揽收"},
        {"time": (created + timedelta(days=1)).strftime("%Y-%m-%d %H:%M"),
         "location": "广州转运中心", "description": "快件已从广州转运中心发出"},
        {"time": (created + timedelta(days=2)).strftime("%Y-%m-%d %H:%M"),
         "location": f"{city}转运中心", "description": f"快件已到达 {city}转运中心"},
    ]
    if status in (OrderStatus.DELIVERED, OrderStatus.REFUNDED):
        events += [
            {"time": (created + timedelta(days=2, hours=8)).strftime("%Y-%m-%d %H:%M"),
             "location": f"{city}派送点", "description": "快件正在派送中，请保持电话畅通"},
            {"time": (created + timedelta(days=2, hours=12)).strftime("%Y-%m-%d %H:%M"),
             "location": f"{city}派送点", "description": "快件已签收，签收人：本人"},
        ]
    return events


def seed() -> None:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session = get_session()
    now = datetime.now()

    # ---- 用户 ----
    users: list[User] = []
    for i, (name, phone, level) in enumerate(USERS, start=1):
        u = User(name=name, phone=phone, email=f"user{i:02d}@example.com",
                 member_level=level)
        users.append(u)
    session.add_all(users)
    session.flush()  # 拿到 user.id

    # ---- 订单：50 单，覆盖各状态 ----
    # 状态分布：待发货 12 / 运输中 13 / 已签收 17 / 已退款 6 / 已取消 2
    status_pool = (
        [OrderStatus.PENDING_SHIPMENT] * 12
        + [OrderStatus.IN_TRANSIT] * 13
        + [OrderStatus.DELIVERED] * 17
        + [OrderStatus.REFUNDED] * 6
        + [OrderStatus.CANCELLED] * 2
    )
    random.shuffle(status_pool)

    orders: list[Order] = []
    for i, status in enumerate(status_pool, start=1):
        user = random.choice(users)
        product, price = random.choice(PRODUCTS)
        qty = random.choice([1, 1, 1, 2])
        created = now - timedelta(days=random.randint(0, 30), hours=random.randint(0, 23))
        shipped = status not in (OrderStatus.PENDING_SHIPMENT, OrderStatus.CANCELLED)
        order = Order(
            order_no=f"ORD{now:%Y%m}{i:04d}",
            user_id=user.id,
            product_name=product,
            quantity=qty,
            amount=round(price * qty, 2),
            status=status,
            shipping_address=f"{random.choice(CITIES)}{random.choice(STREETS)}",
            carrier=random.choice(CARRIERS) if shipped else None,
            tracking_no=f"SF{random.randint(10**11, 10**12 - 1)}" if shipped else None,
            logistics_events=make_logistics_events(status, created),
            created_at=created,
        )
        orders.append(order)
    session.add_all(orders)
    session.flush()

    # ---- 退款单：已退款订单各对应一条 approved 记录 ----
    refunds = []
    refund_seq = 1
    for order in orders:
        if order.status == OrderStatus.REFUNDED:
            refunds.append(Refund(
                refund_no=f"RFD{now:%Y%m}{refund_seq:04d}",
                order_id=order.id,
                user_id=order.user_id,
                amount=order.amount,
                reason=random.choice(REASONS),
                status=RefundStatus.APPROVED,
                created_at=order.created_at + timedelta(days=3),
            ))
            refund_seq += 1
    session.add_all(refunds)

    # ---- 历史工单 ----
    tickets = []
    for i, (summary, priority, status) in enumerate(TICKET_SAMPLES, start=1):
        tickets.append(Ticket(
            ticket_no=f"TKT{now:%Y%m}{i:04d}",
            user_id=random.choice(users).id,
            summary=summary,
            priority=priority,
            status=status,
            created_at=now - timedelta(days=random.randint(1, 20)),
        ))
    session.add_all(tickets)

    session.commit()
    session.close()
    print(f"✅ Seed 完成：{len(users)} 用户 / {len(orders)} 订单 / "
          f"{len(refunds)} 退款单 / {len(tickets)} 工单")


if __name__ == "__main__":
    seed()
