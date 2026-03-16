"""SQLite-backed artifact and metadata store."""

from __future__ import annotations

import json
import sqlite3
from difflib import get_close_matches

from ui_knowledge_service.config import Settings
from ui_knowledge_service.models import ComponentDocument, SearchHit
from ui_knowledge_service.utils import infer_extension, make_snippet, slugify, utcnow


class DocumentStore:
    """Persist normalized documents and a lightweight searchable catalog."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.settings.ensure_dirs()
        self._conn = sqlite3.connect(self.settings.database_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._initialize()

    def close(self) -> None:
        self._conn.close()

    def _initialize(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                library TEXT NOT NULL,
                component TEXT NOT NULL,
                doc_type TEXT NOT NULL,
                title TEXT NOT NULL,
                content_md TEXT NOT NULL,
                code_examples_json TEXT NOT NULL,
                source_url TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                version TEXT,
                etag TEXT,
                last_modified TEXT,
                checksum TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                stale_after TEXT NOT NULL,
                citations_json TEXT NOT NULL,
                raw_path TEXT,
                normalized_path TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_documents_library_component
                ON documents (library, component, doc_type);

            CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                doc_id UNINDEXED,
                library,
                component,
                doc_type,
                title,
                content_md,
                tokenize = 'porter unicode61'
            );
            """
        )
        self._conn.commit()

    def save_raw_snapshot(self, *, url: str, content_type: str | None, content: str, document_id: str) -> str:
        extension = infer_extension(url, content_type)
        filename = f"{document_id.replace(':', '__')}__{utcnow().strftime('%Y%m%dT%H%M%SZ')}{extension}"
        path = self.settings.raw_dir / filename
        path.write_text(content, encoding="utf-8")
        return str(path)

    def save_document(self, document: ComponentDocument) -> ComponentDocument:
        normalized_path = self.settings.normalized_dir / f"{document.document_id.replace(':', '__')}.json"
        updated = document.model_copy(update={"normalized_path": str(normalized_path)})
        normalized_path.write_text(updated.model_dump_json(indent=2), encoding="utf-8")

        self._conn.execute(
            """
            INSERT INTO documents (
                id, library, component, doc_type, title, content_md, code_examples_json,
                source_url, source_kind, version, etag, last_modified, checksum,
                fetched_at, stale_after, citations_json, raw_path, normalized_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                library = excluded.library,
                component = excluded.component,
                doc_type = excluded.doc_type,
                title = excluded.title,
                content_md = excluded.content_md,
                code_examples_json = excluded.code_examples_json,
                source_url = excluded.source_url,
                source_kind = excluded.source_kind,
                version = excluded.version,
                etag = excluded.etag,
                last_modified = excluded.last_modified,
                checksum = excluded.checksum,
                fetched_at = excluded.fetched_at,
                stale_after = excluded.stale_after,
                citations_json = excluded.citations_json,
                raw_path = excluded.raw_path,
                normalized_path = excluded.normalized_path
            """,
            (
                updated.document_id,
                updated.library,
                slugify(updated.component),
                updated.doc_type,
                updated.title,
                updated.content_md,
                json.dumps(updated.code_examples),
                updated.source_url,
                updated.source_kind,
                updated.version,
                updated.etag,
                updated.last_modified,
                updated.checksum,
                updated.fetched_at.isoformat(),
                updated.stale_after.isoformat(),
                json.dumps([citation.model_dump(mode="json") for citation in updated.citations]),
                updated.raw_path,
                updated.normalized_path,
            ),
        )
        self._conn.execute("DELETE FROM documents_fts WHERE doc_id = ?", (updated.document_id,))
        self._conn.execute(
            """
            INSERT INTO documents_fts(doc_id, library, component, doc_type, title, content_md)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                updated.document_id,
                updated.library,
                slugify(updated.component),
                updated.doc_type,
                updated.title,
                updated.content_md,
            ),
        )
        self._conn.commit()
        return updated

    def get_document(self, library: str, component: str, doc_type: str | None = None) -> ComponentDocument | None:
        component_slug = slugify(component)
        if doc_type:
            row = self._conn.execute(
                """
                SELECT * FROM documents
                WHERE library = ? AND component = ? AND doc_type = ?
                LIMIT 1
                """,
                (library, component_slug, doc_type),
            ).fetchone()
        else:
            row = self._conn.execute(
                """
                SELECT * FROM documents
                WHERE library = ? AND component = ?
                ORDER BY CASE WHEN doc_type = 'overview' THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (library, component_slug),
            ).fetchone()
        if not row:
            return None
        return self._row_to_document(row)

    def list_documents(self) -> list[ComponentDocument]:
        rows = self._conn.execute("SELECT * FROM documents ORDER BY library, component, doc_type").fetchall()
        return [self._row_to_document(row) for row in rows]

    def count_documents(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS count FROM documents").fetchone()
        return int(row["count"]) if row else 0

    def suggest_components(self, library: str | None, query: str, *, limit: int = 5) -> list[str]:
        like_value = f"%{slugify(query)}%"
        if library:
            rows = self._conn.execute(
                """
                SELECT DISTINCT component FROM documents
                WHERE library = ? AND component LIKE ?
                ORDER BY component
                LIMIT ?
                """,
                (library, like_value, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT DISTINCT component FROM documents
                WHERE component LIKE ?
                ORDER BY component
                LIMIT ?
                """,
                (like_value, limit),
            ).fetchall()
        matches = [str(row["component"]) for row in rows]
        if matches:
            return matches
        if library:
            component_rows = self._conn.execute(
                "SELECT DISTINCT component FROM documents WHERE library = ? ORDER BY component",
                (library,),
            ).fetchall()
        else:
            component_rows = self._conn.execute("SELECT DISTINCT component FROM documents ORDER BY component").fetchall()
        candidates = [str(row["component"]) for row in component_rows]
        return get_close_matches(slugify(query), candidates, n=limit, cutoff=0.1)

    def search_fts(self, query: str, *, library: str | None = None, limit: int = 6) -> list[SearchHit]:
        fts_query = self._build_fts_query(query)
        if not fts_query:
            return []
        params: tuple[object, ...]
        sql = """
            SELECT documents.*, bm25(documents_fts) AS rank
            FROM documents_fts
            JOIN documents ON documents.id = documents_fts.doc_id
            WHERE documents_fts MATCH ?
        """
        params = (fts_query,)
        if library:
            sql += " AND documents.library = ?"
            params = (fts_query, library)
        sql += " ORDER BY rank LIMIT ?"
        params += (limit,)
        rows = self._conn.execute(sql, params).fetchall()
        hits: list[SearchHit] = []
        for row in rows:
            document = self._row_to_document(row)
            hits.append(
                SearchHit(
                    document_id=document.document_id,
                    library=document.library,
                    component=document.component,
                    doc_type=document.doc_type,
                    title=document.title,
                    source_url=document.source_url,
                    score=float(-row["rank"]),
                    snippet=make_snippet(document.content_md, query),
                    matched_by="fts",
                    freshness_state=document.freshness_state(),
                )
            )
        return hits

    def _build_fts_query(self, query: str) -> str:
        parts = [f"{token}*" for token in query.lower().split() if token.strip()]
        return " OR ".join(parts)

    def _row_to_document(self, row: sqlite3.Row) -> ComponentDocument:
        return ComponentDocument.model_validate(
            {
                "library": row["library"],
                "component": row["component"],
                "doc_type": row["doc_type"],
                "title": row["title"],
                "content_md": row["content_md"],
                "code_examples": json.loads(row["code_examples_json"]),
                "source_url": row["source_url"],
                "source_kind": row["source_kind"],
                "version": row["version"],
                "etag": row["etag"],
                "last_modified": row["last_modified"],
                "checksum": row["checksum"],
                "fetched_at": row["fetched_at"],
                "stale_after": row["stale_after"],
                "citations": json.loads(row["citations_json"]),
                "raw_path": row["raw_path"],
                "normalized_path": row["normalized_path"],
            }
        )
