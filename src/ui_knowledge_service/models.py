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


class SearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    library: str | None = None
    component_hint: str | None = None
    results: list[SearchHit] = Field(default_factory=list)
    retrieval_path: str = "fts"


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


class SourceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    library: str
    component_count: int
    components: list[str]


def default_stale_after(days: int) -> datetime:
    return utcnow() + timedelta(days=days)
