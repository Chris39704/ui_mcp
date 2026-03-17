# UI Knowledge Service

Offline-first UI documentation service for MUI, USWDS, and Angular Material.

## What it provides

- Local artifact cache on disk plus a SQLite catalog with FTS search
- Structured component retrieval first, vector-style fallback second
- Catalog-driven official sources with multiple document types per component
- Structured parsing for sections, API items, and accessibility notes
- Stale-while-revalidate refresh behavior
- Local FastAPI admin API
- Mounted MCP tools for assistants

## Development

```bash
uv sync --extra dev
uv run ui-knowledge-service serve --host 127.0.0.1 --port 8000
```

## MCP over stdio

Run the MCP server directly over stdio:

```bash
uv run --no-sync ui-knowledge-service stdio
```

Or use the dedicated stdio entrypoint:

```bash
uv run --no-sync ui-knowledge-service-mcp
```

Example MCP client config:

```json
{
  "command": "uv",
  "args": ["run", "--directory", "/Users/chrisfarabaugh/Documents/workspace/ai_ui_skills", "--no-sync", "ui-knowledge-service-mcp"],
  "env": {
    "UIKS_DATA_DIR": "/Users/chrisfarabaugh/Documents/workspace/ai_ui_skills/.data/ui_knowledge_service"
  }
}
```

## Prewarm the starter cache

```bash
uv run ui-knowledge-service prewarm
```

## Audit the source catalog

Fetch, normalize, and validate the configured official sources:

```bash
uv run ui-knowledge-service audit-catalog --library mui --snapshot-dir ./tmp/catalog-audit
```

Compare against the stored baseline and fail if drift is detected:

```bash
uv run ui-knowledge-service audit-catalog --library mui --compare-to-baseline --fail-on-drift
```

Generate a severity-ranked maintenance report and write a Markdown summary:

```bash
uv run ui-knowledge-service audit-catalog --library mui --compare-to-baseline --markdown-report ./tmp/catalog-maintenance.md
```

Fail CI only when the maintenance report contains warnings or errors:

```bash
uv run ui-knowledge-service audit-catalog --library mui --compare-to-baseline --fail-on-severity warn
```

Safely promote the current audit to the baseline after persisting JSON and Markdown report artifacts:

```bash
uv run ui-knowledge-service promote-baseline --library mui --max-allowed-severity warn
```

Force a promotion when you already reviewed the blocking recommendations:

```bash
uv run ui-knowledge-service promote-baseline --library mui --force
```

Write the current audit as the new baseline:

```bash
uv run ui-knowledge-service audit-catalog --library mui --write-baseline
```

The service stores its working data under `.data/ui_knowledge_service` by default.

## Resolve a query into guidance

Use the high-level resolver when a client wants an actionable answer instead of raw documents:

```bash
curl "http://127.0.0.1:8000/resolve?query=button%20props%20variant&library=mui&component_hint=button"
```

## HTTP endpoints

- `GET /health`
- `GET /sources`
- `GET /catalog/audit`
- `GET /catalog/audit/diff`
- `GET /catalog/audit/report`
- `POST /catalog/audit/promote`
- `GET /search?query=button&library=mui`
- `GET /resolve?query=button%20props%20variant&library=mui&component_hint=button`
- `GET /documents/{library}/{component}`
- `GET /bundles/{library}/{component}`
- `GET /status/{library}/{component}`
- `GET /refresh/status`
- `POST /catalog/audit/baseline`
- `POST /refresh`

## MCP tools

- `get_component_doc`
- `get_component_bundle`
- `search_component_docs`
- `resolve_component_query`
- `audit_catalog`
- `compare_catalog_to_baseline`
- `get_catalog_maintenance_report`
- `promote_catalog_baseline`
- `get_component_examples`
- `get_component_status`

## Local npm wrapper

A local, publish-ready npm wrapper lives in [wrappers/npm](/Users/chrisfarabaugh/Documents/workspace/ai_ui_skills/wrappers/npm). For now it defaults to running the checked-out repo locally via `uv run --directory ... ui-knowledge-service-mcp`. When moved out of the repo for publishing, it falls back to `uvx --from ui-knowledge-service ui-knowledge-service-mcp`.
