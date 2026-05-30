#!/usr/bin/env bash
# PostToolUse 钩子：在 Claude 编辑/写入文件之后触发。
# 该钩子从 stdin 读取一个 JSON 事件，这里仅做演示——把被修改的文件路径
# 追加记录到插件目录下的日志文件，方便用户观察钩子确实被触发了。
set -euo pipefail

log_file="${CLAUDE_PLUGIN_ROOT}/.edit-log"
payload="$(cat || true)"

# 尽量从事件 JSON 中解析出文件路径（优先使用 jq，没有 jq 时退回到 grep）。
file_path=""
if command -v jq >/dev/null 2>&1; then
  file_path="$(printf '%s' "$payload" | jq -r '.tool_input.file_path // empty' 2>/dev/null || true)"
fi
if [ -z "$file_path" ]; then
  file_path="$(printf '%s' "$payload" | grep -o '"file_path"[^,]*' | head -n1 || true)"
fi

printf '%s  edited: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "${file_path:-未知}" >> "$log_file"

exit 0
