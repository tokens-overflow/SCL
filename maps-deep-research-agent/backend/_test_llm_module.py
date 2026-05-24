"""LLM 模块静默测试 —— 不需要真实 API Key，覆盖所有关键路径。"""

import asyncio
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))

PASS = "\033[32m PASS\033[0m"
FAIL = "\033[31m FAIL\033[0m"
results: list[tuple[str, bool, str]] = []


def case(name: str):
    def decorator(fn):
        try:
            if asyncio.iscoroutinefunction(fn):
                asyncio.run(fn())
            else:
                fn()
            results.append((name, True, ""))
        except Exception:
            results.append((name, False, traceback.format_exc()))
        return fn
    return decorator


# ─────────────────────────────────────────────────────────────
# 1. 基础导入
# ─────────────────────────────────────────────────────────────

@case("导入 llm 包")
def test_import_llm():
    from src.llm import LLMClient, LLMUsage, build_llm_client, get_active_provider_info
    assert LLMClient and LLMUsage and build_llm_client and get_active_provider_info

@case("llm.client 已移除（不再暴露 DeepSeekLLMClient）")
def test_client_removed():
    import importlib, importlib.util
    spec = importlib.util.find_spec("src.llm.client")
    assert spec is None, "src.llm.client 仍然存在，应已删除"

@case("导入 adapters 包")
def test_import_adapters():
    from src.llm.adapters import AnthropicAdapter, BedrockAdapter, OpenAICompatAdapter
    assert AnthropicAdapter and BedrockAdapter and OpenAICompatAdapter


# ─────────────────────────────────────────────────────────────
# 2. safe_parse_json
# ─────────────────────────────────────────────────────────────

@case("safe_parse_json：标准 JSON")
def test_parse_json_normal():
    from src.llm.adapters._utils import safe_parse_json
    assert safe_parse_json('{"a": 1}') == {"a": 1}

@case("safe_parse_json：带前缀文字的 JSON（容错）")
def test_parse_json_prefix():
    from src.llm.adapters._utils import safe_parse_json
    result = safe_parse_json('Here is the result:\n{"key": "val"}')
    assert result == {"key": "val"}

@case("safe_parse_json：数组")
def test_parse_json_array():
    from src.llm.adapters._utils import safe_parse_json
    assert safe_parse_json('[1,2,3]') == [1, 2, 3]

@case("safe_parse_json：彻底无效返回空 dict")
def test_parse_json_invalid():
    from src.llm.adapters._utils import safe_parse_json
    assert safe_parse_json("not json at all") == {}


# ─────────────────────────────────────────────────────────────
# 3. YAML 加载与环境变量展开
# ─────────────────────────────────────────────────────────────

SAMPLE_YAML = """
active: ds-flash
providers:
  ds-flash:
    type: deepseek
    model: deepseek-v4-flash
    api_key: ${TEST_DS_KEY}
    base_url: https://api.deepseek.com
  openai:
    type: openai
    model: gpt-4o
    api_key: ${TEST_OAI_KEY}
"""

@case("load_llm_config：正确解析 YAML 结构")
def test_load_yaml_structure():
    from src.llm.loader import load_llm_config
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(SAMPLE_YAML)
        tmp = f.name
    try:
        cfg = load_llm_config(tmp)
        assert cfg["active"] == "ds-flash"
        assert "ds-flash" in cfg["providers"]
        assert cfg["providers"]["ds-flash"]["model"] == "deepseek-v4-flash"
    finally:
        os.unlink(tmp)

@case("load_llm_config：${VAR} 环境变量替换")
def test_load_yaml_env_expand():
    from src.llm.loader import load_llm_config
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(SAMPLE_YAML)
        tmp = f.name
    os.environ["TEST_DS_KEY"] = "sk-test-deepseek"
    os.environ["TEST_OAI_KEY"] = "sk-test-openai"
    try:
        cfg = load_llm_config(tmp)
        assert cfg["providers"]["ds-flash"]["api_key"] == "sk-test-deepseek"
        assert cfg["providers"]["openai"]["api_key"] == "sk-test-openai"
    finally:
        os.unlink(tmp)
        del os.environ["TEST_DS_KEY"]
        del os.environ["TEST_OAI_KEY"]

@case("load_llm_config：缺失 VAR 展开为空串")
def test_load_yaml_missing_env():
    from src.llm.loader import load_llm_config
    os.environ.pop("TEST_DS_KEY", None)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(SAMPLE_YAML)
        tmp = f.name
    try:
        cfg = load_llm_config(tmp)
        assert cfg["providers"]["ds-flash"]["api_key"] == ""
    finally:
        os.unlink(tmp)

