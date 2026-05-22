"""
工作流编排范式 · 企业级 PR 自动审查流水线
==========================================

场景：开发者提交 Pull Request，流水线自动完成全链路代码审查，
      并将 Markdown 报告写入文件，可直接集成进 CI/CD 系统。

六大 Stage（顺序固定，skip_if 条件可动态跳过）：
  ① 变更解析   → 提取 PR 摘要与风险区域
  ② 安全扫描   → OWASP Top 10 漏洞检测
  ③ 代码质量   → 复杂度 / 命名 / 可维护性   （critical 安全风险时跳过）
  ④ 测试审计   → 测试覆盖完整性评估         （同上）
  ⑤ 依赖审计   → 新增依赖许可证 / CVE 风险  （无新增依赖时跳过）
  ⑥ 报告生成   → 综合 Markdown 报告 + 写入文件

工程化特性（对标生产环境）：
  · PipelineConfig  — 所有可调参数集中管理，可外化为 YAML / 环境变量
  · StageResult     — 每阶段结构化输出（status / score / findings / duration）
  · 指数退避重试    — LLM 网络抖动保护（with_retry）
  · skip_if         — Pipeline 引擎统一执行条件跳过，Stage 本身无感知
  · on_error 策略   — "warn"（记录并继续）/ "abort"（终止流水线）
  · 全程耗时统计    — 每 Stage 独立计时，精度毫秒
  · 纯函数 Stage    — handler(state, cfg) → state，无全局状态，易单测
  · 结构化日志      — 使用标准 logging 模块，生产可替换为 structlog / loguru
"""

import json
import time
import logging
import datetime
import pathlib
import anthropic
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

# ─── 日志 ────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pipeline")

client = anthropic.Anthropic()


# ============================================================
# 枚举 & 基础数据结构
# ============================================================

class StageStatus(str, Enum):
    PASSED  = "PASSED"
    WARNING = "WARNING"
    FAILED  = "FAILED"
    SKIPPED = "SKIPPED"

RISK_EMOJI = {
    "none": "🟢", "low": "🟡", "medium": "🟠", "high": "🔴", "critical": "💀",
}
STATUS_ICON = {
    StageStatus.PASSED:  "✅",
    StageStatus.WARNING: "⚠️ ",
    StageStatus.FAILED:  "❌",
    StageStatus.SKIPPED: "⏭️ ",
}


@dataclass
class StageResult:
    """每个 Stage 执行完毕后追加到 state.stage_results"""
    stage:    str
    status:   StageStatus     = StageStatus.PASSED
    summary:  str             = ""
    findings: list[str]       = field(default_factory=list)
    score:    Optional[float] = None    # 0-10，无评分时为 None
    duration: float           = 0.0    # 秒，由 Pipeline 引擎注入


@dataclass
class PRContext:
    """PR 元信息（流水线的外部输入，对应真实 CI 触发事件）"""
    repo:        str
    pr_number:   int
    author:      str
    title:       str
    description: str
    diff:        str
    new_deps:    list[str] = field(default_factory=list)


@dataclass
class PipelineState:
    """
    贯穿所有 Stage 的唯一可变数据载体。
    Stage 只读取 / 写入这个对象，不使用全局变量。
    """
    pr:             PRContext
    stage_results:  list[StageResult] = field(default_factory=list)
    parsed_summary: str               = ""
    security_risk:  str               = "none"   # none/low/medium/high/critical
    critical_abort: bool              = False    # True 时质量 / 测试 Stage 自动跳过
    quality_score:  float             = 0.0
    test_score:     float             = 0.0
    report_path:    str               = ""


@dataclass
class PipelineConfig:
    """
    可外化为 YAML / 环境变量的全局配置。
    生产中通过 pydantic-settings 或 dynaconf 加载。
    """
    model:             str   = "claude-sonnet-4-20250514"
    max_tokens:        int   = 1500
    retry_attempts:    int   = 3
    retry_base_delay:  float = 1.0      # 秒，指数退避基数
    output_dir:        str   = "./pr_reports"
    quality_threshold: float = 6.0     # 低于此分 → WARNING
    test_threshold:    float = 6.0


# ============================================================
# Pipeline 引擎（纯调度逻辑，不含任何业务代码）
# ============================================================

