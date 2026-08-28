# Verging Memory CI adapter

A small HTTP adapter that lets [Verging Memory CI](https://verginglabs.com/memory-ci/integration)
exercise **this repository's Basic Memory** as the memory product under test.

It is a thin translation layer, not a second implementation. Each route is a
call into Basic Memory's own v2 API over the in-process ASGI transport — the
same interface the MCP tools use:

| Adapter route | Basic Memory operation |
|---|---|
| `POST /v1/namespaces` | create a project (`POST /v2/projects/`) |
| `POST /v1/namespaces/{id}/memory/store` | create a note (`POST .../knowledge/entities`) |
| `POST /v1/namespaces/{id}/memory/update` | replace that note (`PUT .../knowledge/entities/{id}`) |
| `POST /v1/namespaces/{id}/memory/recall` | search (`POST .../search/`) then read each hit |
| `DELETE /v1/namespaces/{id}` | delete the project and its notes |
| `GET /v1/health` | none — public liveness probe |

A namespace is a real Basic Memory project; a memory is a real markdown file on
disk with frontmatter, observations and relations intact.

## Isolation and untrusted input

- Namespace ids are Basic Memory external UUIDs and are validated as UUIDs
  before use, so nothing caller-supplied reaches a path.
- Project names are generated with a `vmci-` prefix. An id that resolves to any
  other project is reported as an unknown namespace, so a caller cannot reach a
  project the adapter did not create.
- `BASIC_MEMORY_PROJECT_ROOT` constrains every project directory to the data
  root, which is what makes a traversal escape structurally impossible rather
  than merely filtered.
- Deleting an unknown namespace succeeds (`204`): reset is idempotent.

## Configuration

| Variable | Meaning |
|---|---|
| `VERGING_PRODUCT_KEY` | **Required.** Scoped bearer credential required on every route except `/v1/health`. The adapter refuses to start without it. |
| `VERGING_ADAPTER_DATA_ROOT` | Where Basic Memory state lives. Defaults to `<tempdir>/verging-memory-ci`. Must stay outside the checkout so report commits cannot capture product data. |
| `PORT` | Listen port. Defaults to `8000`. |

The adapter sets `BASIC_MEMORY_CONFIG_DIR`, `BASIC_MEMORY_HOME` and
`BASIC_MEMORY_PROJECT_ROOT` itself, deliberately overriding the container
image's `/app`-relative values.

## Run and test

```bash
VERGING_PRODUCT_KEY=local-dev uv run python -m verging_memory_ci_adapter   # PYTHONPATH=integrations/verging-memory-ci
uv run pytest integrations/verging-memory-ci/tests --no-cov
```

The tests run against a real Basic Memory instance — no mocks — and cover
authentication, namespace lifecycle, store/recall, update-without-duplication,
deletion, cross-namespace isolation and untrusted identifiers.

## Deployment

The non-production test endpoint builds from the repository's root `Dockerfile`,
which Railway detects on its own. Two service settings make that image serve the
adapter instead of the image's default MCP server:

- **Start command:** `PYTHONPATH=/app/integrations/verging-memory-ci python -m verging_memory_ci_adapter`
- **Health check path:** `/v1/health`

These live in the service's settings rather than a committed `railway.json`:
Railway has deprecated config-as-code, and its replacement (`.railway/railway.ts`)
requires the Railway TypeScript SDK, which does not belong in this Python
repository.
