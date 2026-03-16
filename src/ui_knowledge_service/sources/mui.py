"""MUI source catalog."""

from ui_knowledge_service.models import SourceDescriptor
from ui_knowledge_service.sources.base import BaseSourceAdapter


class MuiSourceAdapter(BaseSourceAdapter):
    library = "mui"

    def _build_catalog(self) -> list[SourceDescriptor]:
        return [
            SourceDescriptor(
                library=self.library,
                component="button",
                title="MUI Button",
                url="https://mui.com/material-ui/react-button/",
                source_kind="docs_page",
                aliases=("buttons",),
                freshness_days=7,
            ),
            SourceDescriptor(
                library=self.library,
                component="text-field",
                title="MUI Text Field",
                url="https://mui.com/material-ui/react-text-field/",
                source_kind="docs_page",
                aliases=("textfield", "input"),
                freshness_days=7,
            ),
            SourceDescriptor(
                library=self.library,
                component="dialog",
                title="MUI Dialog",
                url="https://mui.com/material-ui/react-dialog/",
                source_kind="docs_page",
                freshness_days=7,
            ),
            SourceDescriptor(
                library=self.library,
                component="table",
                title="MUI Table",
                url="https://mui.com/material-ui/react-table/",
                source_kind="docs_page",
                freshness_days=7,
            ),
        ]