@dataclass
class PipelineStep:
    name:        str
    description: str
    handler:     Callable[[PipelineState, PipelineConfig], PipelineState]
    skip_if:     Callable[[PipelineState], bool] = field(default=lambda _: False)
    on_error:    str = "warn"     # "warn" | "abort"


def with_retry(fn: Callable, attempts: int, base_delay: float):
    """
    指数退避重试装饰器。
    保护 LLM 调用免受网络抖动影响，生产中可换成 tenacity 库。
    """
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if i == attempts - 1:
                raise
            wait = base_delay * (2 ** i)
            log.warning(f"  [retry {i+1}/{attempts-1}] 等待 {wait:.1f}s — {exc}")
            time.sleep(wait)


def run_pipeline(
    pr: PRContext,
    steps: list[PipelineStep],
    cfg: PipelineConfig,
) -> PipelineState:
    """
    Pipeline 执行引擎。
    ─────────────────────────────────────────────────────────
    职责：遍历 steps，统一处理 skip_if / on_error / 计时 / 异常捕获。
    不包含任何业务逻辑，业务逻辑全在各 Stage handler 里。

    数据流：
      PipelineState  ──(handler)──►  PipelineState（追加 StageResult）
                     ◄─(engine)───   duration 注入
    """
    state = PipelineState(pr=pr)
    pipeline_start = time.perf_counter()

    log.info("=" * 65)
    log.info(f"  PR 自动审查流水线  |  {pr.repo}  PR #{pr.pr_number}")
    log.info(f"  {pr.title}")
    log.info(f"  作者: {pr.author}  |  新增依赖: {len(pr.new_deps)} 个")
    log.info("=" * 65)

    for step in steps:
        # ── 条件跳过（skip_if 由 Pipeline 评估，Stage 本身无感知）──
        if step.skip_if(state):
            state.stage_results.append(StageResult(
                stage=step.description,
                status=StageStatus.SKIPPED,
                summary="条件跳过",
            ))
            log.info(f"\n  ⏭️  [{step.name}] 跳过 — {step.description}")
            continue

        log.info(f"\n  ▶ [{step.name}] {step.description}")
        stage_start = time.perf_counter()

        try:
            state = step.handler(state, cfg)
            duration = time.perf_counter() - stage_start
            # Pipeline 引擎注入耗时（Stage 内部不需要关心）
            if state.stage_results:
                state.stage_results[-1].duration = duration
            log.info(f"    ✓ 完成  [{duration:.1f}s]")

        except Exception as exc:
            duration = time.perf_counter() - stage_start
            state.stage_results.append(StageResult(
                stage=step.description,
                status=StageStatus.FAILED,
                summary=f"执行异常：{exc}",
                duration=duration,
            ))
            log.error(f"    ✗ 失败：{exc}")

            if step.on_error == "abort":
                log.error("    ⛔ on_error=abort，终止流水线")
                break
            else:
                log.warning("    ↩ on_error=warn，继续后续 Stage")

    total = time.perf_counter() - pipeline_start
    log.info(f"\n  ✅ 流水线完成  总耗时 {total:.1f}s")
    return state


# ============================================================
# LLM 调用封装（所有 Stage 共用，便于集中替换模型或加缓存）
# ============================================================

