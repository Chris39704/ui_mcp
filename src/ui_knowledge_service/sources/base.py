"""Base source adapter behavior."""

from __future__ import annotations

from abc import ABC
from datetime import timedelta
from importlib import resources
import json
import logging
import re

from bs4 import BeautifulSoup
from bs4 import Tag
import httpx
from markdownify import markdownify as html_to_markdown

from ui_knowledge_service.config import Settings
from ui_knowledge_service.models import Citation, ComponentDocument, DocumentSection, FetchedSource, SourceDescriptor
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
        title, content_md, examples, sections, api_items, accessibility_notes = self._extract_document_parts(fetched)
        return ComponentDocument(
            library=fetched.descriptor.library,
            component=fetched.descriptor.component_slug,
            doc_type=fetched.descriptor.doc_type,
            title=title,
            content_md=content_md,
            code_examples=examples,
            sections=sections,
            api_items=api_items,
            accessibility_notes=accessibility_notes,
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

    def _extract_document_parts(
        self, fetched: FetchedSource
    ) -> tuple[str, str, list[str], list[DocumentSection], list[str], list[str]]:
        content_type = fetched.content_type.lower()
        if "html" in content_type or fetched.descriptor.url.startswith("http"):
            return self._extract_from_html(fetched)
        return self._extract_from_markdown(fetched)

    def _extract_from_markdown(
        self, fetched: FetchedSource
    ) -> tuple[str, str, list[str], list[DocumentSection], list[str], list[str]]:
        content = fetched.content.strip()
        title = fetched.descriptor.title
        for line in content.splitlines():
            if line.startswith("#"):
                title = line.lstrip("# ").strip() or title
                break
        examples = re.findall(r"```[\w-]*\n(.*?)```", content, flags=re.DOTALL)
        sections = self._extract_sections_from_markdown(content, doc_type=fetched.descriptor.doc_type, fallback_title=title)
        api_items = self._extract_api_items_from_markdown(content, fetched.descriptor.doc_type)
        accessibility_notes = self._extract_accessibility_notes_from_sections(
            sections=sections,
            doc_type=fetched.descriptor.doc_type,
        )
        return title, content, unique_strings(examples[:8]), sections, api_items, accessibility_notes

    def _extract_from_html(
        self, fetched: FetchedSource
    ) -> tuple[str, str, list[str], list[DocumentSection], list[str], list[str]]:
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
        sections = self._extract_sections_from_html(main, doc_type=fetched.descriptor.doc_type, fallback_title=title)
        api_items = self._extract_api_items_from_html(main, fetched.descriptor.doc_type)
        accessibility_notes = self._extract_accessibility_notes_from_sections(
            sections=sections,
            doc_type=fetched.descriptor.doc_type,
        )
        return title, markdown, unique_strings(code_blocks[:8]), sections, api_items, accessibility_notes

    def _extract_sections_from_html(self, main: Tag, *, doc_type: str, fallback_title: str) -> list[DocumentSection]:
        headings = list(main.find_all(["h2", "h3"]))
        sections: list[DocumentSection] = []
        if not headings:
            text = html_to_markdown(str(main), heading_style="ATX", bullets="-").strip()
            if text:
                sections.append(
                    DocumentSection(
                        kind=self._classify_section_kind(fallback_title, doc_type),
                        title=fallback_title,
                        content=text,
                    )
                )
            return sections

        for heading in headings:
            title = heading.get_text(" ", strip=True)
            collected: list[str] = []
            for sibling in heading.next_siblings:
                if isinstance(sibling, Tag) and sibling.name in {"h2", "h3"}:
                    break
                if isinstance(sibling, Tag):
                    chunk = html_to_markdown(str(sibling), heading_style="ATX", bullets="-").strip()
                    if chunk:
                        collected.append(chunk)
            content = "\n\n".join(collected).strip()
            if content:
                sections.append(
                    DocumentSection(
                        kind=self._classify_section_kind(title, doc_type),
                        title=title,
                        content=content,
                    )
                )

        if not sections:
            body = html_to_markdown(str(main), heading_style="ATX", bullets="-").strip()
            if body:
                sections.append(
                    DocumentSection(
                        kind=self._classify_section_kind(fallback_title, doc_type),
                        title=fallback_title,
                        content=body,
                    )
                )
        return self._dedupe_sections(sections)

    def _extract_sections_from_markdown(
        self,
        content: str,
        *,
        doc_type: str,
        fallback_title: str,
    ) -> list[DocumentSection]:
        lines = content.splitlines()
        sections: list[DocumentSection] = []
        current_title = fallback_title
        buffer: list[str] = []

        def flush() -> None:
            text = "\n".join(buffer).strip()
            if not text:
                return
            sections.append(
                DocumentSection(
                    kind=self._classify_section_kind(current_title, doc_type),
                    title=current_title,
                    content=text,
                )
            )

        for line in lines:
            if line.startswith("## "):
                flush()
                current_title = line[3:].strip() or fallback_title
                buffer = []
                continue
            if line.startswith("### "):
                flush()
                current_title = line[4:].strip() or fallback_title
                buffer = []
                continue
            buffer.append(line)
        flush()
        return self._dedupe_sections(sections)

    def _extract_api_items_from_html(self, main: Tag, doc_type: str) -> list[str]:
        values: list[str] = []
        for row in main.select("table tr"):
            cells = [cell.get_text(" ", strip=True) for cell in row.select("th, td")]
            if cells:
                values.append(" | ".join(cells))
        for node in main.select("dt code, li code, p code"):
            text = node.get_text(" ", strip=True)
            if text and len(text) <= 80:
                values.append(text)
        if doc_type == "api" and not values:
            for item in main.select("li"):
                text = item.get_text(" ", strip=True)
                if text:
                    values.append(text)
        return unique_strings(values[:25])

    def _extract_api_items_from_markdown(self, content: str, doc_type: str) -> list[str]:
        values: list[str] = []
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("|") and stripped.endswith("|"):
                values.append(stripped.strip("| ").replace("|", " | "))
            for code_match in re.findall(r"`([^`]+)`", stripped):
                if 0 < len(code_match) <= 80:
                    values.append(code_match)
        if doc_type == "api" and not values:
            values.extend(line.strip("- ").strip() for line in content.splitlines() if line.strip().startswith("- "))
        return unique_strings(values[:25])

    def _extract_accessibility_notes_from_sections(
        self,
        *,
        sections: list[DocumentSection],
        doc_type: str,
    ) -> list[str]:
        notes: list[str] = []
        for section in sections:
            title = section.title.lower()
            kind = section.kind
            if kind == "accessibility" or "access" in title or "aria" in title or doc_type == "accessibility":
                for chunk in section.content.splitlines():
                    cleaned = chunk.strip("- ").strip()
                    if cleaned:
                        notes.append(cleaned)
        return unique_strings(notes[:20])

    def _classify_section_kind(self, title: str, doc_type: str) -> str:
        lowered = title.lower()
        if doc_type == "api" or any(token in lowered for token in ("api", "props", "prop", "slots", "css classes", "arguments")):
            return "api"
        if doc_type == "accessibility" or any(token in lowered for token in ("accessibility", "a11y", "aria", "screen reader")):
            return "accessibility"
        if any(token in lowered for token in ("example", "demo", "sample")):
            return "examples"
        if any(token in lowered for token in ("usage", "use", "guidance", "when to use")):
            return "usage"
        if any(token in lowered for token in ("reference", "details", "anatomy")):
            return "reference"
        return "summary"

    def _dedupe_sections(self, sections: list[DocumentSection]) -> list[DocumentSection]:
        seen: set[tuple[str, str]] = set()
        output: list[DocumentSection] = []
        for section in sections:
            key = (section.title, section.content)
            if key in seen:
                continue
            seen.add(key)
            output.append(section)
        return output

    def _doc_type_rank(self, doc_type: str) -> int:
        order = {"overview": 0, "api": 1, "accessibility": 2, "examples": 3}
        return order.get(doc_type, 100)
