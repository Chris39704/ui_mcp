"""Core models shared across the service."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ui_knowledge_service.utils import slugify, utcnow


class FreshnessState(str, Enum):
    fresh = "fresh"
    stale = "stale"
    missing = "missing"


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    url: str
    accessed_at: datetime = Field(default_factory=utcnow)


class DocumentSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["summary", "usage", "api", "accessibility", "examples", "reference"]
    title: str
    content: str


class SourceDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    library: str
    component: str
    doc_type: str = "overview"
    title: str
    url: str
    source_kind: str = "docs_page"
    aliases: tuple[str, ...] = ()
    freshness_days: int = 7
    content_selector: str | None = None
    heading_selector: str | None = None
    code_selector: str | None = None
    exclude_selectors: tuple[str, ...] = ()

    @property
    def component_slug(self) -> str:
        return slugify(self.component)

    @property
    def document_id(self) -> str:
        return f"{self.library}:{self.component_slug}:{self.doc_type}"


class FetchedSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    descriptor: SourceDescriptor
    content: str = ""
    content_type: str = "text/plain"
    fetched_at: datetime = Field(default_factory=utcnow)
    etag: str | None = None
    last_modified: str | None = None
    version: str | None = None
    not_modified: bool = False


class ComponentDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    library: str
    component: str
    doc_type: str
    title: str
    content_md: str
    code_examples: list[str] = Field(default_factory=list)
    sections: list[DocumentSection] = Field(default_factory=list)
    api_items: list[str] = Field(default_factory=list)
    accessibility_notes: list[str] = Field(default_factory=list)
    source_url: str
    source_kind: str
    version: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    checksum: str
    fetched_at: datetime = Field(default_factory=utcnow)
    stale_after: datetime = Field(default_factory=utcnow)
    citations: list[Citation] = Field(default_factory=list)
    raw_path: str | None = None
    normalized_path: str | None = None

    @property
    def document_id(self) -> str:
        return f"{self.library}:{slugify(self.component)}:{self.doc_type}"

    def freshness_state(self, now: datetime | None = None) -> FreshnessState:
        reference = now or utcnow()
        if self.stale_after <= reference:
            return FreshnessState.stale
        return FreshnessState.fresh

    def searchable_text(self) -> str:
        parts = [self.title, self.content_md]
        if self.api_items:
            parts.append("API ITEMS\n" + "\n".join(self.api_items))
        if self.accessibility_notes:
            parts.append("ACCESSIBILITY\n" + "\n".join(self.accessibility_notes))
        for section in self.sections:
            parts.append(f"{section.kind.upper()} {section.title}\n{section.content}")
        return "\n\n".join(part for part in parts if part.strip())


class SearchHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    library: str
    component: str
    doc_type: str
    title: str
    source_url: str
    score: float
    snippet: str
    matched_by: Literal["exact", "fts", "vector"]
    freshness_state: FreshnessState


class ComponentDocResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document: ComponentDocument | None = None
    freshness_state: FreshnessState = FreshnessState.missing
    retrieval_path: str
    refreshed: bool = False
    suggestions: list[str] = Field(default_factory=list)
    message: str | None = None


class ComponentBundleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    library: str
    component: str
    documents: list[ComponentDocument] = Field(default_factory=list)
    available_doc_types: list[str] = Field(default_factory=list)
    freshness_state: FreshnessState = FreshnessState.missing
    retrieval_path: str
    refreshed_documents: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    message: str | None = None


class SearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    library: str | None = None
    component_hint: str | None = None
    results: list[SearchHit] = Field(default_factory=list)
    retrieval_path: str = "fts"


class ResolvedSupportingDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    library: str
    component: str
    doc_type: str
    title: str
    source_url: str
    freshness_state: FreshnessState


class ResolvedComponentAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    library: str | None = None
    component: str | None = None
    summary: str = ""
    key_points: list[str] = Field(default_factory=list)
    api_highlights: list[str] = Field(default_factory=list)
    accessibility_highlights: list[str] = Field(default_factory=list)
    example_snippets: list[str] = Field(default_factory=list)
    supporting_documents: list[ResolvedSupportingDocument] = Field(default_factory=list)
    freshness_state: FreshnessState = FreshnessState.missing
    retrieval_path: str = "miss"
    suggestions: list[str] = Field(default_factory=list)
    message: str | None = None


class ComponentStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    freshness_state: FreshnessState
    source_url: str
    source_kind: str
    fetched_at: datetime
    stale_after: datetime
    version: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    last_refresh_status: str | None = None
    last_refresh_error: str | None = None
    last_refresh_attempted_at: datetime | None = None


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    library: str | None = None
    component: str | None = None
    doc_type: str | None = None
    force: bool = False
    prewarm: bool = False


class RefreshResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refreshed_documents: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class RefreshRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    library: str
    component: str
    doc_type: str
    status: Literal["success", "not_modified", "failure"]
    attempted_at: datetime = Field(default_factory=utcnow)
    error: str | None = None


class RefreshStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_attempts: int = 0
    success_count: int = 0
    not_modified_count: int = 0
    failure_count: int = 0
    records: list[RefreshRecord] = Field(default_factory=list)


class SourceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    library: str
    component_count: int
    components: list[str]
    doc_type_count: int = 0


class SourceAuditEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    library: str
    component: str
    doc_type: str
    url: str
    fetch_status: Literal["success", "failure"]
    content_length: int = 0
    content_checksum: str | None = None
    section_count: int = 0
    api_item_count: int = 0
    accessibility_note_count: int = 0
    example_count: int = 0
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    snapshot_path: str | None = None


class SourceAuditReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: datetime = Field(default_factory=utcnow)
    entries: list[SourceAuditEntry] = Field(default_factory=list)


class SourceAuditDriftEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    library: str
    component: str
    doc_type: str
    status: Literal["unchanged", "changed", "new", "missing", "regressed", "recovered"]
    changes: list[str] = Field(default_factory=list)
    current: SourceAuditEntry | None = None
    baseline: SourceAuditEntry | None = None


class SourceAuditComparisonReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: datetime = Field(default_factory=utcnow)
    baseline_generated_at: datetime | None = None
    current_generated_at: datetime | None = None
    changed_count: int = 0
    unchanged_count: int = 0
    new_count: int = 0
    missing_count: int = 0
    regressed_count: int = 0
    recovered_count: int = 0
    entries: list[SourceAuditDriftEntry] = Field(default_factory=list)


class AuditSeverity(str, Enum):
    info = "info"
    warn = "warn"
    error = "error"


class SourceAuditMaintenanceRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    library: str
    component: str
    doc_type: str
    severity: AuditSeverity
    category: Literal["fetch_failure", "warning", "drift", "baseline"]
    summary: str
    reasons: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    source_url: str | None = None
    drift_status: Literal["unchanged", "changed", "new", "missing", "regressed", "recovered"] | None = None


class SourceAuditMaintenanceReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: datetime = Field(default_factory=utcnow)
    baseline_path: str | None = None
    baseline_available: bool = False
    comparison_available: bool = False
    documents_scanned: int = 0
    recommendation_count: int = 0
    error_count: int = 0
    warn_count: int = 0
    info_count: int = 0
    recommendations: list[SourceAuditMaintenanceRecommendation] = Field(default_factory=list)


class BaselinePromotionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    promoted: bool = False
    forced: bool = False
    baseline_path: str
    report_json_path: str | None = None
    report_markdown_path: str | None = None
    blocking_severity: AuditSeverity | None = None
    blocking_recommendation_count: int = 0
    blocking_recommendations: list[SourceAuditMaintenanceRecommendation] = Field(default_factory=list)
    maintenance_report: SourceAuditMaintenanceReport
    message: str | None = None


def default_stale_after(days: int) -> datetime:
    return utcnow() + timedelta(days=days)
