#!/usr/bin/env python3
import json
import sys
import time
import uuid

session = f"stub-{uuid.uuid4().hex[:8]}"
print(json.dumps({
    "type": "system",
    "subtype": "init",
    "session_id": session,
    "model": "stub-model",
    "claude_code_version": "test",
    "slash_commands": ["context", "verify"],
    "mcp_servers": [],
    "skills": [],
    "agents": [],
    "plugins": [],
}), flush=True)

for line in sys.stdin:
    payload = json.loads(line)
    content = payload.get("message", {}).get("content", [])
    text = "".join(item.get("text", "") for item in content if item.get("type") == "text")
    if text == "sleep":
        time.sleep(30)
    message_id = f"msg-{uuid.uuid4().hex[:8]}"
    print(json.dumps({
        "type": "stream_event",
        "event": {"type": "message_start", "message": {"id": message_id}},
    }), flush=True)
    print(json.dumps({
        "type": "stream_event",
        "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": f"echo:{text}"}},
    }), flush=True)
    print(json.dumps({
        "type": "assistant",
        "message": {"id": message_id, "content": [{"type": "text", "text": f"echo:{text}"}]},
    }), flush=True)
    print(json.dumps({
        "type": "result",
        "is_error": False,
        "duration_ms": 10,
        "total_cost_usd": 0,
        "num_turns": 1,
    }), flush=True)
