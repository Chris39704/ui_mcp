"""Angular Material source catalog."""

from ui_knowledge_service.sources.base import BaseSourceAdapter


class AngularMaterialSourceAdapter(BaseSourceAdapter):
    library = "angular-material"

    def _build_catalog(self):
        return self._load_catalog("angular_material.json")
