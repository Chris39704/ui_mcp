"""Angular Material source catalog."""

from ui_knowledge_service.models import SourceDescriptor
from ui_knowledge_service.sources.base import BaseSourceAdapter


class AngularMaterialSourceAdapter(BaseSourceAdapter):
    library = "angular-material"

    def _build_catalog(self) -> list[SourceDescriptor]:
        return [
            SourceDescriptor(
                library=self.library,
                component="button",
                title="Angular Material Button",
                url="https://material.angular.dev/components/button/overview",
                source_kind="docs_page",
                freshness_days=7,
            ),
            SourceDescriptor(
                library=self.library,
                component="input",
                title="Angular Material Input",
                url="https://material.angular.dev/components/input/overview",
                source_kind="docs_page",
                aliases=("text-field", "textfield"),
                freshness_days=7,
            ),
            SourceDescriptor(
                library=self.library,
                component="dialog",
                title="Angular Material Dialog",
                url="https://material.angular.dev/components/dialog/overview",
                source_kind="docs_page",
                freshness_days=7,
            ),
            SourceDescriptor(
                library=self.library,
                component="table",
                title="Angular Material Table",
                url="https://material.angular.dev/components/table/overview",
                source_kind="docs_page",
                freshness_days=7,
            ),
        ]

