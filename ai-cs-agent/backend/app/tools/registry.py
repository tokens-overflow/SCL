from dataclasses import dataclass
from typing import Any, Callable, Type

from pydantic import BaseModel, ValidationError

from backend.app.agent.state import SessionState
from backend.app.services.guardrails import GuardrailViolation


@dataclass
class ToolDef:
    name: str
    description: str
    input_model: Type[BaseModel]
    handler: Callable[[SessionState, BaseModel], BaseModel]


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
    tool_def = TOOL_REGISTRY.get(name)
    if tool_def is None:
        return f"unknown tool: {name}", True
    try:
        params = tool_def.input_model.model_validate(raw_input)
    except ValidationError as e:
        return f"input validation failed: {e}", True
    try:
        result = tool_def.handler(state, params)
        return result.model_dump_json(), False
    except GuardrailViolation as e:
        return str(e), True
    except Exception as e:
        return f"runtime error: {type(e).__name__}: {e}", True
