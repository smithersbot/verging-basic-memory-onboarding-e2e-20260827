# Verging Memory CI adapter

`adapter.py` serves the standardized Verging Memory CI wire shape for this
repository's Basic Memory implementation. It is a thin translation layer: every
operation is executed by Basic Memory's own v2 HTTP API, mounted in-process over
ASGI, so a namespace is a real Basic Memory project, a stored memory is a real
markdown note on disk, and recall is a real Basic Memory full-text search.

| Route | Behaviour |
|---|---|
| `GET /v1/health` | public liveness, `{"ok":true}` |
| `POST /v1/namespaces` | creates an isolated Basic Memory project, returns its stable UUID |
| `POST /v1/namespaces/{id}/memory/store` | writes a note, returns its stable UUID |
| `POST /v1/namespaces/{id}/memory/update` | replaces that note in place (same file, no duplicate) |
| `POST /v1/namespaces/{id}/memory/recall` | full-text search scoped to the namespace |
| `DELETE /v1/namespaces/{id}` | removes the project and its notes |

Every route except health requires `Authorization: Bearer $VERGING_PRODUCT_KEY`.

## Configuration

| Variable | Purpose |
|---|---|
| `VERGING_PRODUCT_KEY` | required bearer credential |
| `VERGING_DATA_DIR` | Basic Memory config, database and namespace directories; must be outside the checkout |
| `PORT` | listen port (default 8080) |

Namespace ids are Basic Memory project UUIDs and are validated as UUIDs before
use, and namespace directory names are derived by Basic Memory from a sanitized
project name under `BASIC_MEMORY_PROJECT_ROOT`, so caller-supplied strings never
reach the filesystem unsanitized.

## Checks

```bash
uv run python integrations/verging/check_adapter.py
```

Exercises authentication, namespace creation, store/recall, update without
duplication, deletion and cross-namespace isolation against the real
implementation in a throwaway data directory.

This is a non-production test deployment used only by Verging Memory CI.
