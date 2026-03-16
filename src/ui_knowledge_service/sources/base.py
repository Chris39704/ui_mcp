"""Base source adapter behavior."""

from __future__ import annotations

from abc import ABC
from datetime import timedelta
from importlib import resources
import json
import logging
import re

from bs4 import BeautifulSoup
import httpx
from markdownify import markdownify as html_to_markdown

from ui_knowledge_service.config import Settings
from ui_knowledge_service.models import Citation, ComponentDocument, FetchedSource, SourceDescriptor
from ui_knowledge_service.utils import sha256_text, slugify, unique_strings


LOGGER = logging.getLogger(__name__)


class BaseSourceAdapter(ABC):
    """Fetch and normalize official upstream content."""

    library: str

    def __init__(self, settings: Settings):
        self.settings = settings
        self._descriptors_by_id = {
            descriptor.document_id: descriptor for descriptor in self._build_catalog()
        }

    def discover(self) -> list[SourceDescriptor]:
        return list(self._descriptors_by_id.values())

    def list_for_component(self, component: str) -> list[SourceDescriptor]:
        component_slug = slugify(component)
        matches: list[SourceDescriptor] = []
        for descriptor in self._descriptors_by_id.values():
            if descriptor.component_slug == component_slug:
                matches.append(descriptor)
                continue
            aliases = {slugify(alias) for alias in descriptor.aliases}
            if component_slug in aliases:
                matches.append(descriptor)
        return sorted(matches, key=lambda item: (self._doc_type_rank(item.doc_type), item.doc_type, item.title))

    def resolve(self, component: str, doc_type: str | None = None) -> SourceDescriptor | None:
        candidates = self.list_for_component(component)
        if doc_type:
            for candidate in candidates:
                if candidate.doc_type == doc_type:
                    return candidate
            return None
        return candidates[0] if candidates else None

    async def fetch(
        self,
        descriptor: SourceDescriptor,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchedSource:
        headers = {"user-agent": self.settings.user_agent}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=headers) as client:
            response = await client.get(descriptor.url)
        if response.status_code == 304:
            return FetchedSource(
                descriptor=descriptor,
                content="",
                content_type=response.headers.get("content-type", "text/plain"),
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
                version=response.headers.get("x-version"),
                not_modified=True,
            )
        response.raise_for_status()
        LOGGER.info("Fetched %s for %s", descriptor.url, descriptor.document_id)
        return FetchedSource(
            descriptor=descriptor,
            content=response.text,
            content_type=response.headers.get("content-type", "text/plain"),
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
            version=response.headers.get("x-version"),
        )

    def normalize(self, fetched: FetchedSource, *, raw_path: str | None = None) -> ComponentDocument:
        title, content_md, examples = self._extract_document_parts(fetched)
        return ComponentDocument(
            library=fetched.descriptor.library,
            component=fetched.descriptor.component_slug,
            doc_type=fetched.descriptor.doc_type,
            title=title,
            content_md=content_md,
            code_examples=examples,
            source_url=fetched.descriptor.url,
            source_kind=fetched.descriptor.source_kind,
            version=fetched.version,
            etag=fetched.etag,
            last_modified=fetched.last_modified,
            checksum=sha256_text(content_md),
            fetched_at=fetched.fetched_at,
            stale_after=fetched.fetched_at + self.freshness_hint(fetched.descriptor),
            citations=self.citations(fetched.descriptor),
            raw_path=raw_path,
        )

    def freshness_hint(self, descriptor: SourceDescriptor) -> timedelta:
        return timedelta(days=descriptor.freshness_days)

    def citations(self, descriptor: SourceDescriptor) -> list[Citation]:
        return [Citation(label=descriptor.title, url=descriptor.url)]

    def _build_catalog(self) -> list[SourceDescriptor]:
        raise NotImplementedError

    def _load_catalog(self, filename: str) -> list[SourceDescriptor]:
        resource = resources.files("ui_knowledge_service.sources.catalogs").joinpath(filename)
        entries = json.loads(resource.read_text(encoding="utf-8"))
        return [SourceDescriptor.model_validate({"library": self.library, **entry}) for entry in entries]

    def _extract_document_parts(self, fetched: FetchedSource) -> tuple[str, str, list[str]]:
        content_type = fetched.content_type.lower()
        if "html" in content_type or fetched.descriptor.url.startswith("http"):
            return self._extract_from_html(fetched)
        return self._extract_from_markdown(fetched)

    def _extract_from_markdown(self, fetched: FetchedSource) -> tuple[str, str, list[str]]:
        content = fetched.content.strip()
        title = fetched.descriptor.title
        for line in content.splitlines():
            if line.startswith("#"):
                title = line.lstrip("# ").strip() or title
                break
        examples = re.findall(r"```[\w-]*\n(.*?)```", content, flags=re.DOTALL)
        return title, content, unique_strings(examples[:8])

    def _extract_from_html(self, fetched: FetchedSource) -> tuple[str, str, list[str]]:
        soup = BeautifulSoup(fetched.content, "html.parser")
        for selector in ("script", "style", "nav", "footer", "header", "aside", "noscript", "svg"):
            for node in soup.select(selector):
                node.decompose()
        for selector in fetched.descriptor.exclude_selectors:
            for node in soup.select(selector):
                node.decompose()

        if fetched.descriptor.content_selector:
            main = soup.select_one(fetched.descriptor.content_selector)
        else:
            main = soup.find("main") or soup.find("article") or soup.body or soup
        if main is None:
            main = soup.body or soup
        title = fetched.descriptor.title
        if fetched.descriptor.heading_selector:
            heading = main.select_one(fetched.descriptor.heading_selector) or soup.select_one(
                fetched.descriptor.heading_selector
            )
        else:
            heading = main.find(["h1", "title"])
        if heading:
            title = heading.get_text(" ", strip=True) or title
        code_selector = fetched.descriptor.code_selector or "pre code, pre"
        code_blocks = [code.get_text("\n", strip=True) for code in main.select(code_selector) if code.get_text(" ", strip=True)]
        markdown = html_to_markdown(str(main), heading_style="ATX", bullets="-")
        markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
        return title, markdown, unique_strings(code_blocks[:8])

    def _doc_type_rank(self, doc_type: str) -> int:
        order = {"overview": 0, "api": 1, "accessibility": 2, "examples": 3}
        return order.get(doc_type, 100)
