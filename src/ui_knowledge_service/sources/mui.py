"""MUI source catalog."""

from ui_knowledge_service.sources.base import BaseSourceAdapter


class MuiSourceAdapter(BaseSourceAdapter):
    library = "mui"

    def _build_catalog(self):
        return self._load_catalog("mui.json")
