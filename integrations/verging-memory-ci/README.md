# Verging Memory CI adapter

`adapter.py` serves the Verging Memory CI product wire (`/v1/...`) on top of the
real Basic Memory implementation in this repository. Every request becomes a
real Basic Memory project, note and search-index query — the FastAPI app from
`basic_memory.api.app` is run in-process and called over ASGI, the same way the
MCP tools call it. There is no mock or in-memory substitute.

## Wire contract

| Route | Behavior |
| --- | --- |
| `GET /v1/health` | Public liveness probe: `{"ok": true}` |
| `POST /v1/namespaces` | `{name, forceCreate}` → `{id}`; creates an isolated Basic Memory project |
| `POST /v1/namespaces/{id}/memory/store` | `{content, title?, metadata?}` → `{ok, id}`; writes a real markdown note |
| `POST /v1/namespaces/{id}/memory/update` | `{id, content, metadata?}` → `{ok, id}`; replaces that note in place |
| `POST /v1/namespaces/{id}/memory/recall` | `{query, limit?}` → `{ok, results:[{id, content, metadata}]}` |
| `DELETE /v1/namespaces/{id}` | `204`; idempotent, removes the project and its files |

Every route except `/v1/health` requires `Authorization: Bearer $VERGING_PRODUCT_KEY`
and answers `401` otherwise. Malformed bodies get `422`; unknown namespaces and
notes get `404`.

## Mapping and safety

- namespace → Basic Memory project (`external_id` UUID is the namespace id)
- memory → note/entity (`external_id` UUID is the memory id)
- recall → project-scoped full-text search, so namespaces cannot see each other

Namespace names are validated against a strict allowlist and namespace/memory
ids must parse as UUIDs before they reach an API path. `BASIC_MEMORY_PROJECT_ROOT`
is set to the adapter's own data directory, so a project directory is always
`<data>/projects/<sanitized-name>` and traversal is structurally impossible.

Recall queries are reduced to content words and OR-ed together before they hit
the lexical index; a raw natural-language question matches nothing verbatim.

## Configuration

| Variable | Purpose |
| --- | --- |
| `VERGING_PRODUCT_KEY` | Required. The scoped bearer credential the adapter accepts. |
| `VERGING_ADAPTER_DATA_DIR` | Optional. Adapter state root; defaults to `~/.verging-memory-ci`. |
| `PORT` | Optional. Listen port; defaults to `8080`. Railway injects this. |

The data directory is deliberately outside the repository checkout so a Memory
CI report commit can never capture namespace content.

## Deployment

The adapter runs on the repository's non-production Railway service, built from
the existing root `Dockerfile`. Railway's file-based config-as-code
(`railway.json` / `railway.toml`) is deprecated and rejected by the API, and the
replacement (`.railway/railway.ts`) can only be applied from a linked project,
so the service instance carries the two settings that differ from the image
default:

| Setting | Value |
| --- | --- |
| `dockerfilePath` | `Dockerfile` |
| `startCommand` | `python /app/integrations/verging-memory-ci/adapter.py` |
| `healthcheckPath` | `/v1/health` |

`VERGING_PRODUCT_KEY` is stored as a service variable. The image's default
command still starts the Basic Memory MCP server, so nothing about the product
container changes; only this service overrides the entrypoint.

## Tests

```
uv run pytest integrations/verging-memory-ci/test_adapter.py --no-cov -q
```

The suite covers authentication, namespace creation and reset, store/recall,
update-without-duplication, deletion (including idempotency and on-disk
removal), cross-namespace isolation, and rejection of untrusted identifiers and
malformed payloads — all against the real implementation.
