#!/usr/bin/env bash
# SessionStart 钩子：会话开始时向 Claude 注入一段额外上下文。
# stdout 的内容会作为附加上下文提供给模型（不会直接展示给用户）。
set -euo pipefail

echo "【hello-toolkit】插件已激活。可用命令：/hello、/changelog；可用子代理：code-explainer；可用技能：release-notes。"

# 如果当前目录是一个 git 仓库，顺便提供分支信息作为上下文。
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
  echo "【hello-toolkit】当前 git 分支：${branch}"
fi

exit 0
