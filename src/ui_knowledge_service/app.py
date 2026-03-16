"""FastAPI app factory for the UI knowledge service."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from ui_knowledge_service.config import Settings
from ui_knowledge_service.mcp_server import build_mcp_server
from ui_knowledge_service.models import RefreshRequest
from ui_knowledge_service.service import KnowledgeService


def create_app(
    settings: Settings | None = None,
    *,
    service: KnowledgeService | None = None,
) -> FastAPI:
    active_service = service or KnowledgeService(settings or Settings.from_env())
    mcp = build_mcp_server(active_service)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await active_service.startup()
        async with mcp.session_manager.run():
            yield
        await active_service.shutdown()

    app = FastAPI(title="UI Knowledge Service", version="0.1.0", lifespan=lifespan)
    app.state.service = active_service
    app.state.mcp = mcp

    @app.get("/health")
    async def health() -> dict[str, object]:
        return active_service.health()

    @app.get("/sources")
    async def sources() -> list[dict[str, object]]:
        return [summary.model_dump(mode="json") for summary in active_service.source_summaries()]

    @app.get("/documents/{library}/{component}")
    async def get_document(
        library: str,
        component: str,
        doc_type: str | None = None,
        freshness: str = "prefer_cache",
    ) -> dict[str, object]:
        response = await active_service.get_component_doc(
            library=library,
            component=component,
            doc_type=doc_type,
            freshness=freshness,
        )
        if response.document is None:
            raise HTTPException(status_code=404, detail=response.model_dump(mode="json"))
        return response.model_dump(mode="json")

    @app.post("/refresh")
    async def refresh(request: RefreshRequest) -> dict[str, object]:
        result = await active_service.refresh(request)
        return result.model_dump(mode="json")

    app.mount("/mcp", mcp.streamable_http_app())
    return app
