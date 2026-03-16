"""MCP tool surface for the knowledge service."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ui_knowledge_service.models import ComponentDocResponse, ComponentStatus, SearchResponse
from ui_knowledge_service.service import KnowledgeService


def build_mcp_server(service: KnowledgeService) -> FastMCP:
    mcp = FastMCP("UI Knowledge Service", json_response=True)

    @mcp.tool()
    async def get_component_doc(
        library: str,
        component: str,
        doc_type: str | None = None,
        freshness: str = "prefer_cache",
    ) -> ComponentDocResponse:
        """Retrieve structured component documentation with provenance."""

        return await service.get_component_doc(
            library=library,
            component=component,
            doc_type=doc_type,
            freshness=freshness,
        )

    @mcp.tool()
    async def search_component_docs(
        query: str,
        library: str | None = None,
        component_hint: str | None = None,
        k: int = 6,
    ) -> SearchResponse:
        """Search cached component docs using exact, FTS, and vector fallback paths."""

        return await service.search_component_docs(
            query=query,
            library=library,
            component_hint=component_hint,
            k=k,
        )

    @mcp.tool()
    async def get_component_examples(library: str, component: str) -> dict[str, object]:
        """Return code examples extracted from the cached component document."""

        return await service.get_component_examples(library, component)

    @mcp.tool()
    async def get_component_status(
        library: str,
        component: str,
        doc_type: str | None = None,
    ) -> ComponentStatus | None:
        """Return freshness and provenance metadata for a component document."""

        return await service.get_component_status(library, component, doc_type=doc_type)

    return mcp