def llm_call(system: str, user: str, cfg: PipelineConfig) -> str:
    resp = client.messages.create(
        model=cfg.model,
        max_tokens=cfg.max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return resp.content[0].text


def safe_json(text: str) -> dict:
    """容错 JSON 解析，移除 LLM 可能输出的 markdown 代码块包裹"""
    cleaned = text.strip().removeprefix("```json").removeprefix("```")
    cleaned = cleaned.removesuffix("```").strip()
    try:
        return json.loads(cleaned)
    except Exception:
        return {}


# ============================================================
# Prompts（集中管理，便于版本控制和 A/B 测试）
# ============================================================

PARSE_PROMPT = """你是一个代码变更分析专家。

分析 PR 的代码差异（diff），提取结构化信息。

输出 JSON（只输出 JSON，不要其他文字）：
{
  "changed_files": ["变更文件路径"],
  "change_types":  ["功能新增/Bug修复/重构/配置/测试/文档"],
  "complexity":    "low/medium/high",
  "risk_areas":    ["值得关注的风险区域（简短描述）"],
  "summary":       "2-3句话概括本次变更内容和潜在影响"
}"""

SECURITY_PROMPT = """你是一个应用安全专家（SAST 代码审查）。

检查代码中的安全漏洞，重点关注 OWASP Top 10：
- 硬编码凭证 / API Key / 密码
- 注入漏洞（SQL / 命令 / LDAP / XML）
- 加密缺陷（弱算法 MD5/SHA1、关闭 SSL 验证）
- 不安全的反序列化（pickle / yaml.load）
- 访问控制 / 权限校验缺失
- 敏感数据明文存储或传输

输出 JSON（只输出 JSON）：
{
  "risk_level": "none/low/medium/high/critical",
  "vulnerabilities": [
    {
      "type":        "漏洞类型（如 Hardcoded Secret）",
      "location":    "文件名或函数名",
      "severity":    "high/medium/low",
      "description": "问题描述（一句话）",
      "fix":         "修复建议（一句话）"
    }
  ]
}"""

QUALITY_PROMPT = """你是一个代码质量专家。

评估代码可维护性，检查以下维度：
- 函数职责单一性（SRP）
- 命名清晰度（函数 / 变量 / 常量）
- 错误处理完整性（异常捕获、返回值校验）
- 代码重复（DRY 原则）
- 圈复杂度（嵌套层级过深）
- 可测试性（是否便于 Mock / 注入依赖）

输出 JSON（只输出 JSON）：
{
  "score": 1-10,
  "strengths": ["优点"],
  "issues": [
    {
      "severity":    "high/medium/low",
      "location":    "位置",
      "description": "问题",
      "suggestion":  "改进建议"
    }
  ]
}"""

TEST_PROMPT = """你是一个测试工程师。

审查测试代码的覆盖完整性：
- 核心业务逻辑是否覆盖
- 边界条件 / 异常路径是否有测试
- 测试命名是否清晰表达意图
- 是否过度 Mock 导致测试意义下降
- 断言是否足够严格（不只是 assert result is not None）

输出 JSON（只输出 JSON）：
{
  "score":               1-10,
  "has_tests":           true/false,
  "coverage_assessment": "覆盖情况描述（一句话）",
  "missing_scenarios":   ["缺失的关键测试场景"],
  "issues":              ["其他质量问题"]
}"""

DEP_PROMPT = """你是一个开源依赖安全专家。

评估新增依赖的风险（基于你知识截止日期内的信息）：
- 是否知名、活跃维护（有无长期无更新迹象）
- 是否有已知 CVE 高危漏洞
- 许可证兼容性（MIT / Apache-2.0 对商业友好；GPL / AGPL 需法务确认）
- 包名是否有拼写劫持风险（typosquatting）

输出 JSON（只输出 JSON）：
{
  "overall_risk": "none/low/medium/high",
  "assessments": [
    {
      "package": "包名==版本",
      "risk":    "none/low/medium/high",
      "license": "许可证名称",
      "note":    "一句话备注"
    }
  ]
}"""

REPORT_PROMPT = """你是一个技术评审 Lead，正在撰写 PR 审查报告。

要求：
- 面向开发者，重点突出，避免废话
- 必须修复的问题（blocking）和建议改进（non-blocking）分开列出
- 给出明确合并建议：✅ 建议合并 / ⚠️ 修复后合并 / ❌ 不建议合并
- 语言：中文，Markdown 格式

报告结构：
# PR 审查报告 · #{pr_number}

## 📋 概览
（Markdown 表格：安全风险 / 代码质量 / 测试覆盖 / 依赖风险 / 合并建议）

## 📝 变更摘要

## 🔒 安全审查
（分漏洞类型列出，无则写"无安全风险"）

## 🧹 代码质量

## 🧪 测试审计

## 📦 依赖审计
（若跳过则写"本次 PR 无新增依赖"）

## ✅ 合并建议与行动项
### 必须修复（Blocking）
### 建议改进（Non-blocking）
### 结论"""


# ============================================================
# Stage 实现（每个都是纯函数，(state, cfg) → state）
# ============================================================

def stage_parse(state: PipelineState, cfg: PipelineConfig) -> PipelineState:
    raw = with_retry(
        lambda: llm_call(
            PARSE_PROMPT,
            f"PR 标题：{state.pr.title}\n描述：{state.pr.description}\n\nDiff：\n{state.pr.diff}",
            cfg,
        ),
        cfg.retry_attempts, cfg.retry_base_delay,
    )
    data = safe_json(raw)
    state.parsed_summary = data.get("summary", "")
    risk_areas = data.get("risk_areas", [])

    log.info(f"    变更类型：{data.get('change_types', [])}")
    log.info(f"    复杂度：{data.get('complexity', '?')}  风险区：{risk_areas}")

    state.stage_results.append(StageResult(
        stage="变更解析",
        status=StageStatus.PASSED,
        summary=state.parsed_summary,
        findings=risk_areas,
    ))
    return state


def stage_security(state: PipelineState, cfg: PipelineConfig) -> PipelineState:
    raw = with_retry(
        lambda: llm_call(
            SECURITY_PROMPT,
            f"PR：{state.pr.title}\n\nDiff：\n{state.pr.diff}",
            cfg,
        ),
        cfg.retry_attempts, cfg.retry_base_delay,
    )
    data = safe_json(raw)
    vulns = data.get("vulnerabilities", [])
    risk  = data.get("risk_level", "none")
    state.security_risk = risk

    findings = [
        f"[{v.get('severity','?').upper()}][{v.get('type','')}] "
        f"{v.get('location','')} — {v.get('description','')} | Fix: {v.get('fix','')}"
        for v in vulns
    ]
    for f in findings:
        log.info(f"    {f}")

    if risk == "critical":
        state.critical_abort = True
        status = StageStatus.FAILED
    elif risk in ("high", "medium"):
        status = StageStatus.WARNING
    else:
        status = StageStatus.PASSED

    state.stage_results.append(StageResult(
        stage="安全扫描",
        status=status,
        summary=f"风险等级：{RISK_EMOJI.get(risk,'')} {risk.upper()}，发现 {len(vulns)} 个漏洞",
        findings=findings,
    ))
    return state


def stage_quality(state: PipelineState, cfg: PipelineConfig) -> PipelineState:
    raw = with_retry(
        lambda: llm_call(
            QUALITY_PROMPT,
            f"PR：{state.pr.title}\n\nDiff：\n{state.pr.diff}",
            cfg,
        ),
        cfg.retry_attempts, cfg.retry_base_delay,
    )
    data = safe_json(raw)
    state.quality_score = float(data.get("score", 0))
    issues = data.get("issues", [])
    findings = [
        f"[{i.get('severity','?').upper()}] {i.get('location','')} — "
        f"{i.get('description','')} → {i.get('suggestion','')}"
        for i in issues
    ]

    log.info(f"    质量评分：{state.quality_score:.1f}/10  问题数：{len(issues)}")
    status = StageStatus.PASSED if state.quality_score >= cfg.quality_threshold else StageStatus.WARNING

    state.stage_results.append(StageResult(
        stage="代码质量",
        status=status,
        summary=f"评分 {state.quality_score:.1f}/10",
        findings=findings,
        score=state.quality_score,
    ))
    return state


def stage_test(state: PipelineState, cfg: PipelineConfig) -> PipelineState:
    raw = with_retry(
        lambda: llm_call(
            TEST_PROMPT,
            f"PR：{state.pr.title}\n\nDiff：\n{state.pr.diff}",
            cfg,
        ),
        cfg.retry_attempts, cfg.retry_base_delay,
    )
    data = safe_json(raw)
    state.test_score = float(data.get("score", 0))
    has_tests = data.get("has_tests", False)
    missing   = data.get("missing_scenarios", [])

    log.info(f"    测试评分：{state.test_score:.1f}/10  有测试：{has_tests}")
    status = StageStatus.PASSED if state.test_score >= cfg.test_threshold else StageStatus.WARNING

    state.stage_results.append(StageResult(
        stage="测试审计",
        status=status,
        summary=(
            f"评分 {state.test_score:.1f}/10  "
            f"{'有' if has_tests else '无'}测试  "
            f"缺失场景 {len(missing)} 个"
        ),
        findings=missing + data.get("issues", []),
        score=state.test_score,
    ))
    return state


def stage_dep(state: PipelineState, cfg: PipelineConfig) -> PipelineState:
    raw = with_retry(
        lambda: llm_call(
            DEP_PROMPT,
            "新增依赖列表：\n" + "\n".join(f"- {d}" for d in state.pr.new_deps),
            cfg,
        ),
        cfg.retry_attempts, cfg.retry_base_delay,
    )
    data = safe_json(raw)
    assessments  = data.get("assessments", [])
    overall_risk = data.get("overall_risk", "none")
    findings = [
        f"[{a.get('risk','?').upper()}] {a.get('package','')} "
        f"({a.get('license','?')}) — {a.get('note','')}"
        for a in assessments
    ]
    for f in findings:
        log.info(f"    {f}")

    status = StageStatus.WARNING if overall_risk in ("medium", "high") else StageStatus.PASSED

    state.stage_results.append(StageResult(
        stage="依赖审计",
        status=status,
        summary=f"审计 {len(assessments)} 个依赖，整体风险：{overall_risk}",
        findings=findings,
    ))
    return state


def stage_report(state: PipelineState, cfg: PipelineConfig) -> PipelineState:
    # 把所有 StageResult 格式化成 Markdown 供 LLM 汇总
    stages_md = "\n\n".join([
        f"### {r.stage}\n"
        f"状态：{STATUS_ICON.get(r.status,'')} {r.status.value}"
        + (f"  |  评分：{r.score:.1f}/10" if r.score is not None else "") + "\n"
        f"摘要：{r.summary}\n"
        "发现：\n" + (
            "\n".join(f"- {f}" for f in r.findings) if r.findings else "- 无"
        )
        for r in state.stage_results
    ])

    report_md = with_retry(
        lambda: llm_call(
            REPORT_PROMPT.replace("{pr_number}", str(state.pr.pr_number)),
            (
                f"仓库：{state.pr.repo}  |  PR #{state.pr.pr_number}\n"
                f"标题：{state.pr.title}\n"
                f"作者：{state.pr.author}\n"
                f"描述：{state.pr.description}\n\n"
                f"各阶段审查结果：\n\n{stages_md}"
            ),
            cfg,
        ),
        cfg.retry_attempts, cfg.retry_base_delay,
    )

    # 写入文件（生产中可同时推送到 GitHub PR Review API）
    out_dir = pathlib.Path(cfg.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    report_file = out_dir / f"pr_{state.pr.pr_number}_{ts}.md"
    report_file.write_text(report_md, encoding="utf-8")
    state.report_path = str(report_file)

    state.stage_results.append(StageResult(
        stage="报告生成",
        status=StageStatus.PASSED,
        summary=f"已写入 {state.report_path}",
    ))

    # 打印汇总表
    log.info("\n  " + "─" * 63)
    log.info(f"  汇总  |  PR #{state.pr.pr_number}  {state.pr.title}")
    log.info("  " + "─" * 63)
    for r in state.stage_results:
        icon    = STATUS_ICON.get(r.status, "")
        score_s = f"  分={r.score:.1f}" if r.score is not None else ""
        dur_s   = f"  [{r.duration:.1f}s]" if r.duration else ""
        log.info(f"  {icon} {r.stage:<10} {r.status.value:<10}{score_s}{dur_s}")
    log.info("  " + "─" * 63)
    log.info(f"\n{report_md}")

    return state


# ============================================================
# Pipeline 定义
# Stage 列表 = 产品需求的代码化表达，顺序调整 / 新增 Stage 只改这里
# ============================================================

PIPELINE_STEPS: list[PipelineStep] = [
    PipelineStep(
        name="parse",
        description="代码变更解析",
        handler=stage_parse,
        on_error="warn",
    ),
    PipelineStep(
        name="security",
        description="安全漏洞扫描（OWASP Top 10）",
        handler=stage_security,
        on_error="warn",    # 扫描失败只记 WARNING，报告仍会生成
    ),
    PipelineStep(
        name="quality",
        description="代码质量检查",
        handler=stage_quality,
        skip_if=lambda s: s.critical_abort,   # Critical 安全风险时自动跳过
        on_error="warn",
    ),
    PipelineStep(
        name="test",
        description="测试覆盖审计",
        handler=stage_test,
        skip_if=lambda s: s.critical_abort,   # 同上
        on_error="warn",
    ),
    PipelineStep(
        name="dep",
        description="依赖风险评估",
        handler=stage_dep,
        skip_if=lambda s: not s.pr.new_deps,  # 无新增依赖时跳过
        on_error="warn",
    ),
    PipelineStep(
        name="report",
        description="综合报告生成",
        handler=stage_report,
        on_error="abort",   # 报告是最终交付物，失败则终止
    ),
]


# ============================================================
# 演示数据（含多类典型安全问题，便于验证各 Stage）
# ============================================================

DEMO_PR = PRContext(
    repo="acme-corp/payment-service",
    pr_number=847,
    author="zhang.san",
    title="feat: 新增第三方支付渠道接入（微信支付 v3）",
    description=(
        "接入微信支付 v3 API，支持 JSAPI / Native / H5 三种支付方式，"
        "新增异步通知处理和基础重试逻辑"
    ),
    new_deps=["wechatpayv3==1.3.2", "cryptography==41.0.5"],
    diff="""
diff --git a/payment/wechat.py b/payment/wechat.py
new file mode 100644
--- /dev/null
+++ b/payment/wechat.py
@@ -0,0 +1,72 @@
+import requests, hashlib, json, pickle
+
+# !! 硬编码密钥（应从环境变量读取）
+WECHAT_SECRET = "sk-live-abc123XYZ789secretKey"
+MERCHANT_ID   = "1234567890"
+
+class WechatPay:
+    def __init__(self):
+        self.base_url = "https://api.mch.weixin.qq.com"
+        self.session  = requests.Session()
+
+    def create_order(self, order_data: dict) -> dict:
+        sign_str  = "&".join(f"{k}={v}" for k, v in order_data.items())
+        sign_str += f"&key={WECHAT_SECRET}"
+        sign = hashlib.md5(sign_str.encode()).hexdigest()   # 弱哈希算法
+        order_data["sign"] = sign
+        resp = self.session.post(
+            f"{self.base_url}/v3/pay/transactions/jsapi",
+            json=order_data,
+            verify=False,    # 关闭 SSL 验证
+        )
+        return resp.json()
+
+    def handle_notify(self, raw_body: bytes) -> dict:
+        return pickle.loads(raw_body)    # 不安全的反序列化
+
+    def query_order(self, order_id: str) -> dict:
+        url = (f"{self.base_url}/v3/pay/transactions/id/{order_id}"
+               f"?mchid={MERCHANT_ID}")
+        return self.session.get(url).json()
+
+    def refund(self, order_id, amount, reason):
+        # 未校验 amount 为正数，存在退款金额篡改风险
+        payload = {
+            "transaction_id": order_id,
+            "out_refund_no":  f"refund_{order_id}",
+            "amount": {"refund": amount, "currency": "CNY"},
+        }
+        return self.session.post(
+            f"{self.base_url}/v3/refund/domestic/refunds",
+            json=payload,
+        ).json()

diff --git a/payment/tests/test_wechat.py b/payment/tests/test_wechat.py
new file mode 100644
--- /dev/null
+++ b/payment/tests/test_wechat.py
@@ -0,0 +1,10 @@
+from payment.wechat import WechatPay
+
+def test_create_order_happy_path():
+    pay = WechatPay()
+    result = pay.create_order({"amount": 100, "openid": "oXXX"})
+    assert result is not None    # 断言太弱，仅覆盖 happy path

diff --git a/requirements.txt b/requirements.txt
--- a/requirements.txt
+++ b/requirements.txt
@@ -4,3 +4,5 @@
 requests==2.31.0
 pydantic==2.5.0
+wechatpayv3==1.3.2
+cryptography==41.0.5
""",
)


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    cfg = PipelineConfig(
        output_dir="./pr_reports",
        quality_threshold=6.0,
        test_threshold=6.0,
    )
    final_state = run_pipeline(DEMO_PR, PIPELINE_STEPS, cfg)
    print(f"\n✅ 完整报告已写入：{final_state.report_path}")
