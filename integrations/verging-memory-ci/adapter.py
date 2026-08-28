"""Verging Memory CI product adapter for Basic Memory.

Verging Memory CI drives a memory product through a small, uniform HTTP wire:
create an isolated namespace, store notes into it, recall them, update one in
place, then delete the namespace. This module serves that wire on top of the
real Basic Memory implementation in this repository — the FastAPI app in
``basic_memory.api.app``, called in-process over ASGI exactly the way the MCP
tools call it. There is no mock, in-memory store or canned answer anywhere in
this file; every route below turns into a real project, a real markdown note on
disk, and a real search-index query.

Mapping to Basic Memory concepts:

- namespace  -> project (its ``external_id`` UUID is the namespace id)
- memory     -> note/entity (its ``external_id`` UUID is the memory id)
- recall     -> project-scoped full-text search over the note index

Deployment notes live next to this file in README.md.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

# --- Runtime configuration ---
# Basic Memory reads its configuration through pydantic-settings at import time,
# so every environment default has to be in place before the first
# ``basic_memory`` import below. Nothing here is a Basic Memory behavior change:
# these are the same env vars an operator would set for a scratch deployment.


def _configure_basic_memory_environment() -> Path:
    """Point Basic Memory at a data directory outside the repository checkout.

    Why: report commits land inside the checkout. Keeping adapter state (config,
    SQLite index and every namespace's markdown) outside it means a Memory CI
    report commit can never capture test data, and the deployment stays free of
    accidental repository writes.

    Outcome: config + database under ``<data>/config``, one directory per
    namespace under ``<data>/projects``.
    """
    data_dir = Path(
        os.environ.get("VERGING_ADAPTER_DATA_DIR") or Path.home() / ".verging-memory-ci"
    ).resolve()
    projects_dir = data_dir / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)

    # Assigned, not defaulted: the repository's container image points these at
    # /app (the checkout) for the product server, and the adapter must not
    # inherit that. VERGING_ADAPTER_DATA_DIR is the one knob for relocation.
    os.environ["BASIC_MEMORY_CONFIG_DIR"] = str(data_dir / "config")
    # project_root is Basic Memory's own containment guard: with it set, a
    # project's directory is always <root>/<sanitized-name>, so a hostile
    # namespace name cannot escape the data directory.
    os.environ["BASIC_MEMORY_PROJECT_ROOT"] = str(projects_dir)
    os.environ["BASIC_MEMORY_HOME"] = str(projects_dir / "main")
    # Semantic retrieval would download an embedding model on first write. The
    # adapter only needs the deterministic full-text index, so keep it off.
    os.environ.setdefault("BASIC_MEMORY_SEMANTIC_SEARCH_ENABLED", "false")
    # No one edits these files by hand; a filesystem watcher would only race the
    # adapter's own writes.
    os.environ.setdefault("BASIC_MEMORY_SYNC_CHANGES", "false")
    return data_dir


DATA_DIR = _configure_basic_memory_environment()

import frontmatter  # noqa: E402
from fastapi import Depends, FastAPI, Header, HTTPException, Path as PathParam  # noqa: E402
from fastapi.responses import JSONResponse, Response  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from basic_memory.api.app import app as basic_memory_app  # noqa: E402
from basic_memory.index.note_content_materialization import (  # noqa: E402
    drain_pending_materializations,
)

# --- Constants ---

# Namespace names arrive from an untrusted caller. Allow a readable subset and
# reject everything else outright rather than silently rewriting it.
NAMESPACE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
PROJECT_NAME_PREFIX = "vmci"
# Frontmatter key holding the caller's metadata as JSON. A single JSON string
# round-trips types exactly; writing the caller's keys straight into frontmatter
# would coerce numbers to strings and could collide with title/type/permalink.
METADATA_KEY = "vmci_metadata"
DEFAULT_RECALL_LIMIT = 10
MAX_RECALL_LIMIT = 50
# Words that carry no signal in an FTS query. Basic Memory's index is lexical:
# an unfiltered natural-language question matches nothing, so the query is
# reduced to content words and OR-ed together (see _fts_query).
QUERY_STOPWORDS = frozenset(
    """a about again all also an and any are as at be been being by can could did do does
    for from get give had has have he her him his how i if in into is it its just list me
    my no not of off on or our out over please she should show so such tell than that the
    their them then there these they this those to told up us use used user users was we
    were what when where which who whom why will with would you your""".split()
)


class NamespaceCreateRequest(BaseModel):
    name: str
    forceCreate: bool = False  # noqa: N815 - wire field name is fixed by Verging


class MemoryStoreRequest(BaseModel):
    content: str = Field(min_length=1)
    title: str | None = None
    metadata: dict[str, Any] | None = None


class MemoryUpdateRequest(BaseModel):
    id: str
    content: str = Field(min_length=1)
    metadata: dict[str, Any] | None = None


class MemoryRecallRequest(BaseModel):
    query: str
    limit: int | None = None


@dataclass
class _Deployment:
    """Process-wide handles created once in the app lifespan."""

    client: AsyncClient
    api_key: str


_deployment: _Deployment | None = None


# --- Authentication ---


def _require_api_key() -> str:
    """Read the scoped product credential the deployment must present."""
    api_key = os.environ.get("VERGING_PRODUCT_KEY", "").strip()
    if not api_key:  # pragma: no cover - guarded at startup in every real run
        raise RuntimeError("VERGING_PRODUCT_KEY must be set for the adapter to accept requests")
    return api_key


async def authorize(authorization: str | None = Header(default=None)) -> None:
    """Reject any request without the exact bearer credential.

    Trigger: missing header, wrong scheme, or a non-matching token.
    Why: the endpoint is public on the internet and holds test namespaces.
    Outcome: 401 with no detail about which half of the check failed.
    """
    assert _deployment is not None  # set by the lifespan before routes are served
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token.strip(), _deployment.api_key):
        raise HTTPException(status_code=401, detail="unauthorized")


# --- Basic Memory access helpers ---


def _client() -> AsyncClient:
    assert _deployment is not None
    return _deployment.client


async def _call(method: str, url: str, **kwargs: Any) -> Any:
    """Call the real Basic Memory API and surface failures as HTTP errors."""
    response = await _client().request(method, url, **kwargs)
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=_detail(response.text))
    return response.json()


def _detail(body: str) -> str:
    try:
        parsed = json.loads(body)
    except ValueError:
        return body[:500]
    detail = parsed.get("detail") if isinstance(parsed, dict) else None
    return str(detail)[:500] if detail is not None else body[:500]


def _project_name(name: str) -> str:
    """Derive a collision-free Basic Memory project name from a namespace name.

    Identifiers are untrusted, so the name is reduced to lowercase alphanumeric
    runs before it ever reaches the project service. Combined with
    BASIC_MEMORY_PROJECT_ROOT this makes path traversal structurally impossible.
    """
    if not NAMESPACE_NAME_PATTERN.match(name):
        raise HTTPException(status_code=422, detail="invalid namespace name")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        raise HTTPException(status_code=422, detail="invalid namespace name")
    return f"{PROJECT_NAME_PREFIX}-{slug}"


def _namespace_id(namespace_id: str) -> str:
    """Validate a namespace id before it is interpolated into an API path."""
    try:
        return str(uuid.UUID(namespace_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="namespace not found") from None


async def _find_project(project_name: str) -> dict[str, Any] | None:
    projects = await _call("GET", "/v2/projects/")
    for project in projects.get("projects", []):
        if project.get("name") == project_name:
            return project
    return None


async def _delete_project(external_id: str) -> bool:
    """Delete a project and its notes. Returns False when it was already gone."""
    response = await _client().delete(f"/v2/projects/{external_id}", params={"delete_notes": True})
    if response.status_code == 404:
        return False
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=_detail(response.text))
    return True


async def _require_namespace(namespace_id: str) -> str:
    """Resolve a namespace id, 404-ing when it does not exist."""
    external_id = _namespace_id(namespace_id)
    response = await _client().get(f"/v2/projects/{external_id}")
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="namespace not found")
    if response.status_code >= 400:  # pragma: no cover - upstream failure
        raise HTTPException(status_code=response.status_code, detail=_detail(response.text))
    return external_id


def _encode_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    return {METADATA_KEY: json.dumps(metadata)} if metadata is not None else None


def _decode_metadata(entity_metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    raw = (entity_metadata or {}).get(METADATA_KEY)
    if not isinstance(raw, str):
        return None
    try:
        decoded = json.loads(raw)
    except ValueError:  # pragma: no cover - only reachable if frontmatter is hand-edited
        return None
    return decoded if isinstance(decoded, dict) else None


def _note_body(content: str | None) -> str:
    """Strip the frontmatter Basic Memory adds so recall returns what was stored."""
    return frontmatter.loads(content or "").content


def _derive_title(title: str | None, content: str) -> str:
    """Pick a human-readable note title; the id, not the title, is the handle."""
    candidate = (title or content.strip().splitlines()[0] if content.strip() else "").strip()
    candidate = re.sub(r"\s+", " ", candidate)[:120].strip()
    # Title becomes a filename, so drop separators outright rather than relying
    # on downstream sanitization.
    candidate = re.sub(r"[\\/:*?\"<>|]", "-", candidate).strip(" .-")
    return candidate or "Note"


async def _settle_writes() -> None:
    """Wait for accepted writes to reach the file and the search index.

    A Basic Memory write returns 202 and materializes the markdown file and its
    index entry on a background worker. Memory CI reads immediately after it
    writes, so the adapter drains that queue before answering.
    """
    await drain_pending_materializations()


def _fts_query(query: str) -> str | None:
    """Turn a natural-language recall query into a lexical FTS query.

    Trigger: every recall. Why: the index is full-text, so "What coffee does the
    user like?" matches nothing verbatim. Outcome: content words OR-ed together,
    which lets partial matches rank instead of dropping out. Returns None when
    the query holds no usable term, so recall answers with an empty result set
    rather than an error.
    """
    tokens = re.findall(r"[A-Za-z0-9_]+", query.lower())
    content_words = [token for token in tokens if len(token) > 1 and token not in QUERY_STOPWORDS]
    usable = content_words or [token for token in tokens if len(token) > 1] or tokens
    return " OR ".join(usable) if usable else None


def _unique_hits(results: Iterable[dict[str, Any]], limit: int) -> list[str]:
    """Collapse a search response to distinct note ids, keeping rank order."""
    ordered: list[str] = []
    seen: set[str] = set()
    for result in results:
        external_id = result.get("external_id")
        if not external_id or external_id in seen:
            continue
        seen.add(external_id)
        ordered.append(external_id)
        if len(ordered) == limit:
            break
    return ordered


# --- Application ---


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the real Basic Memory app in-process for the adapter's lifetime."""
    global _deployment
    api_key = _require_api_key()
    async with basic_memory_app.router.lifespan_context(basic_memory_app):
        async with AsyncClient(
            transport=ASGITransport(app=basic_memory_app),
            base_url="http://basic-memory",
            timeout=60.0,
        ) as client:
            _deployment = _Deployment(client=client, api_key=api_key)
            try:
                yield
            finally:
                _deployment = None


def create_app() -> FastAPI:
    app = FastAPI(title="Verging Memory CI adapter (Basic Memory)", lifespan=lifespan)
    authorized = [Depends(authorize)]

    @app.get("/v1/health")
    async def health() -> dict[str, bool]:
        """Public liveness probe — deliberately unauthenticated."""
        return {"ok": True}

    @app.post("/v1/namespaces", dependencies=authorized)
    async def create_namespace(request: NamespaceCreateRequest) -> dict[str, str]:
        project_name = _project_name(request.name)
        existing = await _find_project(project_name)
        if existing is not None:
            # Trigger: the namespace name is already in use.
            # Why: forceCreate means "give me a clean namespace"; without it the
            # call stays idempotent and hands back the same id.
            # Outcome: recreated empty, or reused as-is.
            if not request.forceCreate:
                return {"id": existing["external_id"]}
            await _delete_project(existing["external_id"])

        created = await _call(
            "POST",
            "/v2/projects/",
            json={
                # path is ignored: BASIC_MEMORY_PROJECT_ROOT derives the real
                # directory from the sanitized project name.
                "name": project_name,
                "path": str(DATA_DIR / "projects" / project_name),
                "set_default": False,
            },
        )
        return {"id": created["new_project"]["external_id"]}

    @app.delete("/v1/namespaces/{namespace_id}", dependencies=authorized)
    async def delete_namespace(namespace_id: str = PathParam(...)) -> Response:
        # Deletion is idempotent: an unknown or already-deleted namespace is a
        # successful reset, not an error.
        try:
            external_id = _namespace_id(namespace_id)
        except HTTPException:
            return Response(status_code=204)
        await _delete_project(external_id)
        return Response(status_code=204)

    @app.post("/v1/namespaces/{namespace_id}/memory/store", dependencies=authorized)
    async def store_memory(
        request: MemoryStoreRequest, namespace_id: str = PathParam(...)
    ) -> dict[str, Any]:
        project_id = await _require_namespace(namespace_id)
        created = await _call(
            "POST",
            f"/v2/projects/{project_id}/knowledge/entities",
            json={
                "title": _derive_title(request.title, request.content),
                # Each note gets its own generated directory so two stores that
                # share a title become two distinct notes instead of colliding
                # on one file path.
                "directory": f"notes/{uuid.uuid4().hex}",
                "content": request.content,
                "entity_metadata": _encode_metadata(request.metadata),
            },
        )
        await _settle_writes()
        return {"ok": True, "id": created["external_id"]}

    @app.post("/v1/namespaces/{namespace_id}/memory/update", dependencies=authorized)
    async def update_memory(
        request: MemoryUpdateRequest, namespace_id: str = PathParam(...)
    ) -> dict[str, Any]:
        project_id = await _require_namespace(namespace_id)
        try:
            note_id = str(uuid.UUID(request.id))
        except ValueError:
            raise HTTPException(status_code=404, detail="memory not found") from None

        response = await _client().get(f"/v2/projects/{project_id}/knowledge/entities/{note_id}")
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail="memory not found")
        if response.status_code >= 400:  # pragma: no cover - upstream failure
            raise HTTPException(status_code=response.status_code, detail=_detail(response.text))
        current = response.json()

        # Reusing the note's own title and directory replaces the markdown file
        # in place, so an update never leaves a second copy behind.
        metadata = (
            request.metadata
            if request.metadata is not None
            else _decode_metadata(current.get("entity_metadata"))
        )
        updated = await _call(
            "PUT",
            f"/v2/projects/{project_id}/knowledge/entities/{note_id}",
            json={
                "title": current["title"],
                "directory": str(Path(current["file_path"]).parent),
                "content": request.content,
                "entity_metadata": _encode_metadata(metadata),
            },
        )
        await _settle_writes()
        return {"ok": True, "id": updated["external_id"]}

    @app.post("/v1/namespaces/{namespace_id}/memory/recall", dependencies=authorized)
    async def recall_memory(
        request: MemoryRecallRequest, namespace_id: str = PathParam(...)
    ) -> dict[str, Any]:
        project_id = await _require_namespace(namespace_id)
        limit = max(1, min(request.limit or DEFAULT_RECALL_LIMIT, MAX_RECALL_LIMIT))
        text = _fts_query(request.query)
        if text is None:
            return {"ok": True, "results": []}

        # Observation and relation hits point back at notes already in the list,
        # so over-fetch and then collapse to distinct notes to fill the limit.
        found = await _call(
            "POST",
            f"/v2/projects/{project_id}/search/",
            params={"page": 1, "page_size": min(limit * 3, MAX_RECALL_LIMIT * 3)},
            json={"text": text, "entity_types": ["entity"]},
        )
        results = []
        for note_id in _unique_hits(found.get("results", []), limit):
            note = await _client().get(f"/v2/projects/{project_id}/knowledge/entities/{note_id}")
            if note.status_code != 200:  # pragma: no cover - deleted mid-recall
                continue
            body = note.json()
            results.append(
                {
                    "id": body["external_id"],
                    "content": _note_body(body.get("content")),
                    "metadata": _decode_metadata(body.get("entity_metadata")),
                }
            )
        return {"ok": True, "results": results}

    @app.exception_handler(HTTPException)
    async def json_http_exception(_: Any, exc: HTTPException) -> JSONResponse:
        """Answer every error as JSON, which is all the wire contract allows."""
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    return app


app = create_app()


def main() -> None:  # pragma: no cover - process entrypoint
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="info")


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
