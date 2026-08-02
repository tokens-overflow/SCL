# 执行流：一次请求从进来到落地

## 一、完整链路

```
POST /tasks
  ↓ 入口层：复用上游 X-Trace-Id（少了这步只能看到 Agent 内部，看不到它被谁触发）
创建 Task（CREATED，落库）
  ↓
ContextBuilder：净化 → 脱敏 → 工具过滤 → RAG → 记忆 → 风险提示
  ↓
LLM 解析意图 → 生成 ExecutionPlan
  ↓ 程序强制重排：不可撤回的动作沉底（不依赖模型自觉）
登记全部步骤（PENDING + 幂等键，落库）
  ↓
逐步推进 ──→ 控制层裁决 ──→ 执行 ──→ 落盘 ──→ 下一步
  ↓
汇总（从业务系统查事实）→ LLM 生成回复（数字由程序填）
  ↓
审计 / 日志 / 指标 / Trace
```

## 二、配上状态表看一遍

同一个流程，从状态层的角度看是这样的。
**注意模型只在第 2 步和第 7 步出现过**，其余全是程序：

| 时刻 | 发生了什么 | 状态表的变化 |
|------|-----------|------------|
| T0 | 请求进入，创建任务 | 建 task，所有步骤 PENDING |
| T1 | **模型**理解意图，产出结构化动作 | 不落状态（认知过程不是副作用） |
| T2 | 控制层校验通过 | Step1 → RUNNING，写入 idempotency_key |
| T3 | 调用外部系统，超时 | Step1 → **TIMEOUT**，进对账队列 |
| T4 | 对账查明其实已成功 | Step1 → **SUCCESS**，补写 external_reference_id |
| T5 | 金额超限，命中高风险规则 | Step2 → WAITING_APPROVAL，创建审批单 |
| T6 | 主管点了通过 | Step2 → READY → RUNNING → SUCCESS |
| T7 | **模型**把结果写成给用户的话 | 不落状态；金额等事实由程序套模板 |
| T8 | 输出检查 + 审计落盘 | task → COMPLETED，trace 完整可回放 |

**这张表就是「可运维」的定义**：任何时刻进程崩掉，重启后从这张表都能确定
该干什么——跳过已成功的、对账未知的、等待挂起的、重试可重试的、补偿收不了场的。
整个过程不需要问模型任何一句话。

## 三、任务状态迁移表

代码：`app/runtime/state_machine.py::TASK_TRANSITIONS`

| 从 | 事件 | 到 |
|----|------|-----|
| CREATED | START_PLANNING | PLANNING |
| PLANNING | PLAN_READY | RUNNING |
| RUNNING | STEP_SUCCEEDED | RUNNING |
| RUNNING | NEED_APPROVAL | WAITING_APPROVAL |
| RUNNING | SCHEDULE_RETRY | RETRYING |
| RUNNING | START_COMPENSATION | COMPENSATING |
| RUNNING | ALL_STEPS_DONE | COMPLETED |
| RUNNING | **PARTIALLY_DONE** | **PARTIAL_SUCCESS** |
| WAITING_APPROVAL | APPROVAL_GRANTED | RUNNING |
| WAITING_APPROVAL | APPROVAL_REJECTED | FAILED |
| WAITING_APPROVAL | **ESCALATE_TO_HUMAN** | **MANUAL_REVIEW**（超时回收，**必须有**） |
| RETRYING | RETRY_RESUMED | RUNNING |
| COMPENSATING | COMPENSATION_DONE | FAILED |
| COMPENSATING | FATAL_ERROR | MANUAL_REVIEW（补偿没收干净） |
| 任何非终态 | CANCEL | CANCELLED |

**终态没有任何迁出转换**——否则「终态」这个概念就没有意义了。
测试 `test_terminal_states_have_no_outgoing_transitions` 穷举验证了这一点。

## 四、步骤状态迁移表

代码：`app/runtime/state_machine.py::STEP_TRANSITIONS`

最关键的两组：

```
RUNNING ──EXECUTION_TIMEOUT──→ TIMEOUT      ← 超时不是失败
RUNNING ──EXECUTION_UNKNOWN──→ UNKNOWN      ← 崩溃不是失败

TIMEOUT ──RECONCILED_SUCCESS──→ SUCCESS     ← 只能被【对账】落定
TIMEOUT ──RECONCILED_FAILED───→ FAILED
UNKNOWN ──RECONCILED_SUCCESS──→ SUCCESS
UNKNOWN ──RECONCILED_FAILED───→ FAILED
```

