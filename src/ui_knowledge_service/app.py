"""FastAPI app factory for the UI knowledge service."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from ui_knowledge_service.config import Settings
from ui_knowledge_service.mcp_server import build_mcp_server
from ui_knowledge_service.models import AuditSeverity, FreshnessState, RefreshRequest
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

    @app.get("/catalog/audit")
    async def catalog_audit(
        library: str | None = None,
        component: str | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        report = await active_service.audit_sources(
            library=library,
            component=component,
            limit=limit,
        )
        return report.model_dump(mode="json")

    @app.get("/catalog/audit/diff")
    async def catalog_audit_diff(
        library: str | None = None,
        component: str | None = None,
        limit: int | None = None,
        baseline_path: str | None = None,
    ) -> dict[str, object]:
        report, comparison, resolved_baseline_path = await active_service.compare_audit_to_baseline(
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

    @app.get("/catalog/audit/report")
    async def catalog_audit_report(
        library: str | None = None,
        component: str | None = None,
        limit: int | None = None,
        baseline_path: str | None = None,
    ) -> dict[str, object]:
        report, comparison, maintenance_report, resolved_baseline_path = await active_service.build_audit_maintenance_report(
            library=library,
            component=component,
            limit=limit,
            baseline_path=baseline_path,
        )
        return {
            "report": report.model_dump(mode="json"),
            "comparison": comparison.model_dump(mode="json") if comparison else None,
            "maintenance_report": maintenance_report.model_dump(mode="json"),
            "markdown": active_service.render_audit_maintenance_report_markdown(maintenance_report),
            "baseline_path": resolved_baseline_path,
        }

    @app.post("/catalog/audit/baseline")
    async def catalog_audit_write_baseline(
        library: str | None = None,
        component: str | None = None,
        limit: int | None = None,
        baseline_path: str | None = None,
    ) -> dict[str, object]:
        report = await active_service.audit_sources(
            library=library,
            component=component,
            limit=limit,
        )
        saved_path = active_service.save_audit_baseline(report, baseline_path=baseline_path)
        return {
            "baseline_path": saved_path,
            "report": report.model_dump(mode="json"),
        }

    @app.post("/catalog/audit/promote")
    async def catalog_audit_promote(
        library: str | None = None,
        component: str | None = None,
        limit: int | None = None,
        baseline_path: str | None = None,
        snapshot_dir: str | None = None,
        report_dir: str | None = None,
        max_allowed_severity: AuditSeverity = AuditSeverity.warn,
        force: bool = False,
    ) -> dict[str, object]:
        report, comparison, promotion = await active_service.promote_audit_baseline(
            library=library,
            component=component,
            limit=limit,
            baseline_path=baseline_path,
            snapshot_dir=snapshot_dir,
            report_dir=report_dir,
            max_allowed_severity=max_allowed_severity,
            force=force,
        )
        return {
            "report": report.model_dump(mode="json"),
            "comparison": comparison.model_dump(mode="json") if comparison else None,
            "promotion": promotion.model_dump(mode="json"),
        }

    @app.get("/search")
    async def search(
        query: str,
        library: str | None = None,
        component_hint: str | None = None,
        k: int = 6,
    ) -> dict[str, object]:
        response = await active_service.search_component_docs(
            query=query,
            library=library,
            component_hint=component_hint,
            k=k,
        )
        return response.model_dump(mode="json")

    @app.get("/resolve")
    async def resolve(
        query: str,
        library: str | None = None,
        component_hint: str | None = None,
        freshness: str = "prefer_cache",
    ) -> dict[str, object]:
        response = await active_service.resolve_component_query(
            query=query,
            library=library,
            component_hint=component_hint,
            freshness=freshness,
        )
        if response.freshness_state == FreshnessState.missing and not response.supporting_documents:
            raise HTTPException(status_code=404, detail=response.model_dump(mode="json"))
        return response.model_dump(mode="json")

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

    @app.get("/bundles/{library}/{component}")
    async def get_bundle(
        library: str,
        component: str,
        freshness: str = "prefer_cache",
    ) -> dict[str, object]:
        response = await active_service.get_component_bundle(
            library=library,
            component=component,
            freshness=freshness,
        )
        if not response.documents:
            raise HTTPException(status_code=404, detail=response.model_dump(mode="json"))
        return response.model_dump(mode="json")

    @app.post("/refresh")
    async def refresh(request: RefreshRequest) -> dict[str, object]:
        result = await active_service.refresh(request)
        return result.model_dump(mode="json")

    @app.get("/refresh/status")
    async def refresh_status(limit: int = 20) -> dict[str, object]:
        status = active_service.refresh_status(limit=limit)
        return status.model_dump(mode="json")

    @app.get("/status/{library}/{component}")
    async def component_status(
        library: str,
        component: str,
        doc_type: str | None = None,
    ) -> dict[str, object]:
        status = await active_service.get_component_status(library, component, doc_type=doc_type)
        if status is None:
            raise HTTPException(status_code=404, detail={"message": "Component status not found"})
        return status.model_dump(mode="json")

    app.mount("/mcp", mcp.streamable_http_app())
    return app
