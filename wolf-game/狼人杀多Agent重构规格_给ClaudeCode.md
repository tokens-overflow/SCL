# 狼人杀多 Agent 系统 —— 重构规格说明书（交给 Claude Code）

> **给 Claude Code 的总指令**
> 这是一个已能运行的狼人杀 AI 对战项目（Python，纯 Anthropic API 编排，无 agent 框架）。
> 你的任务是按本文件描述的目标架构对现有代码进行**重构**，不是从零重写。
>
> 执行顺序：
> 1. 先通读现有代码库，输出一份《现状审查报告》：列出现有模块、它们对应本规格的哪一部分、以及与目标架构的差距。
> 2. 与我确认审查报告后，按「第 6 节 重构任务清单」逐项改造。
> 3. 每完成一个模块，运行「第 7 节 验收检查」对应项并报告结果。
> 4. 保持纯 Python + Anthropic API，不要引入 LangGraph / AutoGen 等框架。

---

## 1. 系统目标

一个 12 人标准板狼人杀（预言家 / 女巫 / 守卫 / 猎人 + 4 狼 + 4 民）的多 agent 自动对战系统：

- 1 个**裁判 Agent**：纯规则执行，持有全局真相，驱动流程。
- 12 个**玩家 Agent**：各自独立、记忆隔离，只能基于自己的视角博弈。
- 重构后必须满足：**信息严格隔离、记忆结构化、流程状态机化、单局可复现**。

板子与规则细节见附录 A（沿用此前确定的标准 12 人屠边板）。

---

## 2. 目标架构

```
              ┌────────────────────┐
              │   GameEngine        │  顶层循环,驱动状态机
              └─────────┬──────────┘
                        │
              ┌─────────▼──────────┐
              │  ModeratorAgent     │  裁判:持有 GroundTruth
              │  - 规则校验          │  唯一可读写真相层
              │  - 信息分发          │
              │  - 死亡/胜负结算     │
              └─────────┬──────────┘
            按权限分发 context (信息隔离边界)
   ┌────────────────────┼────────────────────┐
   ▼                    ▼                    ▼
PlayerAgent#1       PlayerAgent#2  ...   PlayerAgent#12
 - PrivateMemory     - PrivateMemory       (每个独立 LLM 会话)
 - 决策 / 发言        - 决策 / 发言
```

三层记忆,严格隔离:

| 记忆层 | 持有者 | 读写权限 | 内容 |
|--------|--------|----------|------|
| GroundTruth | 仅 Moderator | 仅 Moderator 读写 | 全部身份、每晚真实动作、药剂数、守卫上一夜目标 |
| PublicLog | 所有 agent | Moderator 写,玩家只读 | 出局座位号、发言全文、投票记录、跳身份、开枪结果 |
| PrivateMemory | 各玩家自己 | Moderator 发放事实,玩家写 beliefs | 自身身份、查验/刀口/狼队友、推理 beliefs |

> **核心红线**：PublicLog 绝不写死因（只写"X 号出局"）。PrivateMemory 物理隔离——每个玩家 agent 独立的 conversation/message 列表,不共享对象。

---

## 3. 数据结构契约（目标）

重构后应存在以下数据类（用 `dataclass` 或 `pydantic`,Claude Code 按现有代码风格选）。

```python
# ---- 真相层,仅 Moderator 持有 ----
@dataclass
class GroundTruth:
    roles: dict[int, str]          # seat -> "wolf"/"seer"/"witch"/"guard"/"hunter"/"villager"
    alive: set[int]                # 存活座位
    wolves: list[int]              # 狼座位列表
    witch_antidote: bool           # 解药是否还在
    witch_poison: bool             # 毒药是否还在
    guard_last_target: int | None  # 守卫上一夜目标(连守校验)
    night_actions: list[dict]      # 每晚原始动作记录
    rng_seed: int                  # 复现用随机种子

# ---- 公共层,所有 agent 只读副本 ----
@dataclass
class PublicLogEntry:
    phase: str                     # "N1","D1","N2",...
    deaths: list[int]              # 本阶段出局座位(不含死因)
    speeches: dict[int, str]       # seat -> 发言全文
    votes: dict[int, int]          # voter_seat -> target_seat
    claims: dict[int, str]         # seat -> 公开跳的身份
    hunter_shot: tuple[int,int] | None  # (猎人seat, 被带走seat)

# ---- 私有层,每个玩家 agent 独有 ----
@dataclass
class PrivateMemory:
    seat: int
    role: str
    teammates: list[int]           # 仅狼人非空
    facts: list[dict]              # append-only 事实: 查验结果/刀口/自身动作
    beliefs: dict[int, dict]       # seat -> {"suspicion": float, "reason": str}
```

