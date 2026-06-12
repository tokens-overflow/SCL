"""工具注册表测试：Schema 自动生成 + execute_tool 漏斗的四类出口。"""
from backend.app.tools import execute_tool, get_anthropic_tools
from backend.app.tools.registry import TOOL_REGISTRY

EXPECTED_TOOLS = {
    "verify_identity",
    "get_user_profile",
    "list_orders",
    "get_order_detail",
    "get_logistics",
    "update_shipping_address",
    "cancel_order",
    "create_refund",
    "escalate_to_human",
}


def test_all_tools_registered():
    assert set(TOOL_REGISTRY) == EXPECTED_TOOLS


def test_schema_generated_from_pydantic():
    tools = {t["name"]: t for t in get_anthropic_tools()}
    refund = tools["create_refund"]
    props = refund["input_schema"]["properties"]
    assert set(refund["input_schema"]["required"]) == {"order_no", "reason", "amount"}
    # Field(description=...) 必须进 schema——这是模型唯一能看到的传参说明
    assert "订单号" in props["order_no"]["description"]
    assert props["amount"]["exclusiveMinimum"] == 0
    # title 是噪音，应被清掉
    assert "title" not in refund["input_schema"]
    assert all("title" not in p for p in props.values())


def test_unknown_tool_returns_error(anon_state):
    result, is_error = execute_tool(anon_state, "no_such_tool", {})
    assert is_error
    assert "unknown tool" in result


def test_validation_error_fed_back_to_model(anon_state):
    result, is_error = execute_tool(anon_state, "verify_identity", {"phone": "123"})
    assert is_error
    assert "input validation failed" in result


def test_guardrail_violation_fed_back_to_model(anon_state):
    result, is_error = execute_tool(anon_state, "list_orders", {"user_id": 1})
    assert is_error
    assert "护栏拦截" in result


def test_happy_path_returns_json_and_updates_state(anon_state, user):
    result, is_error = execute_tool(anon_state, "verify_identity", {"phone": user.phone})
    assert not is_error
    assert '"verified":true' in result.replace(" ", "")
    assert anon_state.verified_user_id == user.id
