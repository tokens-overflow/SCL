"""测试夹具：临时 SQLite 文件库 + 数据工厂。

DATABASE_URL 必须在导入任何 backend 模块之前设置（config 在导入时读取），
所以放在本文件最顶部。
"""
import os
import tempfile

_db_fd, _db_path = tempfile.mkstemp(prefix="ai_cs_agent_test_", suffix=".db")
os.close(_db_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"

import pytest  # noqa: E402

from backend.app.agent.state import SessionState  # noqa: E402
from backend.app.db.session import SessionLocal, engine  # noqa: E402
from backend.app.domain.enums import OrderStatus  # noqa: E402
from backend.app.domain.models import Base, Order, User  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def user(db):
    u = User(name="测试用户", phone="13900000001", email="test@example.com")
    db.add(u)
    db.commit()
    return u


@pytest.fixture
def verified_state(user):
    return SessionState(
        session_id="test-session",
        verified_user_id=user.id,
        verified_user_name=user.name,
    )


@pytest.fixture
def anon_state():
    return SessionState(session_id="test-session-anon")


@pytest.fixture
def make_order(db):
    counter = {"n": 0}

    def _make(
        user: User,
        status: OrderStatus = OrderStatus.DELIVERED,
        amount: float = 100.0,
    ) -> Order:
        counter["n"] += 1
        order = Order(
            order_no=f"ORD20990100{counter['n']:02d}",
            user_id=user.id,
            product_name="测试商品",
            quantity=1,
            amount=amount,
            status=status,
            shipping_address="北京市朝阳区幸福路 12 号",
        )
        db.add(order)
        db.commit()
        return order

    return _make
