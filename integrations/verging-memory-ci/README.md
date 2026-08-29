# Verging Memory CI adapter

A small HTTP adapter that lets [Verging Memory CI](https://verginglabs.com/memory-ci/integration)
exercise **this repository's** Basic Memory implementation. It is onboarding
integration code, not part of the product's public surface.

## What it is

`adapter.py` serves the Verging standardized-adapter contract on top of the
real Basic Memory FastAPI app:

| Route | Basic Memory operation |
|---|---|
| `GET /v1/health` | (public) liveness only |
| `POST /v1/namespaces` | create a project under the namespace root |
| `POST /v1/namespaces/{id}/memory/store` | create a note |
| `POST /v1/namespaces/{id}/memory/update` | replace that note in place |
| `POST /v1/namespaces/{id}/memory/recall` | search, then read the matching notes |
| `DELETE /v1/namespaces/{id}` | delete the project and its notes |

The adapter runs `basic_memory.api.app` inside its own process — it enters that
app's lifespan, so the container, database, migrations and watch coordinator are
the real ones — and reaches it through an in-process httpx ASGI client, the same
transport the MCP tools use. There is no mock store, no in-memory shim and no
canned answer anywhere in the module.

A Verging namespace is a real Basic Memory project: its own directory, entities
and search-index rows. Isolation between namespaces is the product's own project
boundary rather than a weaker one invented here.

## Configuration

| Variable | Meaning |
|---|---|
| `VERGING_PRODUCT_KEY` | Scoped bearer credential required on every `/v1` route except health. **Required** — the app refuses to start without it. |
| `VERGING_ADAPTER_DATA_DIR` | Root directory for namespace projects. Must be outside any repository checkout. |
| `BASIC_MEMORY_PROJECT_ROOT` | Set to the same path so Basic Memory also constrains every project under that root. |
| `BASIC_MEMORY_HOME` | Default project directory, inside the root. |
| `BASIC_MEMORY_CONFIG_DIR` | Basic Memory config + SQLite database location. |
| `BASIC_MEMORY_SEMANTIC_SEARCH_ENABLED` | Set to `false`: full-text search is Basic Memory's default retrieval mode and needs no model download. |
| `PORT` | Supplied by the platform. |

## Running the tests

The tests run the real implementation end to end against a throwaway state
directory outside the checkout:

```bash
uv run pytest integrations/verging-memory-ci/tests -q --no-cov
```

They are deliberately outside `testpaths`, so the repository's own suites are
unaffected.

## Deployment

`.railway/railway.py` declares the non-production Railway service: it builds
with the repository `Dockerfile` and starts `serve.py` instead of the image's
MCP-server `CMD`, with `/v1/health` as the healthcheck. Pushing this branch
deploys it.

Two notes for whoever picks this up next:

- The deprecated `railway.json` form is *not* applied by the current Railway
  version — a service configured that way silently keeps the Dockerfile `CMD`.
  Use the authoring file, and apply changes with
  `railway config plan` / `railway config apply` (needs `pip install railway-sdk`).
- `VERGING_PRODUCT_KEY` is declared as `preserve()` so the credential lives only
  in the Railway service's variables and never in this repository. Run
  `railway config plan` before applying: a variable that is neither declared nor
  preserved is deleted by an apply.