@case("get_active_provider_info：返回 active/type/model")
def test_get_provider_info():
    from src.llm.loader import get_active_provider_info
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(SAMPLE_YAML)
        tmp = f.name
    try:
        info = get_active_provider_info(tmp)
        assert info["active"] == "ds-flash"
        assert info["type"] == "deepseek"
        assert info["model"] == "deepseek-v4-flash"
    finally:
        os.unlink(tmp)

@case("build_llm_client：active provider 不存在时抛 RuntimeError")
def test_build_client_bad_active():
    from src.llm.loader import build_llm_client
    bad_yaml = "active: nonexistent\nproviders:\n  foo:\n    type: openai\n    model: x\n    api_key: k\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(bad_yaml)
        tmp = f.name
    try:
        try:
            build_llm_client(tmp)
            raise AssertionError("应当抛出 RuntimeError")
        except RuntimeError as e:
            assert "nonexistent" in str(e)
    finally:
        os.unlink(tmp)

@case("build_llm_client：未知 type 时抛 RuntimeError")
def test_build_client_unknown_type():
    from src.llm.loader import build_llm_client
    bad_yaml = "active: foo\nproviders:\n  foo:\n    type: unknown_provider\n    model: x\n    api_key: k\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(bad_yaml)
        tmp = f.name
    try:
        try:
            build_llm_client(tmp)
            raise AssertionError("应当抛出 RuntimeError")
        except RuntimeError as e:
            assert "unknown_provider" in str(e)
    finally:
        os.unlink(tmp)


# ─────────────────────────────────────────────────────────────
# 4. OpenAICompatAdapter 初始化与 reasoning 逻辑
# ─────────────────────────────────────────────────────────────

@case("OpenAICompatAdapter：缺 api_key 时抛 RuntimeError")
def test_openai_no_key():
    from src.llm.adapters.openai_compat import OpenAICompatAdapter
    from src.llm.base import LLMUsage
    try:
        OpenAICompatAdapter({"type": "openai", "model": "gpt-4o", "api_key": ""}, usage=LLMUsage())
        raise AssertionError("应当抛出 RuntimeError")
    except RuntimeError as e:
        assert "api_key" in str(e)

@case("OpenAICompatAdapter：DeepSeek flash 默认不开 reasoning")
def test_deepseek_flash_no_reasoning():
    from src.llm.adapters.openai_compat import OpenAICompatAdapter
    from src.llm.base import LLMUsage
    adapter = OpenAICompatAdapter(
        {"type": "deepseek", "model": "deepseek-v4-flash", "api_key": "sk-x", "base_url": "https://api.deepseek.com"},
        usage=LLMUsage(),
    )
    assert adapter._reasoning_effort == ""
    assert adapter._reasoning_kwargs() == {}

@case("OpenAICompatAdapter：DeepSeek pro 默认开启 reasoning=high")
def test_deepseek_pro_reasoning():
    from src.llm.adapters.openai_compat import OpenAICompatAdapter
    from src.llm.base import LLMUsage
    adapter = OpenAICompatAdapter(
        {"type": "deepseek", "model": "deepseek-v4-pro", "api_key": "sk-x"},
        usage=LLMUsage(),
    )
    assert adapter._reasoning_effort == "high"
    kw = adapter._reasoning_kwargs()
    assert kw["reasoning_effort"] == "high"
    assert kw["extra_body"]["thinking"]["type"] == "enabled"

@case("OpenAICompatAdapter：yaml 显式关闭 reasoning")
def test_deepseek_pro_reasoning_disabled():
    from src.llm.adapters.openai_compat import OpenAICompatAdapter
    from src.llm.base import LLMUsage
    adapter = OpenAICompatAdapter(
        {"type": "deepseek", "model": "deepseek-v4-pro", "api_key": "sk-x", "reasoning_effort": ""},
        usage=LLMUsage(),
    )
    assert adapter._reasoning_kwargs() == {}

@case("OpenAICompatAdapter：OpenAI 类型不传 reasoning 参数")
def test_openai_no_reasoning():
    from src.llm.adapters.openai_compat import OpenAICompatAdapter
    from src.llm.base import LLMUsage
    adapter = OpenAICompatAdapter(
        {"type": "openai", "model": "gpt-4o", "api_key": "sk-x"},
        usage=LLMUsage(),
    )
    assert adapter._reasoning_effort == ""
    assert adapter._reasoning_kwargs() == {}

@case("OpenAICompatAdapter：model property 正确")
def test_openai_model_prop():
    from src.llm.adapters.openai_compat import OpenAICompatAdapter
    from src.llm.base import LLMUsage
    adapter = OpenAICompatAdapter(
        {"type": "openai", "model": "gpt-4o-mini", "api_key": "sk-x"},
        usage=LLMUsage(),
    )
    assert adapter.model == "gpt-4o-mini"

