"""数据库连接管理：SQLite + SQLAlchemy 2.0"""
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

# .env 放在项目根目录（ai-cs-agent/）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{PROJECT_ROOT / 'crm.db'}")

# check_same_thread=False：FastAPI 多线程访问 SQLite 需要
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Session:
    """获取一个新的数据库会话，调用方负责关闭。"""
    return SessionLocal()
