"""USWDS source catalog."""

from ui_knowledge_service.models import SourceDescriptor
from ui_knowledge_service.sources.base import BaseSourceAdapter


class UswdsSourceAdapter(BaseSourceAdapter):
    library = "uswds"

    def _build_catalog(self) -> list[SourceDescriptor]:
        return [
            SourceDescriptor(
                library=self.library,
                component="button",
                title="USWDS Button",
                url="https://designsystem.digital.gov/components/button/",
                source_kind="docs_page",
                freshness_days=14,
            ),
            SourceDescriptor(
                library=self.library,
                component="form",
                title="USWDS Form Controls",
                url="https://designsystem.digital.gov/components/form/",
                source_kind="docs_page",
                aliases=("forms",),
                freshness_days=14,
            ),
            SourceDescriptor(
                library=self.library,
                component="table",
                title="USWDS Table",
                url="https://designsystem.digital.gov/components/table/",
                source_kind="docs_page",
                freshness_days=14,
            ),
            SourceDescriptor(
                library=self.library,
                component="header",
                title="USWDS Header",
                url="https://designsystem.digital.gov/components/header/",
                source_kind="docs_page",
                freshness_days=14,
            ),
        ]
