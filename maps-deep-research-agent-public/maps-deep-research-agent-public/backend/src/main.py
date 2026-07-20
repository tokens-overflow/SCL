"""FastAPI 入口 —— 把 MapsDeepResearchAgent 暴露为 HTTP / SSE 接口。"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .agent import MapsDeepResearchAgent
from .config import Configuration, get_configuration
from .llm import get_active_provider_info
from .models import (
    ResearchRequest,
    ResearchResponse,
    UsageSnapshot,
)

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(name)s:%(funcName)s:%(lineno)d | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("maps_deep_research")


# ---------------------------------------------------------------------------
# FastAPI 应用工厂
# ---------------------------------------------------------------------------
_agent_singleton: MapsDeepResearchAgent | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = get_configuration()
    try:
        provider = get_active_provider_info(config.llm)
        logger.info(
            "Maps Deep Research Agent 启动：config=%s provider=%s model=%s max_tasks=%d concurrency=%d",
            config.config_path,
            provider["active"],
            provider["model"],
            config.app.agent.max_tasks,
            config.app.agent.task_concurrency,
        )
    except Exception as exc:
        logger.warning("启动时读取配置失败：%s", exc)
    yield
    global _agent_singleton
    if _agent_singleton is not None:
        await _agent_singleton.aclose()
        _agent_singleton = None


def _get_agent() -> MapsDeepResearchAgent:
    global _agent_singleton
    if _agent_singleton is None:
        _agent_singleton = MapsDeepResearchAgent(config=get_configuration())
    return _agent_singleton


def create_app() -> FastAPI:
    config = get_configuration()
    app = FastAPI(title="Maps Deep Research Agent", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.app.server.cors_origins or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        try:
            provider = get_active_provider_info(config.llm)
            return {
                "status": "ok",
                "provider": provider["active"],
                "model": provider["model"],
                "config_path": str(config.config_path),
            }
        except Exception:
            return {
                "status": "ok",
                "provider": "unknown",
                "model": "unknown",
                "config_path": str(config.config_path),
            }

    @app.get("/usage", response_model=UsageSnapshot)
    async def usage() -> UsageSnapshot:
        return _get_agent().usage_snapshot

    @app.post("/research", response_model=ResearchResponse)
    async def run_research(payload: ResearchRequest) -> ResearchResponse:
        try:
            agent = _get_agent()
            state = await agent.run(
                topic=payload.topic,
                language=payload.language,
                max_tasks=payload.max_tasks,
                location_hint=payload.location_hint,
                budget=payload.budget,
                travel_date=payload.travel_date,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # pragma: no cover - 防御性兜底
            logger.exception("research 调用失败")
            raise HTTPException(status_code=500, detail="research failed") from exc

        return ResearchResponse(
            run_id=state.run_id,
            report_markdown=state.report_markdown,
            itinerary=state.itinerary,
            map_overview=state.map_overview,
            tasks=state.tasks,
        )

    @app.post("/research/stream")
    async def stream_research(payload: ResearchRequest) -> StreamingResponse:
        try:
            agent = _get_agent()
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        async def event_iter() -> AsyncIterator[str]:
            try:
                async for event in agent.run_stream(
                    topic=payload.topic,
                    language=payload.language,
                    max_tasks=payload.max_tasks,
                    location_hint=payload.location_hint,
                    budget=payload.budget,
                    travel_date=payload.travel_date,
                ):
                    body = event.model_dump_json()
                    yield f"event: {event.type}\ndata: {body}\n\n"
            except asyncio.CancelledError:  # pragma: no cover - 客户端断开
                logger.info("客户端在流式过程中断开")
                raise
            except Exception as exc:  # pragma: no cover - 防御性兜底
                logger.exception("流式 research 失败")
                error_payload = json.dumps({"type": "error", "detail": str(exc)})
                yield f"event: error\ndata: {error_payload}\n\n"

        return StreamingResponse(
            event_iter(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    cfg: Configuration = get_configuration()
    uvicorn.run(
        "src.main:app",
        host=cfg.app.server.host,
        port=cfg.app.server.port,
        reload=False,
        log_level=cfg.app.server.log_level.lower(),
    )