@case("OpenAICompatAdapter：_temp() 优先使用 override")
def test_openai_temp_override():
    from src.llm.adapters.openai_compat import OpenAICompatAdapter
    from src.llm.base import LLMUsage
    adapter = OpenAICompatAdapter(
        {"type": "openai", "model": "gpt-4o", "api_key": "sk-x", "temperature": 0.5},
        usage=LLMUsage(),
    )
    assert adapter._temp(None) == 0.5
    assert adapter._temp(0.1) == 0.1


# ─────────────────────────────────────────────────────────────
# 5. OpenAICompatAdapter chat/chat_json/stream_chat（mock API）
# ─────────────────────────────────────────────────────────────

def _make_openai_adapter():
    from src.llm.adapters.openai_compat import OpenAICompatAdapter
    from src.llm.base import LLMUsage
    return OpenAICompatAdapter(
        {"type": "openai", "model": "gpt-4o", "api_key": "sk-fake"},
        usage=LLMUsage(),
    )

@case("OpenAICompatAdapter.chat：返回 content 字符串")
async def test_openai_chat():
    adapter = _make_openai_adapter()
    mock_usage = MagicMock(prompt_tokens=10, completion_tokens=5)
    mock_resp = MagicMock(
        choices=[MagicMock(message=MagicMock(content="Hello"))],
        usage=mock_usage,
    )
    adapter._client.chat.completions.create = AsyncMock(return_value=mock_resp)
    result = await adapter.chat([{"role": "user", "content": "hi"}])
    assert result == "Hello"
    assert adapter.usage.prompt_tokens == 10

@case("OpenAICompatAdapter.chat_json：解析 JSON 响应")
async def test_openai_chat_json():
    adapter = _make_openai_adapter()
    mock_usage = MagicMock(prompt_tokens=8, completion_tokens=12)
    mock_resp = MagicMock(
        choices=[MagicMock(message=MagicMock(content='{"tasks": [1, 2]}'))],
        usage=mock_usage,
    )
    adapter._client.chat.completions.create = AsyncMock(return_value=mock_resp)
    result = await adapter.chat_json([{"role": "user", "content": "plan"}])
    assert result == {"tasks": [1, 2]}

@case("OpenAICompatAdapter.stream_chat：逐 chunk 产出并统计 token")
async def test_openai_stream():
    adapter = _make_openai_adapter()

    chunk1 = MagicMock(choices=[MagicMock(delta=MagicMock(content="Hello "))], usage=None)
    chunk2 = MagicMock(choices=[MagicMock(delta=MagicMock(content="World"))], usage=None)
    chunk_final = MagicMock(
        choices=[],
        usage=MagicMock(prompt_tokens=5, completion_tokens=3),
    )

    async def fake_stream(*args, **kwargs):
        for c in [chunk1, chunk2, chunk_final]:
            yield c

    adapter._client.chat.completions.create = AsyncMock(return_value=fake_stream())
    chunks = []
    async for text in adapter.stream_chat([{"role": "user", "content": "hi"}]):
        chunks.append(text)
    assert chunks == ["Hello ", "World"]
    assert adapter.usage.prompt_tokens == 5
    assert adapter.usage.completion_tokens == 3


# ─────────────────────────────────────────────────────────────
# 6. AnthropicAdapter 初始化
# ─────────────────────────────────────────────────────────────

@case("AnthropicAdapter：未安装 anthropic 包时抛 ImportError")
def test_anthropic_not_installed():
    import builtins
    real_import = builtins.__import__
    def mock_import(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError("mocked missing")
        return real_import(name, *args, **kwargs)
    builtins.__import__ = mock_import
    try:
        # 需要重新导入以触发 ImportError 路径
        import importlib
        import src.llm.adapters.anthropic_adapter as mod
        importlib.reload(mod)
        from src.llm.base import LLMUsage
        try:
            mod.AnthropicAdapter({"model": "x", "api_key": "k"}, usage=LLMUsage())
            raise AssertionError("应当抛出 ImportError")
        except ImportError as e:
            assert "anthropic" in str(e).lower()
    finally:
        builtins.__import__ = real_import

@case("AnthropicAdapter._split_messages：正确分离 system 消息")
def test_split_messages():
    from src.llm.adapters.anthropic_adapter import _split_messages
    msgs = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好！"},
    ]
    system, rest = _split_messages(msgs)
    assert system == "你是助手"
    assert len(rest) == 2
    assert all(m["role"] != "system" for m in rest)

