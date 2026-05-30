#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────
# 【轮次级 Hook】事件：UserPromptSubmit
# 触发频率：你「每提交一次提问」触发一次 —— 即每一轮对话一次。
#
# 动态演示：维护「本会话内的轮次计数器」。你每发一条消息，它就 +1。
# 与会话级对比：会话级数字在一次会话里始终不动，而这个数字会随着
# 你每轮提问稳步增长。
# ───────────────────────────────────────────────────────────────
set -euo pipefail

state_dir="${CLAUDE_PLUGIN_ROOT}/.hook-state"
mkdir -p "$state_dir"
counter_file="${state_dir}/turn.count"

count=$(cat "$counter_file" 2>/dev/null || echo 0)
count=$((count + 1))
echo "$count" > "$counter_file"

session_id=$(cat "${state_dir}/current_session_id" 2>/dev/null || echo "unknown")

# 每新的一轮，把上一轮的工具调用次数清零，方便单独观察「这一轮用了几次工具」
last_tool=$(cat "${state_dir}/tool.count" 2>/dev/null || echo 0)
echo 0 > "${state_dir}/tool.count"

echo "【轮次级 Hook】会话 ${session_id} 进入第 ${count} 轮提问。（上一轮共发生 ${last_tool} 次工具调用，已清零重新计。）"

exit 0
