"""Retrieval and refresh orchestration."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from importlib import resources
import json
import logging
from pathlib import Path
from typing import Iterable

from ui_knowledge_service.config import Settings
from ui_knowledge_service.models import (
    AuditSeverity,
    BaselinePromotionResult,
    ComponentBundleResponse,
    ComponentDocResponse,
    ComponentStatus,
    FreshnessState,
    RefreshRecord,
    RefreshRequest,
    RefreshResult,
    RefreshStatus,
    ResolvedComponentAnswer,
    ResolvedSupportingDocument,
    SearchHit,
    SearchResponse,
    SourceAuditComparisonReport,
    SourceAuditDriftEntry,
    SourceAuditEntry,
    SourceAuditMaintenanceRecommendation,
    SourceAuditMaintenanceReport,
    SourceAuditReport,
    SourceSummary,
)
from ui_knowledge_service.sources import AngularMaterialSourceAdapter, MuiSourceAdapter, UswdsSourceAdapter
from ui_knowledge_service.store import DocumentStore
from ui_knowledge_service.utils import slugify, tokenize, unique_strings, utcnow
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

    async def resolve_component_query(
        self,
        query: str,
        *,
        library: str | None = None,
        component_hint: str | None = None,
        freshness: str = "prefer_cache",
    ) -> ResolvedComponentAnswer:
        retrieval_segments: list[str] = []
        search_response = None
        chosen_library = library
        chosen_component = component_hint

        if not (chosen_library and chosen_component):
            search_response = await self.search_component_docs(
                query=query,
                library=library,
                component_hint=component_hint,
                k=5,
            )
            retrieval_segments.append(search_response.retrieval_path)
            if search_response.results:
                top = search_response.results[0]
                chosen_library = top.library
                chosen_component = top.component
            else:
                return ResolvedComponentAnswer(
                    query=query,
                    library=library,
                    component=component_hint,
                    retrieval_path="+".join(retrieval_segments) or "miss",
                    suggestions=self.store.suggest_components(library, component_hint or query),
                    message="No matching component documents were found.",
                )

        bundle = await self.get_component_bundle(
            chosen_library,
            chosen_component,
            freshness=freshness,
        )
        retrieval_segments.append(bundle.retrieval_path)
        if not bundle.documents:
            suggestions = list(bundle.suggestions)
            if search_response:
                suggestions.extend(hit.component for hit in search_response.results)
            return ResolvedComponentAnswer(
                query=query,
                library=chosen_library,
                component=chosen_component,
                freshness_state=FreshnessState.missing,
                retrieval_path="+".join(segment for segment in retrieval_segments if segment) or "miss",
                suggestions=unique_strings(suggestions),
                message=bundle.message or "Component documents were not available.",
            )

        preferred_doc_types = self._preferred_doc_types_for_query(query)
        ordered_documents = sorted(
            bundle.documents,
            key=lambda item: (
                preferred_doc_types.index(item.doc_type) if item.doc_type in preferred_doc_types else 999,
                item.doc_type,
            ),
        )

        key_points = self._build_key_points(query, ordered_documents)
        api_highlights = self._select_relevant_strings(
            [api_item for document in ordered_documents for api_item in document.api_items],
            query=query,
            limit=4,
        )
        accessibility_highlights = self._select_relevant_strings(
            [note for document in ordered_documents for note in document.accessibility_notes],
            query=query,
            limit=4,
        )
        example_snippets = self._select_relevant_strings(
            [example for document in ordered_documents for example in document.code_examples],
            query=query,
            limit=3,
        )
        summary = self._build_summary(
            library=chosen_library,
            component=chosen_component,
            key_points=key_points,
            api_highlights=api_highlights,
            accessibility_highlights=accessibility_highlights,
        )
        freshness_state = FreshnessState.fresh
        if any(document.freshness_state() == FreshnessState.stale for document in ordered_documents):
            freshness_state = FreshnessState.stale

        supporting_documents = [
            ResolvedSupportingDocument(
                document_id=document.document_id,
                library=document.library,
                component=document.component,
                doc_type=document.doc_type,
                title=document.title,
                source_url=document.source_url,
                freshness_state=document.freshness_state(),
            )
            for document in ordered_documents
        ]

        return ResolvedComponentAnswer(
            query=query,
            library=chosen_library,
            component=chosen_component,
            summary=summary,
            key_points=key_points,
            api_highlights=api_highlights,
            accessibility_highlights=accessibility_highlights,
            example_snippets=example_snippets,
            supporting_documents=supporting_documents,
            freshness_state=freshness_state,
            retrieval_path="+".join(segment for segment in retrieval_segments if segment) or "cache_exact",
            suggestions=[],
        )

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

    async def audit_sources(
        self,
        *,
        library: str | None = None,
        component: str | None = None,
        limit: int | None = None,
        snapshot_dir: str | None = None,
    ) -> SourceAuditReport:
        entries: list[SourceAuditEntry] = []
        selected_adapters = (
            {library: self.adapters[library]} if library and library in self.adapters else self.adapters
        )
        snapshot_base = Path(snapshot_dir) if snapshot_dir else None
        if snapshot_base:
            snapshot_base.mkdir(parents=True, exist_ok=True)

        for adapter_library, adapter in selected_adapters.items():
            descriptors = adapter.list_for_component(component) if component else adapter.discover()
            if limit is not None:
                descriptors = descriptors[:limit]
            for descriptor in descriptors:
                try:
                    fetched = await adapter.fetch(descriptor)
                    document = adapter.normalize(fetched)
                    warnings = self._audit_warnings(document)
                    snapshot_path = None
                    if snapshot_base:
                        snapshot_path = self._write_audit_snapshot(snapshot_base, document, fetched.content)
                    entries.append(
                        SourceAuditEntry(
                            document_id=document.document_id,
                            library=document.library,
                            component=document.component,
                            doc_type=document.doc_type,
                            url=document.source_url,
                            fetch_status="success",
                            content_length=len(document.content_md),
                            content_checksum=document.checksum,
                            section_count=len(document.sections),
                            api_item_count=len(document.api_items),
                            accessibility_note_count=len(document.accessibility_notes),
                            example_count=len(document.code_examples),
                            warnings=warnings,
                            snapshot_path=snapshot_path,
                        )
                    )
                except Exception as exc:
                    entries.append(
                        SourceAuditEntry(
                            document_id=descriptor.document_id,
                            library=adapter_library,
                            component=descriptor.component_slug,
                            doc_type=descriptor.doc_type,
                            url=descriptor.url,
                            fetch_status="failure",
                            error=str(exc),
                            warnings=["fetch_failed"],
                        )
                    )
        return SourceAuditReport(entries=entries)

    def default_audit_baseline_path(self) -> Path:
        return self.settings.audit_dir / "catalog_baseline.json"

    def save_audit_baseline(
        self,
        report: SourceAuditReport,
        *,
        baseline_path: str | None = None,
    ) -> str:
        path = Path(baseline_path) if baseline_path else self.default_audit_baseline_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        return str(path)

    def load_audit_baseline(self, *, baseline_path: str | None = None) -> SourceAuditReport | None:
        path = Path(baseline_path) if baseline_path else self.default_audit_baseline_path()
        if not path.exists():
            return None
        return SourceAuditReport.model_validate_json(path.read_text(encoding="utf-8"))

    def compare_audit_reports(
        self,
        current: SourceAuditReport,
        baseline: SourceAuditReport,
    ) -> SourceAuditComparisonReport:
        current_by_id = {entry.document_id: entry for entry in current.entries}
        baseline_by_id = {entry.document_id: entry for entry in baseline.entries}
        all_ids = sorted(set(current_by_id) | set(baseline_by_id))

        entries: list[SourceAuditDriftEntry] = []
        counts = {
            "changed": 0,
            "unchanged": 0,
            "new": 0,
            "missing": 0,
            "regressed": 0,
            "recovered": 0,
        }

        for document_id in all_ids:
            current_entry = current_by_id.get(document_id)
            baseline_entry = baseline_by_id.get(document_id)
            drift_entry = self._compare_audit_entries(current_entry=current_entry, baseline_entry=baseline_entry)
            counts[drift_entry.status] += 1
            entries.append(drift_entry)

        return SourceAuditComparisonReport(
            baseline_generated_at=baseline.generated_at,
            current_generated_at=current.generated_at,
            changed_count=counts["changed"],
            unchanged_count=counts["unchanged"],
            new_count=counts["new"],
            missing_count=counts["missing"],
            regressed_count=counts["regressed"],
            recovered_count=counts["recovered"],
            entries=entries,
        )

    async def compare_audit_to_baseline(
        self,
        *,
        library: str | None = None,
        component: str | None = None,
        limit: int | None = None,
        baseline_path: str | None = None,
        snapshot_dir: str | None = None,
    ) -> tuple[SourceAuditReport, SourceAuditComparisonReport | None, str]:
        report = await self.audit_sources(
            library=library,
            component=component,
            limit=limit,
            snapshot_dir=snapshot_dir,
        )
        path = baseline_path or str(self.default_audit_baseline_path())
        baseline = self.load_audit_baseline(baseline_path=baseline_path)
        if baseline is None:
            return report, None, path
        comparison = self.compare_audit_reports(report, baseline)
        return report, comparison, path

    async def build_audit_maintenance_report(
        self,
        *,
        library: str | None = None,
        component: str | None = None,
        limit: int | None = None,
        baseline_path: str | None = None,
        snapshot_dir: str | None = None,
    ) -> tuple[SourceAuditReport, SourceAuditComparisonReport | None, SourceAuditMaintenanceReport, str]:
        report, comparison, resolved_baseline_path = await self.compare_audit_to_baseline(
            library=library,
            component=component,
            limit=limit,
            baseline_path=baseline_path,
            snapshot_dir=snapshot_dir,
        )
        maintenance_report = self.generate_audit_maintenance_report(
            report,
            comparison=comparison,
            baseline_path=resolved_baseline_path,
        )
        return report, comparison, maintenance_report, resolved_baseline_path

    def default_audit_report_dir(self) -> Path:
        return self.settings.audit_dir / "reports"

    def save_audit_maintenance_artifacts(
        self,
        report: SourceAuditReport,
        *,
        comparison: SourceAuditComparisonReport | None = None,
        maintenance_report: SourceAuditMaintenanceReport,
        output_dir: str | None = None,
        library: str | None = None,
        component: str | None = None,
    ) -> tuple[str, str]:
        directory = Path(output_dir) if output_dir else self.default_audit_report_dir()
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = maintenance_report.generated_at.strftime("%Y%m%dT%H%M%SZ")
        scope_parts = [slugify(part) for part in (library, component) if part]
        scope_label = "-".join(scope_parts) if scope_parts else "catalog"
        stem = f"{timestamp}-{scope_label}-maintenance"

        json_path = directory / f"{stem}.json"
        markdown_path = directory / f"{stem}.md"
        json_payload = {
            "report": report.model_dump(mode="json"),
            "comparison": comparison.model_dump(mode="json") if comparison else None,
            "maintenance_report": maintenance_report.model_dump(mode="json"),
        }
        json_path.write_text(json.dumps(json_payload, indent=2), encoding="utf-8")
        markdown_path.write_text(
            self.render_audit_maintenance_report_markdown(maintenance_report),
            encoding="utf-8",
        )
        return str(json_path), str(markdown_path)

    async def promote_audit_baseline(
        self,
        *,
        library: str | None = None,
        component: str | None = None,
        limit: int | None = None,
        baseline_path: str | None = None,
        snapshot_dir: str | None = None,
        report_dir: str | None = None,
        max_allowed_severity: AuditSeverity = AuditSeverity.warn,
        force: bool = False,
    ) -> tuple[SourceAuditReport, SourceAuditComparisonReport | None, BaselinePromotionResult]:
        report, comparison, maintenance_report, resolved_baseline_path = await self.build_audit_maintenance_report(
            library=library,
            component=component,
            limit=limit,
            baseline_path=baseline_path,
            snapshot_dir=snapshot_dir,
        )
        report_json_path, report_markdown_path = self.save_audit_maintenance_artifacts(
            report,
            comparison=comparison,
            maintenance_report=maintenance_report,
            output_dir=report_dir,
            library=library,
            component=component,
        )
        blocking_recommendations = [
            recommendation
            for recommendation in maintenance_report.recommendations
            if self._severity_rank(recommendation.severity) >= self._severity_rank(max_allowed_severity)
        ]
        if blocking_recommendations and not force:
            return report, comparison, BaselinePromotionResult(
                promoted=False,
                forced=False,
                baseline_path=resolved_baseline_path,
                report_json_path=report_json_path,
                report_markdown_path=report_markdown_path,
                blocking_severity=max_allowed_severity,
                blocking_recommendation_count=len(blocking_recommendations),
                blocking_recommendations=blocking_recommendations,
                maintenance_report=maintenance_report,
                message="Baseline promotion was blocked by maintenance recommendations.",
            )

        saved_baseline_path = self.save_audit_baseline(report, baseline_path=baseline_path)
        message = "Baseline promoted successfully."
        if blocking_recommendations and force:
            message = "Baseline promoted with force despite blocking maintenance recommendations."
        return report, comparison, BaselinePromotionResult(
            promoted=True,
            forced=force,
            baseline_path=saved_baseline_path,
            report_json_path=report_json_path,
            report_markdown_path=report_markdown_path,
            blocking_severity=max_allowed_severity if blocking_recommendations else None,
            blocking_recommendation_count=len(blocking_recommendations),
            blocking_recommendations=blocking_recommendations,
            maintenance_report=maintenance_report.model_copy(update={"baseline_path": saved_baseline_path}),
            message=message,
        )

    def generate_audit_maintenance_report(
        self,
        report: SourceAuditReport,
        *,
        comparison: SourceAuditComparisonReport | None = None,
        baseline_path: str | None = None,
    ) -> SourceAuditMaintenanceReport:
        recommendations: list[SourceAuditMaintenanceRecommendation] = []
        seen_keys: set[tuple[str, str, str | None]] = set()

        for entry in report.entries:
            recommendation = self._build_audit_entry_recommendation(entry)
            if recommendation is None:
                continue
            dedupe_key = (recommendation.document_id, recommendation.category, recommendation.drift_status)
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            recommendations.append(recommendation)

        if comparison is not None:
            for entry in comparison.entries:
                recommendation = self._build_drift_recommendation(entry)
                if recommendation is None:
                    continue
                dedupe_key = (recommendation.document_id, recommendation.category, recommendation.drift_status)
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)
                recommendations.append(recommendation)

        recommendations.sort(
            key=lambda item: (
                -self._severity_rank(item.severity),
                item.library,
                item.component,
                item.doc_type,
                item.category,
            ),
        )

        counts = {
            AuditSeverity.error: 0,
            AuditSeverity.warn: 0,
            AuditSeverity.info: 0,
        }
        for recommendation in recommendations:
            counts[recommendation.severity] += 1

        return SourceAuditMaintenanceReport(
            baseline_path=baseline_path,
            baseline_available=comparison is not None,
            comparison_available=comparison is not None,
            documents_scanned=len(report.entries),
            recommendation_count=len(recommendations),
            error_count=counts[AuditSeverity.error],
            warn_count=counts[AuditSeverity.warn],
            info_count=counts[AuditSeverity.info],
            recommendations=recommendations,
        )

    def render_audit_maintenance_report_markdown(self, report: SourceAuditMaintenanceReport) -> str:
        lines = [
            "# Catalog Maintenance Report",
            "",
            f"- Documents scanned: {report.documents_scanned}",
            f"- Recommendations: {report.recommendation_count}",
            f"- Errors: {report.error_count}",
            f"- Warnings: {report.warn_count}",
            f"- Info: {report.info_count}",
        ]
        if report.baseline_path:
            baseline_label = report.baseline_path if report.baseline_available else f"{report.baseline_path} (not found)"
            lines.append(f"- Baseline: {baseline_label}")

        if not report.recommendations:
            lines.extend(["", "No maintenance recommendations."])
            return "\n".join(lines)

        grouped: dict[AuditSeverity, list[SourceAuditMaintenanceRecommendation]] = {
            AuditSeverity.error: [],
            AuditSeverity.warn: [],
            AuditSeverity.info: [],
        }
        for recommendation in report.recommendations:
            grouped[recommendation.severity].append(recommendation)

        for severity in (AuditSeverity.error, AuditSeverity.warn, AuditSeverity.info):
            items = grouped[severity]
            if not items:
                continue
            lines.extend(["", f"## {severity.value.title()}"])
            for item in items:
                lines.append(
                    f"- `{item.document_id}`: {item.summary}"
                )
                if item.reasons:
                    lines.append(f"  Reasons: {', '.join(item.reasons)}")
                if item.recommended_actions:
                    lines.append(f"  Actions: {'; '.join(item.recommended_actions)}")
                if item.source_url:
                    lines.append(f"  Source: {item.source_url}")
        return "\n".join(lines)

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

    def _build_key_points(self, query: str, documents) -> list[str]:
        points: list[str] = []
        for document in documents:
            relevant_sections = sorted(
                document.sections,
                key=lambda section: self._section_relevance_score(section.title + "\n" + section.content, query),
                reverse=True,
            )
            if relevant_sections:
                top_section = relevant_sections[0]
                points.append(self._condense_text(top_section.content))
            elif document.content_md.strip():
                points.append(self._condense_text(document.content_md))
            if len(points) >= 4:
                break
        return unique_strings(points)[:4]

    def _build_summary(
        self,
        *,
        library: str,
        component: str,
        key_points: list[str],
        api_highlights: list[str],
        accessibility_highlights: list[str],
    ) -> str:
        intro = f"Best match: {library} {component}."
        parts = [intro]
        if key_points:
            parts.append(key_points[0])
        if api_highlights:
            parts.append(f"Key API details include {', '.join(api_highlights[:2])}.")
        elif accessibility_highlights:
            parts.append(f"Important accessibility guidance includes {', '.join(accessibility_highlights[:2])}.")
        return " ".join(part.strip() for part in parts if part.strip())

    def _select_relevant_strings(self, values: Iterable[str], *, query: str, limit: int) -> list[str]:
        ranked = sorted(
            unique_strings(values),
            key=lambda value: (self._section_relevance_score(value, query), len(value)),
            reverse=True,
        )
        filtered = [self._condense_text(value) for value in ranked if value.strip()]
        return filtered[:limit]

    def _section_relevance_score(self, text: str, query: str) -> float:
        query_tokens = set(tokenize(query))
        if not query_tokens:
            return 0.0
        text_tokens = set(tokenize(text))
        overlap = query_tokens & text_tokens
        return float(len(overlap))

    def _condense_text(self, text: str, *, max_length: int = 220) -> str:
        cleaned = " ".join(text.split())
        if len(cleaned) <= max_length:
            return cleaned
        for delimiter in (". ", "; ", ": "):
            index = cleaned.find(delimiter)
            if 0 < index <= max_length:
                return cleaned[: index + 1].strip()
        return cleaned[: max_length - 3].rstrip() + "..."

    def _audit_warnings(self, document) -> list[str]:
        warnings: list[str] = []
        if len(document.content_md.strip()) < 200:
            warnings.append("content_short")
        if not document.sections:
            warnings.append("no_sections")
        if document.doc_type == "api" and not document.api_items:
            warnings.append("api_items_missing")
        if document.doc_type == "accessibility" and not document.accessibility_notes:
            warnings.append("accessibility_notes_missing")
        if document.doc_type == "overview" and not document.code_examples:
            warnings.append("examples_missing")
        return warnings

    def _write_audit_snapshot(self, snapshot_base: Path, document, raw_content: str) -> str:
        stem = document.document_id.replace(":", "__")
        raw_path = snapshot_base / f"{stem}.raw.html"
        normalized_path = snapshot_base / f"{stem}.normalized.json"
        raw_path.write_text(raw_content, encoding="utf-8")
        normalized_path.write_text(document.model_dump_json(indent=2), encoding="utf-8")
        return str(raw_path)

    def _build_audit_entry_recommendation(
        self,
        entry: SourceAuditEntry,
    ) -> SourceAuditMaintenanceRecommendation | None:
        if entry.fetch_status == "failure":
            return SourceAuditMaintenanceRecommendation(
                document_id=entry.document_id,
                library=entry.library,
                component=entry.component,
                doc_type=entry.doc_type,
                severity=AuditSeverity.error,
                category="fetch_failure",
                summary=f"Source fetch failed for {entry.library} {entry.component} {entry.doc_type}.",
                reasons=unique_strings(["fetch_failed", entry.error or "unknown_error"]),
                recommended_actions=[
                    "Verify the upstream URL still exists and is reachable.",
                    "Check whether the catalog entry or parser selectors need to be updated.",
                    "Re-run the catalog audit with snapshots enabled to inspect the raw response.",
                ],
                source_url=entry.url,
            )

        if not entry.warnings:
            return None

        warning_actions = {
            "content_short": "Review the content selector and page structure for truncated extraction.",
            "no_sections": "Update parsing selectors so the normalized document keeps meaningful sections.",
            "api_items_missing": "Tighten the API extraction rules so props and arguments are captured explicitly.",
            "accessibility_notes_missing": "Review accessibility parsing so guidance is promoted into structured notes.",
            "examples_missing": "Inspect example extraction selectors for code blocks or embedded demos.",
        }
        actions = [warning_actions[warning] for warning in entry.warnings if warning in warning_actions]
        actions.append("Re-run the catalog audit after parser changes to confirm the warning is cleared.")
        return SourceAuditMaintenanceRecommendation(
            document_id=entry.document_id,
            library=entry.library,
            component=entry.component,
            doc_type=entry.doc_type,
            severity=AuditSeverity.warn,
            category="warning",
            summary=f"Normalized output quality warnings were detected for {entry.library} {entry.component} {entry.doc_type}.",
            reasons=entry.warnings,
            recommended_actions=unique_strings(actions),
            source_url=entry.url,
        )

    def _compare_audit_entries(
        self,
        *,
        current_entry: SourceAuditEntry | None,
        baseline_entry: SourceAuditEntry | None,
    ) -> SourceAuditDriftEntry:
        reference = current_entry or baseline_entry
        assert reference is not None

        if baseline_entry is None and current_entry is not None:
            return SourceAuditDriftEntry(
                document_id=current_entry.document_id,
                library=current_entry.library,
                component=current_entry.component,
                doc_type=current_entry.doc_type,
                status="new",
                changes=["new_entry"],
                current=current_entry,
            )

        if current_entry is None and baseline_entry is not None:
            return SourceAuditDriftEntry(
                document_id=baseline_entry.document_id,
                library=baseline_entry.library,
                component=baseline_entry.component,
                doc_type=baseline_entry.doc_type,
                status="missing",
                changes=["missing_from_current_audit"],
                baseline=baseline_entry,
            )

        assert current_entry is not None and baseline_entry is not None
        changes: list[str] = []
        status = "unchanged"

        if baseline_entry.fetch_status == "success" and current_entry.fetch_status == "failure":
            status = "regressed"
            changes.append("fetch_status:success->failure")
        elif baseline_entry.fetch_status == "failure" and current_entry.fetch_status == "success":
            status = "recovered"
            changes.append("fetch_status:failure->success")
        else:
            metric_fields = (
                ("content_checksum", current_entry.content_checksum, baseline_entry.content_checksum),
                ("content_length", current_entry.content_length, baseline_entry.content_length),
                ("section_count", current_entry.section_count, baseline_entry.section_count),
                ("api_item_count", current_entry.api_item_count, baseline_entry.api_item_count),
                (
                    "accessibility_note_count",
                    current_entry.accessibility_note_count,
                    baseline_entry.accessibility_note_count,
                ),
                ("example_count", current_entry.example_count, baseline_entry.example_count),
            )
            for field_name, current_value, baseline_value in metric_fields:
                if current_value != baseline_value:
                    changes.append(f"{field_name}:{baseline_value}->{current_value}")

            current_warnings = sorted(current_entry.warnings)
            baseline_warnings = sorted(baseline_entry.warnings)
            if current_warnings != baseline_warnings:
                changes.append(f"warnings:{','.join(baseline_warnings)}->{','.join(current_warnings)}")

            if changes:
                status = "changed"

        return SourceAuditDriftEntry(
            document_id=reference.document_id,
            library=reference.library,
            component=reference.component,
            doc_type=reference.doc_type,
            status=status,
            changes=changes,
            current=current_entry,
            baseline=baseline_entry,
        )

    def _build_drift_recommendation(
        self,
        entry: SourceAuditDriftEntry,
    ) -> SourceAuditMaintenanceRecommendation | None:
        if entry.status == "unchanged":
            return None

        current_entry = entry.current
        baseline_entry = entry.baseline
        reference = current_entry or baseline_entry
        assert reference is not None

        severity = AuditSeverity.info
        if entry.status in {"regressed", "missing"}:
            severity = AuditSeverity.error
        elif entry.status == "changed":
            severity = AuditSeverity.warn if self._is_structural_drift(entry.changes) else AuditSeverity.info

        summary_by_status = {
            "changed": f"Catalog output changed for {reference.library} {reference.component} {reference.doc_type}.",
            "new": f"New catalog entry detected for {reference.library} {reference.component} {reference.doc_type}.",
            "missing": f"Baseline entry is missing from the current catalog audit for {reference.library} {reference.component} {reference.doc_type}.",
            "regressed": f"Catalog fetch regressed for {reference.library} {reference.component} {reference.doc_type}.",
            "recovered": f"Catalog fetch recovered for {reference.library} {reference.component} {reference.doc_type}.",
        }
        actions = self._drift_actions(entry)
        return SourceAuditMaintenanceRecommendation(
            document_id=reference.document_id,
            library=reference.library,
            component=reference.component,
            doc_type=reference.doc_type,
            severity=severity,
            category="drift",
            summary=summary_by_status[entry.status],
            reasons=entry.changes,
            recommended_actions=actions,
            source_url=(current_entry.url if current_entry else baseline_entry.url if baseline_entry else None),
            drift_status=entry.status,
        )

    def _drift_actions(self, entry: SourceAuditDriftEntry) -> list[str]:
        if entry.status == "new":
            return [
                "Review the new source entry and decide whether it should be kept in the catalog baseline.",
                "Write a fresh audit baseline if the new entry is expected.",
            ]
        if entry.status == "missing":
            return [
                "Confirm whether the upstream page was removed or the catalog entry drifted.",
                "Restore the missing source entry or update the baseline if the removal is intentional.",
            ]
        if entry.status == "regressed":
            return [
                "Compare the failing current source with the last known-good baseline.",
                "Verify the source URL, response shape, and parser selectors before refreshing the baseline.",
            ]
        if entry.status == "recovered":
            return [
                "Spot-check the recovered document output against the baseline and current upstream page.",
                "Update the baseline if the recovered output is now the expected shape.",
            ]
        if self._is_structural_drift(entry.changes):
            return [
                "Inspect structural extraction changes such as sections, API items, examples, or accessibility notes.",
                "Update selectors or parsing rules if the structure loss is unintended.",
                "Refresh the baseline only after confirming the new structure is correct.",
            ]
        return [
            "Review the upstream content change and confirm it is expected.",
            "Refresh the baseline if the new content is correct.",
        ]

    def _is_structural_drift(self, changes: Iterable[str]) -> bool:
        structural_fields = {
            "section_count",
            "api_item_count",
            "accessibility_note_count",
            "example_count",
            "warnings",
            "fetch_status",
        }
        for change in changes:
            field_name = change.split(":", 1)[0]
            if field_name in structural_fields:
                return True
        return False

    def _severity_rank(self, severity: AuditSeverity) -> int:
        return {
            AuditSeverity.info: 1,
            AuditSeverity.warn: 2,
            AuditSeverity.error: 3,
        }[severity]

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
