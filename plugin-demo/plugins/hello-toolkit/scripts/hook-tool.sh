#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────
# 【工具级 Hook】事件：PreToolUse（matcher 匹配具体工具）
# 触发频率：模型「每调用一次工具」就触发一次 —— 频率最高。
#
# 动态演示：从 stdin 读取事件 JSON，取出本次调用的工具名，把
# 「本轮内的工具调用计数」+1，并把每次调用追加到日志文件。
# 与轮次级对比：你一轮里如果让 Claude 连续读 3 个文件 + 跑 2 条
# 命令，这个数字这一轮就会从 1 一路涨到 5。
# ───────────────────────────────────────────────────────────────
set -euo pipefail

state_dir="${CLAUDE_PLUGIN_ROOT}/.hook-state"
mkdir -p "$state_dir"
counter_file="${state_dir}/tool.count"
log_file="${state_dir}/tool-calls.log"

# PreToolUse 事件会通过 stdin 传入 JSON
payload="$(cat || true)"

# 解析工具名：优先用 jq，没有就退回 grep
tool_name=""
if command -v jq >/dev/null 2>&1; then
  tool_name="$(printf '%s' "$payload" | jq -r '.tool_name // empty' 2>/dev/null || true)"
fi
if [ -z "$tool_name" ]; then
  tool_name="$(printf '%s' "$payload" | grep -o '"tool_name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -n1 | sed 's/.*:[[:space:]]*"//; s/"$//' || true)"
fi
tool_name="${tool_name:-未知工具}"

count=$(cat "$counter_file" 2>/dev/null || echo 0)
count=$((count + 1))
echo "$count" > "$counter_file"

turn=$(cat "${state_dir}/turn.count" 2>/dev/null || echo "?")

# 追加一行可读日志，方便你 tail 观察
printf '%s  [第%s轮·本轮第%s次]  调用工具: %s\n' \
  "$(date '+%H:%M:%S')" "$turn" "$count" "$tool_name" >> "$log_file"

# PreToolUse 用 JSON 输出告诉 Claude Code「放行」，并附一句上下文
printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"},"systemMessage":"【工具级 Hook】本轮第 %s 次工具调用：%s"}\n' \
  "$count" "$tool_name"

exit 0