@case("AnthropicAdapter._split_messages：多个 system 消息拼接")
def test_split_messages_multi_system():
    from src.llm.adapters.anthropic_adapter import _split_messages
    msgs = [
        {"role": "system", "content": "Part A"},
        {"role": "system", "content": "Part B"},
        {"role": "user", "content": "hi"},
    ]
    system, rest = _split_messages(msgs)
    assert system == "Part A\n\nPart B"
    assert len(rest) == 1


# ─────────────────────────────────────────────────────────────
# 7. LLMUsage 并발 安全
# ─────────────────────────────────────────────────────────────

@case("LLMUsage：并发 record 后计数正确")
async def test_usage_concurrent():
    from src.llm.base import LLMUsage
    usage = LLMUsage()
    await asyncio.gather(*[usage.record(1, 2) for _ in range(100)])
    assert usage.prompt_tokens == 100
    assert usage.completion_tokens == 200
    assert usage.request_count == 100

@case("LLMUsage.snapshot：返回正确的 key 名")
def test_usage_snapshot():
    from src.llm.base import LLMUsage
    usage = LLMUsage()
    snap = usage.snapshot()
    assert set(snap.keys()) == {"llm_prompt_tokens", "llm_completion_tokens", "llm_request_count"}


# ─────────────────────────────────────────────────────────────
# 8. Configuration
# ─────────────────────────────────────────────────────────────

@case("Configuration：默认 llm_config_path = LLMConfig.yaml")
def test_config_default_path():
    # 绕开 lru_cache，直接实例化
    from src.config import Configuration
    cfg = Configuration()
    assert cfg.llm_config_path == "LLMConfig.yaml"

@case("Configuration：不含任何 deepseek_* 字段")
def test_config_no_deepseek_fields():
    from src.config import Configuration
    cfg = Configuration()
    for attr in vars(cfg):
        assert not attr.startswith("deepseek_"), f"意外字段：{attr}"

@case("Configuration.assert_ready：LLMConfig.yaml 不存在时抛 RuntimeError")
def test_assert_ready_no_yaml():
    from src.config import Configuration
    cfg = Configuration(
        llm_config_path="/nonexistent/path/LLMConfig.yaml",
        google_maps_api_key="fake-key",
    )
    try:
        cfg.assert_ready()
        raise AssertionError("应当抛出 RuntimeError")
    except RuntimeError as e:
        assert "LLM" in str(e) or "配置文件" in str(e)

@case("Configuration.assert_ready：缺 GOOGLE_MAPS_API_KEY 时抛 RuntimeError")
def test_assert_ready_no_maps_key():
    from src.config import Configuration
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write("active: x\nproviders:\n  x:\n    type: openai\n    model: y\n    api_key: z\n")
        tmp = f.name
    try:
        cfg = Configuration(llm_config_path=tmp, google_maps_api_key="")
        try:
            cfg.assert_ready()
            raise AssertionError("应当抛出 RuntimeError")
        except RuntimeError as e:
            assert "GOOGLE_MAPS_API_KEY" in str(e)
    finally:
        os.unlink(tmp)

@case("Configuration.assert_ready：两者齐全时不抛异常")
def test_assert_ready_ok():
    from src.config import Configuration
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write("active: x\nproviders:\n  x:\n    type: openai\n    model: y\n    api_key: z\n")
        tmp = f.name
    try:
        cfg = Configuration(llm_config_path=tmp, google_maps_api_key="AIzaSy-test")
        cfg.assert_ready()  # 不应抛出
    finally:
        os.unlink(tmp)


# ─────────────────────────────────────────────────────────────
# 9. FastAPI app 可正常创建
# ─────────────────────────────────────────────────────────────

@case("create_app()：FastAPI 实例创建成功")
def test_create_app():
    from src.main import create_app
    app = create_app()
    assert app is not None
    routes = [r.path for r in app.routes]
    assert "/healthz" in routes
    assert "/research" in routes
    assert "/research/stream" in routes
    assert "/usage" in routes


# ─────────────────────────────────────────────────────────────
# 报告
# ─────────────────────────────────────────────────────────────

print()
pass_count = sum(1 for _, ok, _ in results if ok)
fail_count = len(results) - pass_count

for name, ok, tb in results:
    icon = PASS if ok else FAIL
    print(f"{icon} {name}")
    if not ok:
        for line in tb.strip().splitlines()[-6:]:
            print(f"       {line}")

print()
print(f"  {'='*50}")
print(f"  共 {len(results)} 项，{PASS} {pass_count}，{FAIL if fail_count else chr(32)} {fail_count}")
print()

if fail_count:
    sys.exit(1)
