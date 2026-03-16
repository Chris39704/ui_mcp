"""Rebuildable local vector-style fallback index."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass

from ui_knowledge_service.config import Settings
from ui_knowledge_service.models import ComponentDocument, FreshnessState, SearchHit
from ui_knowledge_service.utils import chunk_text, make_snippet, tokenize


@dataclass(slots=True)
class IndexedChunk:
    chunk_id: str
    document_id: str
    library: str
    component: str
    doc_type: str
    title: str
    source_url: str
    text: str
    vector: dict[int, float]
    freshness_state: str


class VectorIndex:
    """Tiny hashed vector fallback that stays rebuildable from local artifacts."""

    def __init__(self, settings: Settings, *, dimensions: int = 256):
        self.settings = settings
        self.dimensions = dimensions
        self._chunks: list[IndexedChunk] = []
        self.load()

    def load(self) -> None:
        path = self.settings.vector_index_path
        if not path.exists():
            self._chunks = []
            return
        payload = json.loads(path.read_text(encoding="utf-8"))
        self._chunks = [IndexedChunk(**chunk) for chunk in payload.get("chunks", [])]

    def rebuild(self, documents: list[ComponentDocument]) -> None:
        self._chunks = []
        for document in documents:
            self.upsert_document(document, persist=False)
        self._persist()

    def upsert_document(self, document: ComponentDocument, *, persist: bool = True) -> None:
        self._chunks = [chunk for chunk in self._chunks if chunk.document_id != document.document_id]
        chunks = chunk_text(document.searchable_text() or document.title)
        if not chunks:
            chunks = [document.title]
        freshness = document.freshness_state().value
        for index, chunk in enumerate(chunks):
            self._chunks.append(
                IndexedChunk(
                    chunk_id=f"{document.document_id}#{index}",
                    document_id=document.document_id,
                    library=document.library,
                    component=document.component,
                    doc_type=document.doc_type,
                    title=document.title,
                    source_url=document.source_url,
                    text=chunk,
                    vector=self._vectorize(chunk),
                    freshness_state=freshness,
                )
            )
        if persist:
            self._persist()

    def search(self, query: str, *, library: str | None = None, limit: int = 6) -> list[SearchHit]:
        query_vector = self._vectorize(query)
        if not query_vector:
            return []
        ranked: list[tuple[float, IndexedChunk]] = []
        for chunk in self._chunks:
            if library and chunk.library != library:
                continue
            score = self._cosine_similarity(query_vector, chunk.vector)
            if score <= 0:
                continue
            ranked.append((score, chunk))
        ranked.sort(key=lambda item: item[0], reverse=True)

        deduped: list[SearchHit] = []
        seen: set[str] = set()
        for score, chunk in ranked:
            if chunk.document_id in seen:
                continue
            seen.add(chunk.document_id)
            deduped.append(
                SearchHit(
                    document_id=chunk.document_id,
                    library=chunk.library,
                    component=chunk.component,
                    doc_type=chunk.doc_type,
                    title=chunk.title,
                    source_url=chunk.source_url,
                    score=score,
                    snippet=make_snippet(chunk.text, query),
                    matched_by="vector",
                    freshness_state=FreshnessState(chunk.freshness_state),
                )
            )
            if len(deduped) >= limit:
                break
        return deduped

    def _persist(self) -> None:
        payload = {
            "dimensions": self.dimensions,
            "chunks": [asdict(chunk) for chunk in self._chunks],
        }
        self.settings.vector_index_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _vectorize(self, text: str) -> dict[int, float]:
        vector: dict[int, float] = {}
        for token in tokenize(text):
            bucket = hash(token) % self.dimensions
            vector[bucket] = vector.get(bucket, 0.0) + 1.0
        norm = math.sqrt(sum(value * value for value in vector.values()))
        if norm == 0:
            return {}
        return {key: value / norm for key, value in vector.items()}

    def _cosine_similarity(self, left: dict[int, float], right: dict[int, float]) -> float:
        if not left or not right:
            return 0.0
        if len(left) > len(right):
            left, right = right, left
        return sum(value * right.get(key, 0.0) for key, value in left.items())
