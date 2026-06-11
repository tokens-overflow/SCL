"""命令行测试入口。

交互模式：
    uv run python -m backend.app.cli

演示场景（多轮脚本自动跑）：
    uv run python -m backend.app.cli --scenario 1   # 查物流
    uv run python -m backend.app.cli --scenario 2   # 小额退款成功
    uv run python -m backend.app.cli --scenario 3   # 大额退款触发护栏转人工
"""
import argparse
import sys
import uuid

from backend.app.agent.agent import CSAgent

CYAN, YELLOW, GREEN, RED, RESET = "\033[96m", "\033[93m", "\033[92m", "\033[91m", "\033[0m"


def print_event(event_type: str, payload: dict) -> None:
    if event_type == "tool_use":
        print(f"{YELLOW}  ⚙ 调用工具 {payload['name']}{RESET} {payload['input']}")
    elif event_type == "tool_result":
        color = RED if payload["is_error"] else GREEN
        result = payload["result"]
        if len(result) > 300:
            result = result[:300] + "…"
        print(f"{color}  ↩ {payload['name']} 返回{RESET} {result}")
    elif event_type == "escalated":
        print(f"{RED}  ▲ 已转人工，agent 结束接待{RESET}")


# 演示场景：每个场景是一串用户消息（含确认回合）
SCENARIOS = {
    "1": [
        "你好，我想查一下我的快递到哪了",
        "13800000001",
        "查最近那个运输中的订单",
    ],
    "2": [
        "你好，我买的东西想退款，质量太差了",
        "我的手机号是 13800000002",
        "退那个已签收的、金额 200 以内的订单，全额退",
        "对，确认退款",
    ],
    "3": [
        "我要退货！这个东西完全是骗人的",
        "13800000014",
        "退已签收里最贵的那单，全额退款",
        "确认，就退这个",
    ],
}


def run_scenario(name: str) -> None:
    agent = CSAgent(session_id=f"cli-demo-{name}-{uuid.uuid4().hex[:8]}")
    print(f"\n{'=' * 60}\n演示场景 {name}（session: {agent.state.session_id}）\n{'=' * 60}")
    for msg in SCENARIOS[name]:
        print(f"\n{CYAN}用户：{msg}{RESET}")
        reply = agent.run(msg, on_event=print_event)
        print(f"客服：{reply}")
        if agent.state.escalated:
            break
    print(f"\n触发过的护栏：{agent.state.triggered_guardrails or '无'}")


def interactive() -> None:
    agent = CSAgent(session_id=f"cli-{uuid.uuid4().hex[:8]}")
    print("智能客服 CLI（Ctrl+C / exit 退出）")
    while True:
        try:
            msg = input(f"\n{CYAN}你：{RESET}").strip()
        except (KeyboardInterrupt, EOFError):
            break
        if not msg or msg.lower() in ("exit", "quit"):
            break
        reply = agent.run(msg, on_event=print_event)
        print(f"客服：{reply}")
        if agent.state.escalated:
            print("（会话已转人工，结束）")
            break


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=list(SCENARIOS), help="运行预设演示场景")
    args = parser.parse_args()
    if args.scenario:
        run_scenario(args.scenario)
    else:
        interactive()
    sys.exit(0)
