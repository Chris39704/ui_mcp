from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from ui_knowledge_service.app import create_app
from ui_knowledge_service.config import Settings
from ui_knowledge_service.models import Citation, ComponentDocument, FetchedSource, SourceDescriptor
from ui_knowledge_service.service import KnowledgeService
from ui_knowledge_service.utils import sha256_text, utcnow


class FakeAdapter:
    def __init__(self, library: str, content: str, *, should_fail: bool = False):
        self.library = library
        self.content = content
        self.should_fail = should_fail
        self.descriptor = SourceDescriptor(
            library=library,
            component="button",
            title="Fake Button",
            url=f"https://example.com/{library}/button",
            source_kind="docs_page",
            freshness_days=1,
        )

    def discover(self) -> list[SourceDescriptor]:
        return [self.descriptor]

    def resolve(self, component: str, doc_type: str | None = None) -> SourceDescriptor | None:
        if component in {"button", "buttons"}:
            return self.descriptor
        return None

    async def fetch(self, descriptor: SourceDescriptor, *, etag=None, last_modified=None) -> FetchedSource:
        if self.should_fail:
            raise RuntimeError("offline")
        return FetchedSource(
            descriptor=descriptor,
            content=f"<main><h1>{descriptor.title}</h1><p>{self.content}</p><pre><code>button()</code></pre></main>",
            content_type="text/html",
            etag="fake-etag",
            last_modified="Mon, 01 Jan 2024 00:00:00 GMT",
        )

    def normalize(self, fetched: FetchedSource, *, raw_path: str | None = None) -> ComponentDocument:
        return ComponentDocument(
            library=fetched.descriptor.library,
            component=fetched.descriptor.component,
            doc_type=fetched.descriptor.doc_type,
            title=fetched.descriptor.title,
            content_md=self.content,
            code_examples=["button()"],
            source_url=fetched.descriptor.url,
            source_kind=fetched.descriptor.source_kind,
            version="1.0.0",
            etag=fetched.etag,
            last_modified=fetched.last_modified,
            checksum=sha256_text(self.content),
            fetched_at=fetched.fetched_at,
            stale_after=fetched.fetched_at + timedelta(days=1),
            citations=[Citation(label=fetched.descriptor.title, url=fetched.descriptor.url)],
            raw_path=raw_path,
        )

    def freshness_hint(self, descriptor: SourceDescriptor) -> timedelta:
        return timedelta(days=1)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def build_service(tmp_path, *, adapter: FakeAdapter | None = None) -> KnowledgeService:
    settings = Settings(data_dir=tmp_path / "data")
    service = KnowledgeService(settings)
    service.adapters = {"mui": adapter or FakeAdapter("mui", "Primary action button")}
    return service


def test_store_backed_document_roundtrip(tmp_path):
    service = build_service(tmp_path)
    document = ComponentDocument(
        library="mui",
        component="button",
        doc_type="overview",
        title="Button",
        content_md="Primary action button",
        code_examples=["button()"],
        source_url="https://example.com/mui/button",
        source_kind="docs_page",
        version="1.0.0",
        checksum=sha256_text("Primary action button"),
        fetched_at=utcnow(),
        stale_after=utcnow() + timedelta(days=1),
        citations=[Citation(label="Button", url="https://example.com/mui/button")],
    )
    saved = service.store.save_document(document)
    loaded = service.store.get_document("mui", "button")
    hits = service.store.search_fts("primary action", library="mui")

    assert saved.normalized_path is not None
    assert loaded is not None
    assert loaded.document_id == saved.document_id
    assert hits
    assert hits[0].document_id == saved.document_id


@pytest.mark.anyio
async def test_cache_miss_fetches_and_persists(tmp_path):
    service = build_service(tmp_path)
    await service.startup()
    try:
        response = await service.get_component_doc("mui", "button")
        stored = service.store.get_document("mui", "button")

        assert response.document is not None
        assert response.retrieval_path == "source_fetch"
        assert stored is not None
        assert stored.source_url == "https://example.com/mui/button"
        assert stored.raw_path is not None
    finally:
        await service.shutdown()


