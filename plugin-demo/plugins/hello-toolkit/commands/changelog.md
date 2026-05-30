---
description: 根据 git 提交历史生成一份整洁的变更日志(changelog)。
argument-hint: "[起始 git ref，默认上一个 tag]"
allowed-tools: Bash(git log:*), Bash(git tag:*), Bash(git describe:*), Read
---

你需要为当前仓库生成一份变更日志。

## 上下文

- 起始引用（如果用户提供）：$1
- 当前分支与最近提交：
  !`git log --oneline -15`
- 最近的 tag（如果有）：
  !`git describe --tags --abbrev=0 2>/dev/null || echo "（无 tag）"`

## 任务

1. 如果用户在 `$1` 中提供了起始 ref，就统计从该 ref 到 HEAD 的提交；否则从最近的 tag（若无 tag 则取最近 15 条提交）开始统计。
2. 按以下分类整理提交，使用 Conventional Commits 风格的前缀进行归类：
   - ✨ 新功能 (feat)
   - 🐛 修复 (fix)
   - 📝 文档 (docs)
   - ♻️ 重构 (refactor)
   - 🔧 其它 (chore / build / ci / test 等)
3. 以 Markdown 的形式输出，每条目保留简短描述，去掉无意义的合并提交。

如果没有可用的提交，请明确说明「没有发现新的变更」。