**不变量（重构必须保证）**：
- `facts` 与 `PublicLogEntry` 一旦写入**只追加,不可篡改**。
- 玩家 agent 永远拿不到 `GroundTruth` 的引用。
- `beliefs` 是玩家每轮唯一可改写的记忆,且应"增量更新"而非整体重写。

---

## 4. Agent 接口契约（目标）

```python
class ModeratorAgent:
    def setup_game(self, seed: int) -> None: ...
        # 随机分配身份/座位,初始化三层记忆,私发身份给各玩家

    def run_night(self, night_no: int) -> list[int]:
        # 按 守卫→狼人→女巫→预言家 顺序索要动作
        # 校验合法性(非法则要求重选),写真相层,返回出局名单

    def run_day(self, day_no: int) -> str | None:
        # 公布出局→猎人开枪→发言轮→投票→结算
        # 返回胜负结果或 None(继续)

    def build_context(self, seat: int, event: dict) -> list[dict]:
        # 关键函数:为某玩家拼装本轮 LLM context
        # = 静态规则 + 身份块 + PublicLog + 该玩家PrivateMemory + 本轮事件
        # 必须只注入该 seat 有权看到的信息

    def check_action(self, seat: int, action: dict) -> bool:
        # 合法性校验,见附录 B 清单

    def settle_deaths(self, night_actions: dict) -> list[int]:
        # 按死亡规则结算(含同守同救判死)

    def check_victory(self) -> str | None:
        # 屠边判定,每次死亡后调用


class PlayerAgent:
    def __init__(self, seat: int, llm_client): ...
        # 持有独立的 message 历史,即独立 LLM 会话

    def act(self, context: list[dict], action_type: str) -> dict:
        # action_type: "guard"/"kill"/"witch"/"seer"/"speech"/"vote"/"hunter_shot"
        # 返回结构化动作,如 {"action":"kill","target":3}
        # 同时返回更新后的 beliefs,由 Moderator 写回其 PrivateMemory
```

---

## 5. 一局执行流程（目标状态机）

`GameEngine` 应实现如下循环,`ModeratorAgent` 是执行体：

```
setup_game(seed)
loop:
    deaths = run_night(n)          # 见下
    if check_victory(): break
    result = run_day(n)            # 见下
    if result: break
    n += 1

# run_night 内部
for role in [guard, wolves, witch, seer]:
    ctx = build_context(seat, event)
    action = player.act(ctx, role)
    while not check_action(seat, action):   # 非法重选
        action = player.act(ctx_with_error, role)
    record to GroundTruth
deaths = settle_deaths(...)
write deaths to PublicLog (仅座位号)

# run_day 内部
broadcast deaths to all alive players (PublicLog)
if hunter died and can_shoot: handle hunter_shot
for seat in seating_order(alive):
    ctx = build_context(seat, {"type":"speech_turn"})
    speech = player.act(ctx, "speech")
    append speech to PublicLog   # 后续发言者能看到
collect votes -> resolve (含平票PK重投)
write vote result + 出局 to PublicLog
if hunter voted out and can_shoot: handle hunter_shot
return check_victory()
```

**狼人协同**：夜晚 4 个狼 agent 各自收到"狼队友名单 + 狼频道历史发言",依次在狼频道发言,最后由 Moderator 对 4 个刀杀目标做多数决（或指定狼队长 agent 汇总）。狼频道发言进各狼的 PrivateMemory,**不进 PublicLog**。

