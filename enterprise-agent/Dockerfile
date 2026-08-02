# ==========================================================================
# 企业级 AI Agent 骨架 · 生产镜像
#
# 两阶段构建：builder 装依赖，runtime 只带运行所需的东西。
# 这样最终镜像里没有编译工具链，体积更小、攻击面更小。
# ==========================================================================
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app

# 装到独立前缀，方便整体拷进 runtime 阶段。
RUN pip install --prefix=/install ".[postgres]"


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # 生产默认：关闭 debug、关闭演示数据、日志走 JSON 便于采集。
    ENVIRONMENT=prod \
    DEBUG=false \
    SEED_DEMO_DATA=false \
    LOG_JSON=true

WORKDIR /app

COPY --from=builder /install /usr/local
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts ./scripts

# **不以 root 运行。** 容器逃逸的第一步通常就是利用 root 权限。
RUN useradd --create-home --uid 10001 agent \
    && chown -R agent:agent /app
USER agent

EXPOSE 8000

# 健康检查用 /health：它不只回答「进程活着」，
# 还会返回有多少任务卡在非终态——这才是 Agent 系统真正的健康度指标。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
