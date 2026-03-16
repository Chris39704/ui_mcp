# Local npm wrapper

This wrapper is structured so it can be published later, but it is kept local for now with `"private": true`.

## Local use

Run it directly:

```bash
node ./bin/ui-knowledge-service-mcp.js
```

Or from this wrapper directory:

```bash
npm link
ui-knowledge-service-mcp
```

## Later publishing

When you are ready to publish:

1. Remove `"private": true`
2. Rename the package if needed
3. Publish to npm

When the wrapper no longer lives inside this repo, it will fall back to:

```bash
uvx --from ui-knowledge-service ui-knowledge-service-mcp
```
