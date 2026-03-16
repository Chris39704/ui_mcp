from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ui_knowledge_service.app import create_app
from ui_knowledge_service.cli import _run_stdio_command
from ui_knowledge_service.config import Settings
from ui_knowledge_service.models import Citation, ComponentDocument, FetchedSource, RefreshRequest, SourceDescriptor
from ui_knowledge_service.service import KnowledgeService
from ui_knowledge_service.utils import sha256_text, utcnow


class FakeAdapter:
    def __init__(self, library: str, content: str, *, should_fail: bool = False, docs: dict[str, str] | None = None):
        self.library = library
        self.should_fail = should_fail
        self.docs = docs or {"overview": content}
        self.descriptors = {
            doc_type: SourceDescriptor(
                library=library,
                component="button",
                doc_type=doc_type,
                title=f"Fake Button {doc_type.title()}",
                url=f"https://example.com/{library}/button/{doc_type}",
                source_kind="api_reference" if doc_type == "api" else "docs_page",
                freshness_days=1,
            )
            for doc_type in self.docs
        }

    def discover(self) -> list[SourceDescriptor]:
        return list(self.descriptors.values())

    def list_for_component(self, component: str) -> list[SourceDescriptor]:
        if component not in {"button", "buttons"}:
            return []
        order = {"overview": 0, "api": 1, "accessibility": 2, "examples": 3}
        return sorted(self.descriptors.values(), key=lambda item: (order.get(item.doc_type, 100), item.doc_type))

    def resolve(self, component: str, doc_type: str | None = None) -> SourceDescriptor | None:
        if component not in {"button", "buttons"}:
            return None
        if doc_type:
            return self.descriptors.get(doc_type)
        descriptors = self.list_for_component(component)
        return descriptors[0] if descriptors else None

    async def fetch(self, descriptor: SourceDescriptor, *, etag=None, last_modified=None) -> FetchedSource:
        if self.should_fail:
            raise RuntimeError("offline")
        content = self.docs[descriptor.doc_type]
        return FetchedSource(
            descriptor=descriptor,
            content=f"<main><h1>{descriptor.title}</h1><p>{content}</p><pre><code>{descriptor.doc_type}_button()</code></pre></main>",
            content_type="text/html",
            etag="fake-etag",
            last_modified="Mon, 01 Jan 2024 00:00:00 GMT",
        )

    def normalize(self, fetched: FetchedSource, *, raw_path: str | None = None) -> ComponentDocument:
        content = self.docs[fetched.descriptor.doc_type]
        return ComponentDocument(
            library=fetched.descriptor.library,
            component=fetched.descriptor.component,
            doc_type=fetched.descriptor.doc_type,
            title=fetched.descriptor.title,
            content_md=content,
            code_examples=[f"{fetched.descriptor.doc_type}_button()"],
            source_url=fetched.descriptor.url,
            source_kind=fetched.descriptor.source_kind,
            version="1.0.0",
            etag=fetched.etag,
            last_modified=fetched.last_modified,
            checksum=sha256_text(content),
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
        assert stored.source_url == "https://example.com/mui/button/overview"
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
async def test_component_bundle_returns_multiple_doc_types(tmp_path):
    service = build_service(
        tmp_path,
        adapter=FakeAdapter("mui", "Primary action button", docs={"overview": "Overview content", "api": "API content"}),
    )
    await service.startup()
    try:
        response = await service.get_component_bundle("mui", "button")
        assert len(response.documents) == 2
        assert response.available_doc_types == ["overview", "api"]
        assert {document.doc_type for document in response.documents} == {"overview", "api"}
        assert response.retrieval_path == "source_fetch"
    finally:
        await service.shutdown()


@pytest.mark.anyio
async def test_search_prefers_api_doc_type_for_api_queries(tmp_path):
    service = build_service(
        tmp_path,
        adapter=FakeAdapter(
            "mui",
            "Overview content",
            docs={
                "overview": "Usage guidance for buttons",
                "api": "variant prop disabled prop onClick callback API reference",
            },
        ),
    )
    await service.startup()
    try:
        await service.refresh(RefreshRequest(library="mui", component="button"))
        response = await service.search_component_docs(
            "button props variant",
            library="mui",
            component_hint="button",
        )
        assert response.results
        assert response.results[0].doc_type == "api"
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
    service = build_service(
        tmp_path,
        adapter=FakeAdapter("mui", "Primary action button", docs={"overview": "Overview content", "api": "API content"}),
    )
    app = create_app(service=service)

    with TestClient(app) as client:
        health = client.get("/health")
        refresh = client.post("/refresh", json={"library": "mui", "component": "button"})
        document = client.get("/documents/mui/button")
        sources = client.get("/sources")
        search = client.get("/search", params={"query": "overview content", "library": "mui"})
        bundle = client.get("/bundles/mui/button")
        status = client.get("/status/mui/button")
        refresh_status = client.get("/refresh/status")

    assert health.status_code == 200
    assert refresh.status_code == 200
    assert document.status_code == 200
    assert sources.status_code == 200
    assert search.status_code == 200
    assert bundle.status_code == 200
    assert status.status_code == 200
    assert refresh_status.status_code == 200
    assert document.json()["document"]["component"] == "button"
    assert len(refresh.json()["refreshed_documents"]) == 2
    assert search.json()["results"]
    assert len(bundle.json()["documents"]) == 2
    assert status.json()["last_refresh_status"] in {"success", "not_modified"}
    assert refresh_status.json()["total_attempts"] >= 2


@pytest.mark.anyio
async def test_mcp_tool_roundtrip(tmp_path):
    service = build_service(
        tmp_path,
        adapter=FakeAdapter("mui", "Primary action button", docs={"overview": "Overview content", "api": "API content"}),
    )
    await service.startup()
    try:
        from ui_knowledge_service.mcp_server import build_mcp_server

        mcp_server = build_mcp_server(service)
        tools = await mcp_server.list_tools()
        result = await mcp_server.call_tool("get_component_doc", {"library": "mui", "component": "button"})
        structured = result[1]
        bundle_result = await mcp_server.call_tool("get_component_bundle", {"library": "mui", "component": "button"})
        structured_bundle = bundle_result[1]

        assert any(tool.name == "get_component_doc" for tool in tools)
        assert any(tool.name == "get_component_bundle" for tool in tools)
        assert structured["document"]["component"] == "button"
        assert len(structured_bundle["documents"]) == 2
    finally:
        await service.shutdown()


@pytest.mark.anyio
async def test_stdio_runner_starts_and_stops_service(tmp_path):
    service = SimpleNamespace(started=0, stopped=0)

    async def startup():
        service.started += 1

    async def shutdown():
        service.stopped += 1

    service.startup = startup
    service.shutdown = shutdown

    class FakeMcpServer:
        def __init__(self):
            self.ran = False

        async def run_stdio_async(self):
            self.ran = True

    fake_mcp = FakeMcpServer()
    settings = Settings(data_dir=tmp_path / "data")

    await _run_stdio_command(settings, service=service, mcp_factory=lambda _: fake_mcp)

    assert service.started == 1
    assert fake_mcp.ran is True
    assert service.stopped == 1