---

## 6. 重构任务清单（按此顺序执行）

> 每项先在现有代码里定位对应实现,能复用则复用,不符合契约则改造。

- [ ] **T1 记忆三层化**：把现有"记忆"拆成 GroundTruth / PublicLog / PrivateMemory 三个独立结构,确保玩家 agent 无任何途径访问 GroundTruth。检查现有代码是否有上帝视角泄露并修复。
- [ ] **T2 信息隔离**：每个 PlayerAgent 持有独立 message 历史。审查 `build_context`,确保只注入该 seat 有权看到的信息;PublicLog 写入处禁止写死因。
- [ ] **T3 裁判状态机化**：把流程整理成第 5 节的状态机,夜晚顺序严格 守卫→狼人→女巫→预言家。
- [ ] **T4 动作结构化**：玩家 agent 输出统一为结构化 dict（带 JSON schema 约束 prompt）,而非自由文本解析。Moderator 用 `check_action` 校验,非法重选。
- [ ] **T5 死亡/胜负结算独立化**：`settle_deaths` 与 `check_victory` 抽成纯函数,覆盖同守同救判死、被毒不可开枪、屠边判定。
- [ ] **T6 beliefs 记忆**：玩家每轮 act 后输出增量更新的 beliefs,Moderator 写回其 PrivateMemory,下一轮注入 context,保证推理连贯。
- [ ] **T7 context 压缩**：发言历史过长时做摘要;但出局记录/投票记录/查验结果等关键事实不得压缩失真。
- [ ] **T8 可复现**：引入 `rng_seed`,同一 seed + 同一 LLM 设定下单局流程可复现;全程日志落盘（含每个 agent 的真实 context,便于复盘）。

---

## 7. 验收检查

- **V1 信息隔离**：构造测试,断言任一 PlayerAgent 的 context 中不含其他人的身份、不含死因。
- **V2 规则正确性**：单元测试覆盖——同守同救判死、女巫非首夜不可自救、女巫同晚不可解+毒、守卫不可连守、猎人被毒不可开枪、狼人不可自刀。
- **V3 状态机**：模拟一局,断言夜晚动作顺序、白天流程顺序正确。
- **V4 胜负判定**：构造屠神边 / 屠民边 / 杀光狼三种终局,断言判定正确。
- **V5 复现**：同 seed 跑两次,断言 GroundTruth 与 PublicLog 一致（在 LLM 输出固定/mock 的前提下）。
- **V6 端到端**：完整跑通一局且无异常,日志可复盘。

---

## 附录 A：规则速查（标准 12 人屠边板）

- 配置：狼 4 / 预言家 1 / 女巫 1 / 守卫 1 / 猎人 1 / 平民 4。
- 胜负（屠边）：狼全灭→好人胜；4 神全灭 或 4 民全灭→狼胜。
- 夜晚顺序：守卫 → 狼人 → 女巫 → 预言家。
- 预言家：每晚查 1 人,只返回好人/狼人。
- 女巫：解药毒药各 1,全局各 1 次;同晚不可并用;仅首夜可自救;仅首夜可见刀口。
- 守卫：守 1 人(可守己),不可连守同一目标;同守同救→该玩家仍死。
- 猎人：被刀/被票可开枪,被毒不可开枪。
- 死亡结算：被刀且(无守无救)→死;仅守或仅救→活;同守同救→死;被毒→死。
- 白天：公布出局(只座位号)→猎人开枪→顺序发言→投票(平票PK重投,再平票无人出局)。

## 附录 B：合法性校验清单（check_action）

- 目标必须为存活玩家。
- 预言家不可查自己。
- 女巫：解药/毒药各全局一次;同晚不可解+毒并用;非首夜不可自救;解药目标必须是当晚狼刀目标。
- 守卫：不可连续两晚守同一目标(首夜不限)。
- 狼人：不可自刀;目标须为存活非狼人或空刀。
- 猎人：被毒死时禁止开枪。
- 投票：每人至多 1 票,目标须存活。
