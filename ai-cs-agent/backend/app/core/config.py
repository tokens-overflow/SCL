"""Application configuration loaded from .env and environment variables."""
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(PROJECT_ROOT / ".env")

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-fable-5")
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{PROJECT_ROOT / 'crm.db'}")
BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000")
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "10"))
REFUND_AUTO_ESCALATE_THRESHOLD = float(os.getenv("REFUND_AUTO_ESCALATE_THRESHOLD", "200"))
