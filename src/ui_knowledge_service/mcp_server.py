"""MCP tool surface for the knowledge service."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ui_knowledge_service.models import (
    ComponentBundleResponse,
    ComponentDocResponse,
    ComponentStatus,
    ResolvedComponentAnswer,
    SearchResponse,
)
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
    async def get_component_bundle(
        library: str,
        component: str,
        freshness: str = "prefer_cache",
    ) -> ComponentBundleResponse:
        """Retrieve all available document types for a component from local cache and official sources."""

        return await service.get_component_bundle(
            library=library,
            component=component,
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
    async def resolve_component_query(
        query: str,
        library: str | None = None,
        component_hint: str | None = None,
        freshness: str = "prefer_cache",
    ) -> ResolvedComponentAnswer:
        """Resolve a user query into the best component guidance, examples, and provenance."""

        return await service.resolve_component_query(
            query=query,
            library=library,
            component_hint=component_hint,
            freshness=freshness,
        )

    @mcp.tool()
    async def audit_catalog(
        library: str | None = None,
        component: str | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        """Fetch and validate catalog sources for maintenance and drift inspection."""

        report = await service.audit_sources(
            library=library,
            component=component,
            limit=limit,
        )
        return report.model_dump(mode="json")

    @mcp.tool()
    async def compare_catalog_to_baseline(
        library: str | None = None,
        component: str | None = None,
        limit: int | None = None,
        baseline_path: str | None = None,
    ) -> dict[str, object]:
        """Compare the current catalog audit against the stored audit baseline."""

        report, comparison, resolved_baseline_path = await service.compare_audit_to_baseline(
            library=library,
            component=component,
            limit=limit,
            baseline_path=baseline_path,
        )
        return {
            "report": report.model_dump(mode="json"),
            "comparison": comparison.model_dump(mode="json") if comparison else None,
            "baseline_path": resolved_baseline_path,
        }

    @mcp.tool()
    async def get_catalog_maintenance_report(
        library: str | None = None,
        component: str | None = None,
        limit: int | None = None,
        baseline_path: str | None = None,
    ) -> dict[str, object]:
        """Return severity-ranked audit maintenance recommendations and a readable markdown report."""

        report, comparison, maintenance_report, resolved_baseline_path = await service.build_audit_maintenance_report(
            library=library,
            component=component,
            limit=limit,
            baseline_path=baseline_path,
        )
        return {
            "report": report.model_dump(mode="json"),
            "comparison": comparison.model_dump(mode="json") if comparison else None,
            "maintenance_report": maintenance_report.model_dump(mode="json"),
            "markdown": service.render_audit_maintenance_report_markdown(maintenance_report),
            "baseline_path": resolved_baseline_path,
        }

    @mcp.tool()
    async def promote_catalog_baseline(
        library: str | None = None,
        component: str | None = None,
        limit: int | None = None,
        baseline_path: str | None = None,
        snapshot_dir: str | None = None,
        report_dir: str | None = None,
        max_allowed_severity: str = "warn",
        force: bool = False,
    ) -> dict[str, object]:
        """Safely promote the current audit to the baseline after writing report artifacts."""

        from ui_knowledge_service.models import AuditSeverity

        report, comparison, promotion = await service.promote_audit_baseline(
            library=library,
            component=component,
            limit=limit,
            baseline_path=baseline_path,
            snapshot_dir=snapshot_dir,
            report_dir=report_dir,
            max_allowed_severity=AuditSeverity(max_allowed_severity),
            force=force,
        )
        return {
            "report": report.model_dump(mode="json"),
            "comparison": comparison.model_dump(mode="json") if comparison else None,
            "promotion": promotion.model_dump(mode="json"),
        }

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
