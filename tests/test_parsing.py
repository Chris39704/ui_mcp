from __future__ import annotations

from pathlib import Path

from ui_knowledge_service.config import Settings
from ui_knowledge_service.models import FetchedSource, SourceDescriptor
from ui_knowledge_service.sources.base import BaseSourceAdapter


class FixtureAdapter(BaseSourceAdapter):
    library = "fixture"

    def _build_catalog(self) -> list[SourceDescriptor]:
        return []


def test_html_api_fixture_extracts_structured_fields(tmp_path):
    adapter = FixtureAdapter(Settings(data_dir=tmp_path / "data"))
    html = Path("tests/fixtures/mui_button_api.html").read_text(encoding="utf-8")
    fetched = FetchedSource(
        descriptor=SourceDescriptor(
            library="mui",
            component="button",
            doc_type="api",
            title="MUI Button API",
            url="https://mui.com/material-ui/api/button/",
            source_kind="api_reference",
        ),
        content=html,
        content_type="text/html",
    )

    document = adapter.normalize(fetched)

    assert document.doc_type == "api"
    assert document.code_examples
    assert any(section.kind == "api" and "Props" in section.title for section in document.sections)
    assert any("variant" in item for item in document.api_items)
    assert any("accessible name" in note.lower() for note in document.accessibility_notes)


def test_html_accessibility_fixture_extracts_accessibility_notes(tmp_path):
    adapter = FixtureAdapter(Settings(data_dir=tmp_path / "data"))
    html = Path("tests/fixtures/uswds_form_accessibility.html").read_text(encoding="utf-8")
    fetched = FetchedSource(
        descriptor=SourceDescriptor(
            library="uswds",
            component="form",
            doc_type="accessibility",
            title="USWDS Form Accessibility",
            url="https://designsystem.digital.gov/components/form/",
            source_kind="accessibility_reference",
        ),
        content=html,
        content_type="text/html",
    )

    document = adapter.normalize(fetched)

    assert any(section.kind == "accessibility" for section in document.sections)
    assert any("Associate labels" in note for note in document.accessibility_notes)
    assert document.code_examples
