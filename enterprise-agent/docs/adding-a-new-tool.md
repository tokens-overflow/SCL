# 新增一个 Tool

## 完整流程（四步）

### 第 1 步：定义参数模型

**禁止把未经验证的字典直接传进工具。** 每个工具必须有自己的 Pydantic 模型。

```python
from pydantic import BaseModel, ConfigDict, Field

class RefundArgs(BaseModel):
    """`refund_payment` 的参数模型。"""

    # extra="forbid" 很重要：模型偶尔会自作主张多塞一个字段，
    # 静默忽略它意味着我们不知道模型其实想做别的事。
    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(min_length=4, max_length=32, description="订单号")
    amount: float = Field(gt=0, le=100_000, description="退款金额（元）")
    reason: str = Field(default="", max_length=200)
```

Pydantic 在这里能保证的是**格式与取值范围**。它保证不了「这个客服有没有
资格退这么多钱」——那是业务规则，由 `app/control/` 负责。

**结构化输出只是必要条件，不是充分条件。**

### 第 2 步：实现工具

```python
class RefundTool(AgentTool):
    """对指定订单发起退款。"""

    name = "refund_payment"                     # 必须与注册表键一致，参与幂等键生成
    description = "对指定订单发起退款。用于客户明确要求退款且订单状态允许的场景。"
    risk_level = RiskLevel.HIGH                 # 静态【基线】，实际风险由 RiskPolicy 按参数算
    required_permissions = {"refund:issue"}
    idempotent = True                           # 写工具必须为 True
    supports_compensation = False               # 退款不可逆 → 必须排在链路最后
    step_type = StepType.WRITE
    service_id = "payment_service"              # 参与三方权限交集
    args_model = RefundArgs
    default_timeout_seconds = 15.0

    async def execute(self, arguments, execution_context) -> ToolExecutionResult:
        assert isinstance(arguments, RefundArgs)
        # 【必须】把幂等键透传给下游——下游的唯一约束才是物理保证
        resp = await payment_client.refund(
            order_id=arguments.order_id,
            amount=arguments.amount,
            idempotency_key=execution_context.idempotency_key,
        )
        return self.build_result(
            execution_context,
            status=ToolExecutionStatus.SUCCESS,
            result={"refund_id": resp.id},
            external_reference_id=resp.id,      # 对账的钥匙之一
        )

    async def query_external_status(self, idempotency_key, execution_context):
        """【所有写工具都必须实现】超时后对账的唯一出路。"""
        found = await payment_client.query_by_idempotency_key(idempotency_key)
        if found:
            return self.build_result(
                execution_context, status=ToolExecutionStatus.SUCCESS,
                external_reference_id=found.id,
            )
        return self.build_result(
            execution_context, status=ToolExecutionStatus.FAILED,
            error_code=ErrorCode.UPSTREAM_UNAVAILABLE,
            error_message="对账确认：退款未发生，可安全重试",
            retryable=True,        # 由【被调方】声明
        )
```

### 第 3 步：注册

```python
# app/actions/registry.py :: register_builtin_tools()
from app.actions.tools.refund import RefundTool

for tool in (QueryCustomerTool(), ApplyDiscountTool(), SendNotificationTool(), RefundTool()):
    if not target.has(tool.name):
        target.register(tool)
```

**显式 import + 显式注册，没有任何动态发现机制。**
自动扫描目录注册看起来优雅，但它意味着「往目录里丢一个文件就能获得执行权」，
在有权限边界的系统里这是一条不该开的口子。

### 第 4 步：加进 Agent 白名单

```python
# app/security/identity.py
"refund_agent": AgentIdentity(
    agent_id="refund_agent",
    permissions=frozenset({"order:read", "refund:issue"}),
    allowed_tools=frozenset({"query_order", "refund_payment"}),
    max_risk_level="HIGH",
    description="处理退款申请。",
),
```

**不在白名单里的工具，即使已注册、即使用户有权限，也不允许这个 Agent 调用。**

---

## 注册时就会被拒绝的三种错误