@pytest.mark.anyio
async def test_stale_hit_returns_cached_and_refreshes_in_background(tmp_path):
    adapter = FakeAdapter("mui", "Fresh button content")
    service = build_service(tmp_path, adapter=adapter)
    stale_doc = ComponentDocument(
        library="mui",
        component="button",
        doc_type="overview",
        title="Button",
        content_md="Old stale content",
        code_examples=["button_old()"],
        source_url="https://example.com/mui/button",
        source_kind="docs_page",
        version="0.9.0",
        checksum=sha256_text("Old stale content"),
        fetched_at=utcnow() - timedelta(days=2),
        stale_after=utcnow() - timedelta(hours=1),
        citations=[Citation(label="Button", url="https://example.com/mui/button")],
    )
    service.store.save_document(stale_doc)
    await service.startup()
    try:
        response = await service.get_component_doc("mui", "button")
        assert response.document is not None
        assert response.freshness_state.value == "stale"
        assert response.document.content_md == "Old stale content"

        await asyncio.gather(*service._refresh_tasks.values())
        refreshed = service.store.get_document("mui", "button")
        assert refreshed is not None
        assert refreshed.content_md == "Fresh button content"
    finally:
        await service.shutdown()


@pytest.mark.anyio
async def test_miss_returns_clear_message_when_refresh_fails(tmp_path):
    service = build_service(tmp_path, adapter=FakeAdapter("mui", "unused", should_fail=True))
    await service.startup()
    try:
        response = await service.get_component_doc("mui", "button")
        assert response.document is None
        assert response.freshness_state.value == "missing"
        assert "upstream refresh" in (response.message or "")
    finally:
        await service.shutdown()


@pytest.mark.anyio
async def test_search_uses_vector_fallback_when_fts_is_empty(tmp_path, monkeypatch):
    service = build_service(tmp_path)
    document = ComponentDocument(
        library="mui",
        component="button",
        doc_type="overview",
        title="Button",
        content_md="primary action button for submit flows",
        code_examples=["button()"],
        source_url="https://example.com/mui/button",
        source_kind="docs_page",
        version="1.0.0",
        checksum=sha256_text("primary action button for submit flows"),
        fetched_at=utcnow(),
        stale_after=utcnow() + timedelta(days=1),
        citations=[Citation(label="Button", url="https://example.com/mui/button")],
    )
    service.store.save_document(document)
    service.vector_index.rebuild([document])
    monkeypatch.setattr(service.store, "search_fts", lambda *args, **kwargs: [])
    await service.startup()
    try:
        response = await service.search_component_docs("primary action button", library="mui")
        assert response.results
        assert response.results[0].matched_by == "vector"
    finally:
        await service.shutdown()


def test_admin_api_endpoints(tmp_path):
    service = build_service(tmp_path)
    app = create_app(service=service)

    with TestClient(app) as client:
        health = client.get("/health")
        refresh = client.post("/refresh", json={"library": "mui", "component": "button"})
        document = client.get("/documents/mui/button")
        sources = client.get("/sources")

    assert health.status_code == 200
    assert refresh.status_code == 200
    assert document.status_code == 200
    assert sources.status_code == 200
    assert document.json()["document"]["component"] == "button"


@pytest.mark.anyio
async def test_mcp_tool_roundtrip(tmp_path):
    service = build_service(tmp_path)
    await service.startup()
    try:
        from ui_knowledge_service.mcp_server import build_mcp_server

        mcp_server = build_mcp_server(service)
        tools = await mcp_server.list_tools()
        result = await mcp_server.call_tool("get_component_doc", {"library": "mui", "component": "button"})
        structured = result[1]

        assert any(tool.name == "get_component_doc" for tool in tools)
        assert structured["document"]["component"] == "button"
    finally:
        await service.shutdown()
