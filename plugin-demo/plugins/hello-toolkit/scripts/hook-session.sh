#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────
# 【会话级 Hook】事件：SessionStart
# 触发频率：每个会话只触发「一次」——会话开始的那一刻。
#
# 动态演示：维护一个「会话计数器」，每开一个新会话 +1。
# 你会观察到：在同一个会话里反复提问、反复调用工具，这个数字
# 都「不变」；只有新开会话时才 +1。
# ───────────────────────────────────────────────────────────────
set -euo pipefail

state_dir="${CLAUDE_PLUGIN_ROOT}/.hook-state"
mkdir -p "$state_dir"
counter_file="${state_dir}/session.count"

# 读取旧值并 +1
count=$(cat "$counter_file" 2>/dev/null || echo 0)
count=$((count + 1))
echo "$count" > "$counter_file"

# 同时为本会话生成一个唯一 ID，方便后面轮次级/工具级脚本引用
session_id="S$(date +%s)-$$"
echo "$session_id" > "${state_dir}/current_session_id"
# 重置「本会话内」的轮次与工具计数器，从 0 重新开始
echo 0 > "${state_dir}/turn.count"
echo 0 > "${state_dir}/tool.count"

# stdout 会作为上下文注入给模型（用户不直接看到）
echo "【会话级 Hook】这是第 ${count} 个会话被启动（session_id=${session_id}）。本会话的轮次计数与工具计数已归零。"

exit 0