也就是说：**一个未知状态只能被「查明真相」这一个动作落定**，
不能被「重试成功了」或「我猜它失败了」落定。

另一条重要约束：

```
SUCCESS ──START_COMPENSATION──→ COMPENSATING    （唯一允许的迁出）
SUCCESS ──START_EXECUTION─────→ ✗ 非法
```

**一个已经成功的步骤不可能被重新执行**——这是防重复副作用的最后一道语义闸门。

## 五、可推进 vs 阻塞 vs 终态

`app/core/enums.py` 定义了三个集合，`_drive()` 靠它们决定下一步：

```python
ACTIONABLE_STEP_STATUSES = {PENDING, READY, RETRY_SCHEDULED, TIMEOUT, UNKNOWN}
BLOCKING_STEP_STATUSES   = {RUNNING, WAITING_APPROVAL, COMPENSATING}
TERMINAL_STEP_STATUSES   = {SUCCESS, COMPENSATED, SKIPPED}
```

**FAILED 不在任何一个里**，这是刻意的：一个步骤失败之后该怎么办
（重试 / 补偿 / 跳过 / 终止），是在它失败的那一刻就决定好的，
不是留给下一轮循环去猜。

如果把 FAILED 也算作可推进，`_drive` 会反复捞起同一个已经判定为
「不可重试」的步骤，形成死循环——而且第一轮就会撞上非法状态转换。

同理，如果没有 `BLOCKING`，一个还在等审批的任务会被误判为
「所有步骤都处理完了」而提前收尾。

## 六、控制层的四条出口

```
PolicyEngine.evaluate() → PolicyDecision
    ├── DENY             → 拒绝，工具绝不执行，写审计，任务 FAILED
    ├── REQUIRE_APPROVAL → 创建审批单（含确切参数快照），任务 WAITING_APPROVAL
    ├── MANUAL_REVIEW    → 转人工，任务 MANUAL_REVIEW
    └── ALLOW            → 执行
```

聚合优先级（**ALLOW 是最弱的裁决**）：

```
DENY (4) > MANUAL_REVIEW (3) > REQUIRE_APPROVAL (2) > RETRY (1) > ALLOW (0)
```

这个方向不能反——如果实现成「有一条 ALLOW 就放行」，
那么加一条新策略反而可能让系统变得更宽松。

### 审批通过后的降级语义

`approval_granted=True` 时，引擎会把 **REQUIRE_APPROVAL 降级为 ALLOW**，
但**绝不降级 DENY**。

这条规则放在引擎里而不是每条策略各写一遍，因为任何策略都可能要求审批
（`BusinessRulePolicy` 因为金额、`ApprovalPolicy` 因为风险等级），
漏掉任何一条都会导致「批了之后又要批」的死循环——
而这个 Bug 只在审批路径上出现，平时的自助额度流程完全测不到。

## 七、失败分流

| 失败类型 | 例子 | 处理 |
|---------|------|------|
| 明确业务失败 | 余额不足、参数非法 | 不重试，回认知层或如实告知 |
| 技术失败（瞬时） | 下游 5xx、连接失败 | 指数退避 + 抖动，写操作必须带幂等键 |
| 限流 | 429 | 按 Retry-After 退避 |
| **网络超时** | 请求超时 | **不重试**，先对账 |
| **未知执行状态** | 进程崩在执行中途 | **不重试**，先对账 |
| 不可重试失败 | 权限不足、订单状态不允许 | 终止或转人工 |

最容易犯的错误是把「超时」当成「可重试的技术失败」。
超时意味着**结果未知**——如果写操作其实已经成功，重试就是第二笔。

代码：`app/runtime/retry.py::RetryPolicy.decide()`，
第一个分支就是「结果未知 → 不重试」。

## 八、执行到一半

工程上不该出现「执行到 50%」这种状态。
**50% 无法恢复，因为你不知道那 50% 是哪一半。**

正确做法是继续拆，拆到每个子步骤都能被单独记录：

```
生成折扣数据    SUCCESS
写入折扣主表    SUCCESS
写入折扣明细    FAILED      ← 恢复点在这里，清清楚楚
发送通知        PENDING
```

**一条实用的原则：可记录的粒度，就是可恢复的粒度。**
你希望能从哪里重来，就必须在哪里留下记录。
