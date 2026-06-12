from dataclasses import dataclass
from typing import Any, Callable, Type

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from backend.app.agent.state import SessionState
from backend.app.db.session import session_scope
from backend.app.services.guardrails import GuardrailViolation


@dataclass
class ToolDef:
    name: str
    description: str
    input_model: Type[BaseModel]
    handler: Callable[[Session, SessionState, BaseModel], BaseModel]


TOOL_REGISTRY: dict[str, ToolDef] = {}


def tool(name: str, description: str, input_model: Type[BaseModel]):
    def decorator(fn):
        TOOL_REGISTRY[name] = ToolDef(name, description, input_model, fn)
        return fn

    return decorator


def _clean_schema(schema: dict) -> dict:
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    for defn in schema.get("$defs", {}).values():
        defn.pop("title", None)
    return schema


def get_anthropic_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": _clean_schema(t.input_model.model_json_schema()),
        }
        for t in TOOL_REGISTRY.values()
    ]


def execute_tool(state: SessionState, name: str, raw_input: dict) -> tuple[str, bool]:
    """工具执行漏斗：入参校验 → 事务内执行 → 出参序列化。

    数据库 session 与事务边界在这里统一管理：handler 正常返回则 commit，
    抛异常（含护栏拦截）则 rollback——半截写入不会落库。
    三类失败原样回传给模型自我修正：未知工具 / 入参校验失败 / 护栏拦截。
    """
    tool_def = TOOL_REGISTRY.get(name)
    if tool_def is None:
        return f"unknown tool: {name}", True
    try:
        params = tool_def.input_model.model_validate(raw_input)
    except ValidationError as e:
        return f"input validation failed: {e}", True
    try:
        with session_scope() as session:
            result = tool_def.handler(session, state, params)
        return result.model_dump_json(), False
    except GuardrailViolation as e:
        return str(e), True
    except Exception as e:
        return f"runtime error: {type(e).__name__}: {e}", True
