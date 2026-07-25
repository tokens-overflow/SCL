#!/usr/bin/env bash
# ccmem 安装脚本:复制脚本 → inspect 确认格式 → 建索引 → 合并 hooks 进 settings.json
set -euo pipefail

CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
CCMEM_DIR="$CLAUDE_DIR/ccmem"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$CCMEM_DIR/ccmem.py"

if [ -z "$PYTHON_BIN" ]; then
  echo "错误:找不到 python3" >&2
  exit 1
fi

echo "== 1/4 安装脚本到 $CCMEM_DIR =="
mkdir -p "$CCMEM_DIR"
cp "$SRC_DIR/ccmem.py" "$SCRIPT"
echo "已复制 ccmem.py"

echo
echo "== 2/4 检查 transcript 格式(inspect)=="
"$PYTHON_BIN" "$SCRIPT" inspect || true
echo
if [ -t 0 ]; then
  printf "上面的 role / 正文解析看起来正确吗?回车继续安装,Ctrl-C 取消: "
  read -r _
else
  echo "(非交互模式,自动继续。若解析不对,请修改 $SCRIPT 中的 extract_role/extract_text 后重跑 index --force)"
fi

echo
echo "== 3/4 建立索引 =="
"$PYTHON_BIN" "$SCRIPT" index
"$PYTHON_BIN" "$SCRIPT" stats

echo
echo "== 4/4 合并 hooks 到 $CLAUDE_DIR/settings.json =="
"$PYTHON_BIN" - "$CLAUDE_DIR" "$PYTHON_BIN" "$SCRIPT" <<'PYEOF'
import json, os, sys, time

claude_dir, pybin, script = sys.argv[1], sys.argv[2], sys.argv[3]
settings_path = os.path.join(claude_dir, "settings.json")

# 读取并解析;解析失败必须中止,绝不覆盖用户配置
settings = {}
raw = None
if os.path.exists(settings_path):
    with open(settings_path, "r", encoding="utf-8") as f:
        raw = f.read()
    if raw.strip():
        try:
            settings = json.loads(raw)
        except Exception as e:
            sys.exit("错误:无法解析 %s(%s)。为避免破坏现有配置,安装中止,"
                     "请手动修复该文件后重试。" % (settings_path, e))
    if not isinstance(settings, dict):
        sys.exit("错误:%s 顶层不是 JSON 对象,安装中止。" % settings_path)
    bak = "%s.bak.%s" % (settings_path, time.strftime("%Y%m%d%H%M%S"))
    n = 1
    while os.path.exists(bak):   # 同一秒内重复运行不要覆盖上一份备份
        bak = "%s.bak.%s-%d" % (settings_path, time.strftime("%Y%m%d%H%M%S"), n)
        n += 1
    with open(bak, "w", encoding="utf-8") as f:
        f.write(raw)
    print("已备份原配置到 %s" % bak)

hooks = settings.setdefault("hooks", {})


def is_ccmem(handler):
    # 按 command/args 里是否含 ccmem 识别本工具注册过的 handler
    return "ccmem" in json.dumps(handler, ensure_ascii=False)


def without_ccmem(event):
    groups = hooks.get(event)
    if not isinstance(groups, list):
        return []
    kept = []
    for g in groups:
        if isinstance(g, dict) and isinstance(g.get("hooks"), list):
            g = dict(g)
            g["hooks"] = [h for h in g["hooks"] if not is_ccmem(h)]
            if not g["hooks"]:
                continue
        kept.append(g)
    return kept


# exec form(command + args 数组),路径含空格也不需要操心引号
new_handlers = {
    "UserPromptSubmit": {"type": "command", "command": pybin,
                         "args": [script, "recall"], "timeout": 10},
    "Stop": {"type": "command", "command": pybin,
             "args": [script, "hook-index"], "async": True},
    "SessionEnd": {"type": "command", "command": pybin,
                   "args": [script, "hook-index"], "async": True},
}
for event, handler in new_handlers.items():
    groups = without_ccmem(event)  # 幂等:先移除旧的 ccmem handler
    groups.append({"hooks": [handler]})
    hooks[event] = groups

# 允许 show 命令直接执行,取回原文时不弹确认框
perms = settings.setdefault("permissions", {})
allow = perms.setdefault("allow", [])
rule = "Bash(%s %s show:*)" % (pybin, script)
if rule not in allow:
    allow.append(rule)

tmp = settings_path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    f.write(json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
os.replace(tmp, settings_path)
print("hooks 已写入 %s" % settings_path)
PYEOF

echo
echo "安装完成。新开一个 Claude Code 会话即可生效;"
echo "调试:$PYTHON_BIN $SCRIPT search \"关键词\""
