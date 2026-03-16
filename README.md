# UI Knowledge Service

Offline-first UI documentation service for MUI, USWDS, and Angular Material.

## What it provides

- Local artifact cache on disk plus a SQLite catalog with FTS search
- Structured component retrieval first, vector-style fallback second
- Stale-while-revalidate refresh behavior
- Local FastAPI admin API
- Mounted MCP tools for assistants

## Development

```bash
uv sync --extra dev
uv run ui-knowledge-service serve --host 127.0.0.1 --port 8000
```

## Prewarm the starter cache

```bash
uv run ui-knowledge-service prewarm
```

The service stores its working data under `.data/ui_knowledge_service` by default.

