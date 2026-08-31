# Verging Memory CI adapter

A small HTTP adapter that serves the standardized [Verging Memory CI](https://verginglabs.com/memory-ci/integration)
product contract from a real Basic Memory runtime.

Verging tests a product's *memory*, so nothing here is simulated:

| Verging concept | Basic Memory reality |
|---|---|
| namespace | a real Basic Memory project, with its own directory and index |
| stored memory | a real markdown note written through the v2 knowledge API |
| recall | a real Basic Memory search scoped to that project |
| namespace deletion | project deletion, including the notes on disk |

Requests go through `basic_memory.mcp.async_client.get_client()` and the typed
clients in `basic_memory.mcp.clients` — the same in-process API path the MCP
tools use.

## Routes

Everything except `/v1/health` requires `Authorization: Bearer $VERGING_PRODUCT_KEY`.

```text
GET    /v1/health                          -> {"ok": true}
POST   /v1/namespaces                      -> {"id": "<namespace id>"}
POST   /v1/namespaces/{id}/memory/store    -> {"ok": true, "id": "<memory id>"}
POST   /v1/namespaces/{id}/memory/update   -> {"ok": true, "id": "<memory id>"}
POST   /v1/namespaces/{id}/memory/recall   -> {"ok": true, "results": [...]}
DELETE /v1/namespaces/{id}                 -> {"ok": true}
```

A missing or wrong credential is `401`, malformed input is `422` (or `400` for a
namespace name that cannot become a directory), and an unknown namespace or
memory id is `404`. A failed Basic Memory call is `502`.

## Isolation and safety

- Namespace ids are Basic Memory project external ids and must parse as UUIDs
  before a request can reach the API, so a traversal-shaped id resolves to
  nothing.
- The adapter only addresses projects whose name starts with `verging-ns-`, so it
  can never read, write or delete a project it did not create — including the
  default project.
- `BASIC_MEMORY_PROJECT_ROOT` is pinned at import time, which makes Basic Memory
  resolve every project directory as `project_root/<permalink(name)>` and ignore
  the requested path. A hostile namespace name cannot escape the data root.
- All state (notes, config, index database) lives under `VERGING_ADAPTER_DATA_DIR`
  — outside the git checkout, so a Verging report commit can never capture it.
- Each stored memory gets its own directory, so two memories sharing a title
  cannot collide on one file. `update` replaces a note in place, keeping its id
  and file path, so it never leaves a duplicate behind.

## Configuration

| Variable | Purpose |
|---|---|
| `VERGING_PRODUCT_KEY` | required; the scoped bearer credential callers must present |
| `VERGING_ADAPTER_DATA_DIR` | optional; where Basic Memory state lives (default: a `verging-memory-ci` directory under the system temp dir) |
| `PORT` | optional; listen port (default `8000`) |

The adapter fails to start without `VERGING_PRODUCT_KEY` rather than serving
unauthenticated traffic.

## Running

```bash
VERGING_PRODUCT_KEY=... uv run python -m integrations.verging.app
```

## Tests

Contract tests run against the real Basic Memory runtime — real projects, real
markdown files, real search, no mocks:

```bash
cd integrations/verging && uv run pytest
```

## Deployment

The repository root `railway.json` declares how the non-production Railway test
service runs this adapter: build the repository `Dockerfile`, start
`integrations.verging.app:app`, and use `/v1/health` as the healthcheck. That
service exists only to host this test deployment for Verging Memory CI.

The same start command and healthcheck are also set on the Railway service
itself, and those settings are what actually take effect: a `railway up` deploy
builds the root `Dockerfile` but keeps the image's own `CMD` (Basic Memory's MCP
server) unless the service overrides it, and Railway's replacement for
config-as-code needs a `railway` SDK package this repository does not carry. The
two are kept identical — treat `railway.json` as the declaration and the service
settings as the mechanism.