| 错误 | 报错 |
|------|------|
| 没有 `args_model` | `禁止注册未定义参数模型的工具` |
| 写工具声明为非幂等 | `写操作必须支持幂等，否则重试会产生重复副作用` |
| 工具名重复 | `工具名冲突` |

第三条默认报错而不是静默覆盖——静默覆盖是一类很难查的事故：
两个模块各注册了一个 `send_notification`，线上跑的是哪个取决于 import 顺序。

---

## 决策清单

写工具前先回答这几个问题：

| 问题 | 影响 |
|------|------|
| 有副作用吗？ | `step_type = WRITE/NOTIFY` vs `READ/COMPUTE` |
| 可逆吗？ | `supports_compensation`。**不可逆的必须排在链路最后** |
| 幂等吗？ | 写操作必须是。不是的话先改造下游 |
| 需要什么权限？ | `required_permissions` |
| 背后是哪个服务账号？ | `service_id`（参与三方交集） |
| 失败了能重试吗？ | 在 `execute()` 里如实声明 `retryable` |
| 超时了怎么查真相？ | 实现 `query_external_status()` |
| 风险有多高？ | `risk_level` 是**基线**；实际由 RiskPolicy 按参数算 |

---

## 需要业务规则时：加一条 Policy，不要写进工具

工具应该只关心「怎么做」，不关心「能不能做」。

```python
class RefundLimitPolicy:
    name = "RefundLimitPolicy"

    async def evaluate(self, request) -> PolicyEvaluationResult:
        if request.tool_name != "refund_payment":
            return PolicyEvaluationResult.allow(self.name)   # 职责单一

        amount = float(request.validated_arguments.get("amount", 0))
        if amount > 50_000:
            return PolicyEvaluationResult.deny(
                self.name, "REFUND_EXCEEDS_LIMIT",
                f"退款金额 {amount} 超过单笔上限 50000",
                risk_level=RiskLevel.CRITICAL,
            )
        if amount > 5_000:
            return PolicyEvaluationResult.require_approval(
                self.name, "REFUND_NEEDS_APPROVAL",
                "退款金额超过自助额度，需财务审批",
                ApprovalType.COMPLIANCE,
            )
        return PolicyEvaluationResult.allow(self.name)
```

加进 `build_default_policy_engine()`，位置在 `ParameterPolicy` 之后
（因为它用 `validated_arguments`）、`ApprovalPolicy` 之前。

---

## 测试清单

新工具至少要覆盖：

```python
class TestRefundTool:
    async def test_success(self): ...
    async def test_idempotent_second_call_returns_original(self): ...   # 幂等
    async def test_reconcile_finds_committed(self): ...                 # 对账：已成功
    async def test_reconcile_confirms_not_executed(self): ...           # 对账：未发生
    async def test_permanent_failure_is_not_retryable(self): ...        # 不可重试
    async def test_transient_failure_is_retryable(self): ...            # 可重试
    def test_invalid_arguments_rejected(self): ...                      # 参数校验
    def test_extra_field_rejected(self): ...                            # extra="forbid"
    async def test_compensate_or_declares_unsupported(self): ...        # 补偿或如实拒绝
```

用 `app/actions/tools/fault_injection.py` 可以精确构造超时、瞬时失败、
永久失败和崩溃场景。

---

## 常见错误

| 错误 | 后果 |
|------|------|
| 工具里直接判断权限 | 权限逻辑散落各处，改一条规则要翻遍工具目录 |
| 不透传幂等键 | 重试就是重复副作用 |
| 超时时返回 FAILED | 外部可能已经成功，会被误判并回滚 |
| 一律 `retryable=True` | 业务失败被反复重试，白烧钱 |
| 不实现 `query_external_status` | 一条 UNKNOWN 记录永远查不清 |
| 假装不可逆动作可补偿 | 上层误以为链路可回滚，把它排到中间 |
| 工具里 `session.rollback()` | 连带回滚掉执行器的幂等占位记录（Demo 特有约束，用 SAVEPOINT 解决） |
| 接受自由文本正文 | 等于把「对外说什么」的决定权交给模型 |
