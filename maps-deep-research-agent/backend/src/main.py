"""FastAPI entrypoint exposing the Maps Deep Research Agent."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from loguru import logger

from .agent import MapsDeepResearchAgent
from .config import Configuration, get_configuration
from .models import (
    ResearchRequest,
    ResearchResponse,
    UsageSnapshot,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger.remove()
logger.add(
    sys.stderr,
    level="INFO",
    format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <5}</level> | "
        "<cyan>{name}:{function}:{line}</cyan> | <level>{message}</level>"
    ),
    colorize=True,
)
logging.basicConfig(level=logging.INFO)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
_agent_singleton: MapsDeepResearchAgent | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = get_configuration()
    logger.info(
        "Starting Maps Deep Research Agent: model={} max_tasks={} concurrency={}",
        config.deepseek_model,
        config.max_tasks,
        config.task_concurrency,
    )
    yield
    # Lazy singleton may not exist if no request was served.
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
        allow_origins=config.cors_origin_list or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "model": config.deepseek_model}

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
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("research failed")
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
                ):
                    body = event.model_dump_json()
                    yield f"event: {event.type}\ndata: {body}\n\n"
            except asyncio.CancelledError:  # pragma: no cover
                logger.info("Client disconnected mid-stream")
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("Streaming research failed")
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
        host=cfg.host,
        port=cfg.port,
        reload=False,
        log_level=cfg.log_level.lower(),
    )
