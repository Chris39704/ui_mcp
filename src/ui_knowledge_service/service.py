"""Retrieval and refresh orchestration."""

from __future__ import annotations

import asyncio
from collections import defaultdict
import json
import logging
from pathlib import Path

from ui_knowledge_service.config import Settings
from ui_knowledge_service.models import (
    ComponentDocResponse,
    ComponentStatus,
    FreshnessState,
    RefreshRequest,
    RefreshResult,
    SearchHit,
    SearchResponse,
    SourceSummary,
)
from ui_knowledge_service.sources import AngularMaterialSourceAdapter, MuiSourceAdapter, UswdsSourceAdapter
from ui_knowledge_service.store import DocumentStore
from ui_knowledge_service.utils import slugify, utcnow
from ui_knowledge_service.vector_index import VectorIndex


LOGGER = logging.getLogger(__name__)


class KnowledgeService:
    """High-level API used by FastAPI routes and MCP tools."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()
        self.store = DocumentStore(self.settings)
        self.vector_index = VectorIndex(self.settings)
        self.adapters = {
            "mui": MuiSourceAdapter(self.settings),
            "angular-material": AngularMaterialSourceAdapter(self.settings),
            "uswds": UswdsSourceAdapter(self.settings),
        }
        self._refresh_tasks: dict[str, asyncio.Task[None]] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def startup(self) -> None:
        self.settings.ensure_dirs()
        if self.store.count_documents():
            self.vector_index.rebuild(self.store.list_documents())
        if self.settings.prewarm_on_start and not self.store.count_documents():
            await self.prewarm()

    async def shutdown(self) -> None:
        if self._refresh_tasks:
            await asyncio.gather(*self._refresh_tasks.values(), return_exceptions=True)
        self.store.close()

    async def get_component_doc(
        self,
        library: str,
        component: str,
        *,
        doc_type: str | None = None,
        freshness: str = "prefer_cache",
    ) -> ComponentDocResponse:
        document = self.store.get_document(library, component, doc_type)
        if document:
            freshness_state = document.freshness_state()
            if freshness == "prefer_fresh":
                refreshed = await self._refresh_document(library, component, doc_type, force=True)
                if refreshed:
                    return ComponentDocResponse(
                        document=refreshed,
                        freshness_state=refreshed.freshness_state(),
                        retrieval_path="refreshed",
                        refreshed=True,
                    )
            elif freshness_state == FreshnessState.stale:
                self._schedule_refresh(library, component, doc_type)
            return ComponentDocResponse(
                document=document,
                freshness_state=freshness_state,
                retrieval_path="cache_exact",
                refreshed=False,
            )

        fetched = await self._refresh_document(library, component, doc_type)
        if fetched:
            return ComponentDocResponse(
                document=fetched,
                freshness_state=fetched.freshness_state(),
                retrieval_path="source_fetch",
                refreshed=True,
            )

        suggestions = self.store.suggest_components(library, component)
        message = "Component was not available locally and the upstream refresh did not succeed."
        return ComponentDocResponse(
            document=None,
            freshness_state=FreshnessState.missing,
            retrieval_path="miss",
            suggestions=suggestions,
            message=message,
        )

    async def search_component_docs(
        self,
        query: str,
        *,
        library: str | None = None,
        component_hint: str | None = None,
        k: int = 6,
    ) -> SearchResponse:
        results: list[SearchHit] = []
        if component_hint and library:
            exact = self.store.get_document(library, component_hint)
            if exact:
                results.append(
                    SearchHit(
                        document_id=exact.document_id,
                        library=exact.library,
                        component=exact.component,
                        doc_type=exact.doc_type,
                        title=exact.title,
                        source_url=exact.source_url,
                        score=1.0,
                        snippet=exact.content_md[:240].strip(),
                        matched_by="exact",
                        freshness_state=exact.freshness_state(),
                    )
                )

        fts_hits = self.store.search_fts(query, library=library, limit=k)
        results.extend(fts_hits)
        retrieval_path = "fts"

        if len(results) < k:
            vector_hits = self.vector_index.search(query, library=library, limit=k)
            retrieval_path = "fts+vector"
            seen = {hit.document_id for hit in results}
            for hit in vector_hits:
                if hit.document_id not in seen:
                    results.append(hit)
                if len(results) >= k:
                    break

        deduped: list[SearchHit] = []
        seen = set()
        for hit in results:
            if hit.document_id in seen:
                continue
            seen.add(hit.document_id)
            deduped.append(hit)
            if len(deduped) >= k:
                break

        return SearchResponse(
            query=query,
            library=library,
            component_hint=component_hint,
            results=deduped,
            retrieval_path=retrieval_path,
        )

    async def get_component_examples(self, library: str, component: str) -> dict[str, object]:
        response = await self.get_component_doc(library, component)
        document = response.document
        return {
            "library": library,
            "component": slugify(component),
            "examples": document.code_examples if document else [],
            "freshness_state": response.freshness_state,
            "source_url": document.source_url if document else None,
        }

    async def get_component_status(self, library: str, component: str, *, doc_type: str | None = None) -> ComponentStatus | None:
        document = self.store.get_document(library, component, doc_type)
        if not document:
            return None
        return ComponentStatus(
            document_id=document.document_id,
            freshness_state=document.freshness_state(),
            source_url=document.source_url,
            source_kind=document.source_kind,
            fetched_at=document.fetched_at,
            stale_after=document.stale_after,
            version=document.version,
            citations=document.citations,
        )

    def source_summaries(self) -> list[SourceSummary]:
        return [
            SourceSummary(
                library=library,
                component_count=len(adapter.discover()),
                components=sorted(descriptor.component_slug for descriptor in adapter.discover()),
            )
            for library, adapter in self.adapters.items()
        ]

    async def refresh(self, request: RefreshRequest) -> RefreshResult:
        result = RefreshResult()
        if request.prewarm:
            prewarm_result = await self.prewarm(force=request.force)
            result.refreshed_documents.extend(prewarm_result.refreshed_documents)
            result.errors.extend(prewarm_result.errors)
            return result

        if not request.library or not request.component:
            result.errors.append("library and component are required unless prewarm=true")
            return result

        refreshed = await self._refresh_document(
            request.library,
            request.component,
            request.doc_type,
            force=request.force,
        )
        if refreshed:
            result.refreshed_documents.append(refreshed.document_id)
        else:
            result.errors.append(f"Unable to refresh {request.library}:{slugify(request.component)}")
        return result

    async def prewarm(self, *, force: bool = False) -> RefreshResult:
        manifest_path = Path(__file__).with_name("prewarm_manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        result = RefreshResult()
        for library, components in manifest.items():
            for component in components:
                try:
                    refreshed = await self._refresh_document(library, component, force=force)
                    if refreshed:
                        result.refreshed_documents.append(refreshed.document_id)
                except Exception as exc:  # pragma: no cover - defensive logging path
                    LOGGER.exception("Prewarm failed for %s:%s", library, component)
                    result.errors.append(f"{library}:{component}: {exc}")
        return result

    def health(self) -> dict[str, object]:
        return {
            "status": "ok",
            "documents": self.store.count_documents(),
            "sources": [summary.model_dump(mode="json") for summary in self.source_summaries()],
            "timestamp": utcnow().isoformat(),
        }

    async def _refresh_document(
        self,
        library: str,
        component: str,
        doc_type: str | None = None,
        *,
        force: bool = False,
    ):
        adapter = self.adapters.get(library)
        if not adapter:
            return None
        descriptor = adapter.resolve(component, doc_type)
        if not descriptor:
            return None

        lock_key = descriptor.document_id
        async with self._locks[lock_key]:
            existing = self.store.get_document(library, component, doc_type)
            try:
                fetched = await adapter.fetch(
                    descriptor,
                    etag=None if force or not existing else existing.etag,
                    last_modified=None if force or not existing else existing.last_modified,
                )
            except Exception as exc:
                LOGGER.warning("Refresh failed for %s: %s", descriptor.document_id, exc)
                return None

            if fetched.not_modified and existing:
                updated = existing.model_copy(
                    update={
                        "fetched_at": fetched.fetched_at,
                        "stale_after": fetched.fetched_at + adapter.freshness_hint(descriptor),
                        "etag": fetched.etag or existing.etag,
                        "last_modified": fetched.last_modified or existing.last_modified,
                    }
                )
                saved = self.store.save_document(updated)
                self.vector_index.upsert_document(saved)
                return saved

            raw_path = self.store.save_raw_snapshot(
                url=descriptor.url,
                content_type=fetched.content_type,
                content=fetched.content,
                document_id=descriptor.document_id,
            )
            normalized = adapter.normalize(fetched, raw_path=raw_path)
            saved = self.store.save_document(normalized)
            self.vector_index.upsert_document(saved)
            return saved

    def _schedule_refresh(self, library: str, component: str, doc_type: str | None = None) -> None:
        task_key = f"{library}:{slugify(component)}:{doc_type or 'overview'}"
        existing = self._refresh_tasks.get(task_key)
        if existing and not existing.done():
            return

        async def runner() -> None:
            try:
                await self._refresh_document(library, component, doc_type)
            finally:
                self._refresh_tasks.pop(task_key, None)

        self._refresh_tasks[task_key] = asyncio.create_task(runner())
