"""Retrieval and refresh orchestration."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from importlib import resources
import json
import logging
from typing import Iterable

from ui_knowledge_service.config import Settings
from ui_knowledge_service.models import (
    ComponentBundleResponse,
    ComponentDocResponse,
    ComponentStatus,
    FreshnessState,
    RefreshRecord,
    RefreshRequest,
    RefreshResult,
    RefreshStatus,
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
        preferred_doc_types = self._preferred_doc_types_for_query(query)
        if component_hint and library:
            for doc_type in preferred_doc_types:
                exact = self.store.get_document(library, component_hint, doc_type)
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
                    break
            else:
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
        reranked = self._rerank_hits(deduped, query=query, preferred_doc_types=preferred_doc_types, component_hint=component_hint)
        reranked = reranked[:k]

        return SearchResponse(
            query=query,
            library=library,
            component_hint=component_hint,
            results=reranked,
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
        refresh_record = self.store.last_refresh_record(library, component, doc_type)
        return ComponentStatus(
            document_id=document.document_id,
            freshness_state=document.freshness_state(),
            source_url=document.source_url,
            source_kind=document.source_kind,
            fetched_at=document.fetched_at,
            stale_after=document.stale_after,
            version=document.version,
            citations=document.citations,
            last_refresh_status=refresh_record.status if refresh_record else None,
            last_refresh_error=refresh_record.error if refresh_record else None,
            last_refresh_attempted_at=refresh_record.attempted_at if refresh_record else None,
        )

    def refresh_status(self, *, limit: int = 20) -> RefreshStatus:
        counts = self.store.refresh_counts()
        records = self.store.recent_refresh_records(limit=limit)
        return RefreshStatus(
            total_attempts=sum(counts.values()),
            success_count=counts["success"],
            not_modified_count=counts["not_modified"],
            failure_count=counts["failure"],
            records=records,
        )

    def source_summaries(self) -> list[SourceSummary]:
        summaries: list[SourceSummary] = []
        for library, adapter in self.adapters.items():
            descriptors = adapter.discover()
            components = sorted({descriptor.component_slug for descriptor in descriptors})
            summaries.append(
                SourceSummary(
                    library=library,
                    component_count=len(components),
                    components=components,
                    doc_type_count=len(descriptors),
                )
            )
        return summaries

    async def get_component_bundle(
        self,
        library: str,
        component: str,
        *,
        freshness: str = "prefer_cache",
    ) -> ComponentBundleResponse:
        adapter = self.adapters.get(library)
        if not adapter:
            return ComponentBundleResponse(
                library=library,
                component=slugify(component),
                retrieval_path="miss",
                message="Unknown library",
            )

        descriptors = adapter.list_for_component(component)
        if not descriptors:
            return ComponentBundleResponse(
                library=library,
                component=slugify(component),
                retrieval_path="miss",
                suggestions=self.store.suggest_components(library, component),
                message="Component is not available in the source catalog.",
            )

        documents = []
        refreshed_documents: list[str] = []
        retrieval_segments: list[str] = []
        for descriptor in descriptors:
            response = await self.get_component_doc(
                library=library,
                component=component,
                doc_type=descriptor.doc_type,
                freshness=freshness,
            )
            retrieval_segments.append(response.retrieval_path)
            if response.refreshed and response.document:
                refreshed_documents.append(response.document.document_id)
            if response.document:
                documents.append(response.document)

        if not documents:
            return ComponentBundleResponse(
                library=library,
                component=slugify(component),
                retrieval_path="+".join(sorted(set(retrieval_segments))) or "miss",
                available_doc_types=[descriptor.doc_type for descriptor in descriptors],
                suggestions=self.store.suggest_components(library, component),
                message="No documents were available locally and refresh did not succeed.",
            )

        freshness_state = FreshnessState.fresh
        if any(document.freshness_state() == FreshnessState.stale for document in documents):
            freshness_state = FreshnessState.stale

        return ComponentBundleResponse(
            library=library,
            component=slugify(component),
            documents=documents,
            available_doc_types=[descriptor.doc_type for descriptor in descriptors],
            freshness_state=freshness_state,
            retrieval_path="+".join(sorted(set(retrieval_segments))) or "cache_exact",
            refreshed_documents=refreshed_documents,
        )

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

        adapter = self.adapters.get(request.library)
        if not adapter:
            result.errors.append(f"Unknown library: {request.library}")
            return result

        descriptors = (
            [adapter.resolve(request.component, request.doc_type)]
            if request.doc_type
            else adapter.list_for_component(request.component)
        )
        descriptors = [descriptor for descriptor in descriptors if descriptor is not None]
        if not descriptors:
            result.errors.append(f"Unable to find catalog entries for {request.library}:{slugify(request.component)}")
            return result

        for descriptor in descriptors:
            refreshed = await self._refresh_document(
                request.library,
                request.component,
                descriptor.doc_type,
                force=request.force,
            )
            if refreshed:
                result.refreshed_documents.append(refreshed.document_id)
            else:
                result.errors.append(f"Unable to refresh {request.library}:{slugify(request.component)}:{descriptor.doc_type}")
        return result

    async def prewarm(self, *, force: bool = False) -> RefreshResult:
        manifest_resource = resources.files("ui_knowledge_service").joinpath("prewarm_manifest.json")
        manifest = json.loads(manifest_resource.read_text(encoding="utf-8"))
        result = RefreshResult()
        for library, components in manifest.items():
            for component in components:
                adapter = self.adapters.get(library)
                if not adapter:
                    continue
                for descriptor in adapter.list_for_component(component):
                    try:
                        refreshed = await self._refresh_document(
                            library,
                            component,
                            doc_type=descriptor.doc_type,
                            force=force,
                        )
                        if refreshed:
                            result.refreshed_documents.append(refreshed.document_id)
                    except Exception as exc:  # pragma: no cover - defensive logging path
                        LOGGER.exception("Prewarm failed for %s:%s:%s", library, component, descriptor.doc_type)
                        result.errors.append(f"{library}:{component}:{descriptor.doc_type}: {exc}")
        return result

    def health(self) -> dict[str, object]:
        refresh_status = self.refresh_status(limit=5)
        return {
            "status": "ok",
            "documents": self.store.count_documents(),
            "stale_documents": self.store.stale_document_count(),
            "sources": [summary.model_dump(mode="json") for summary in self.source_summaries()],
            "refresh": refresh_status.model_dump(mode="json"),
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
                self.store.record_refresh(
                    RefreshRecord(
                        document_id=descriptor.document_id,
                        library=library,
                        component=descriptor.component_slug,
                        doc_type=descriptor.doc_type,
                        status="failure",
                        error=str(exc),
                    )
                )
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
                self.store.record_refresh(
                    RefreshRecord(
                        document_id=saved.document_id,
                        library=saved.library,
                        component=saved.component,
                        doc_type=saved.doc_type,
                        status="not_modified",
                    )
                )
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
            self.store.record_refresh(
                RefreshRecord(
                    document_id=saved.document_id,
                    library=saved.library,
                    component=saved.component,
                    doc_type=saved.doc_type,
                    status="success",
                )
            )
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

    def _preferred_doc_types_for_query(self, query: str) -> list[str]:
        lowered = query.lower()
        preferred: list[str] = []
        if any(token in lowered for token in ("accessibility", "a11y", "aria", "screen reader", "keyboard")):
            preferred.append("accessibility")
        if any(token in lowered for token in ("api", "props", "prop", "slot", "slots", "argument", "attribute")):
            preferred.append("api")
        if any(token in lowered for token in ("example", "examples", "demo", "usage", "how do i", "how to")):
            preferred.append("overview")
            preferred.append("examples")
        preferred.extend(["overview", "api", "accessibility", "examples"])
        deduped: list[str] = []
        for item in preferred:
            if item not in deduped:
                deduped.append(item)
        return deduped

    def _rerank_hits(
        self,
        hits: Iterable[SearchHit],
        *,
        query: str,
        preferred_doc_types: list[str],
        component_hint: str | None,
    ) -> list[SearchHit]:
        component_slug = slugify(component_hint) if component_hint else None
        preference_scores = {doc_type: len(preferred_doc_types) - index for index, doc_type in enumerate(preferred_doc_types)}
        reranked: list[SearchHit] = []
        for hit in hits:
            score = hit.score
            if hit.matched_by == "exact":
                score += 5.0
            elif hit.matched_by == "fts":
                score += 2.0
            else:
                score += 1.0
            score += preference_scores.get(hit.doc_type, 0) * 0.35
            if component_slug and hit.component == component_slug:
                score += 1.5
            if hit.freshness_state == FreshnessState.stale:
                score -= 0.5
            reranked.append(hit.model_copy(update={"score": score}))
        reranked.sort(key=lambda item: item.score, reverse=True)
        return reranked
