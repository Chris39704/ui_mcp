"""USWDS source catalog."""

from ui_knowledge_service.sources.base import BaseSourceAdapter


class UswdsSourceAdapter(BaseSourceAdapter):
    library = "uswds"

    def _build_catalog(self):
        return self._load_catalog("uswds.json")
