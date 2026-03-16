"""Configuration for the service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Settings:
    data_dir: Path
    database_filename: str = "catalog.sqlite3"
    vector_index_filename: str = "vector_index.json"
    user_agent: str = "ui-knowledge-service/0.1"
    prewarm_on_start: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        base_dir = Path(os.environ.get("UIKS_DATA_DIR", Path.cwd() / ".data" / "ui_knowledge_service"))
        prewarm = os.environ.get("UIKS_PREWARM_ON_START", "0").lower() in {"1", "true", "yes"}
        user_agent = os.environ.get("UIKS_USER_AGENT", "ui-knowledge-service/0.1")
        return cls(data_dir=base_dir, user_agent=user_agent, prewarm_on_start=prewarm)

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def normalized_dir(self) -> Path:
        return self.data_dir / "normalized"

    @property
    def index_dir(self) -> Path:
        return self.data_dir / "index"

    @property
    def database_path(self) -> Path:
        return self.data_dir / self.database_filename

    @property
    def vector_index_path(self) -> Path:
        return self.index_dir / self.vector_index_filename

    def ensure_dirs(self) -> None:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.normalized_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)

