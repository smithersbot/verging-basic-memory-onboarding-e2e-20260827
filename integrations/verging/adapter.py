"""Verging Memory CI adapter for Basic Memory.

Exposes the standardized Verging Memory CI wire shape (namespace lifecycle plus
store / update / recall) on top of the real Basic Memory implementation in this
repository. Every operation is served by Basic Memory's own v2 HTTP API, mounted
in-process over ASGI, so notes are written as real markdown files in a real
Basic Memory project and recall is a real Basic Memory search.

Run it with:

    python integrations/verging/adapter.py

Configuration (environment):
  VERGING_PRODUCT_KEY   required bearer credential for every /v1 route but health
  VERGING_DATA_DIR      where Basic Memory state lives (must be outside the checkout)
  PORT                  listen port (default 8080)
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _data_dir() -> Path:
    configured = os.environ.get("VERGING_DATA_DIR")
    if configured:
        return Path(configured)
    # Never inside the checkout: report commits must not be able to capture it.
    return Path(tempfile.gettempdir()) / "verging-memory-ci-data"


DATA_DIR = _data_dir()
PROJECTS_DIR = DATA_DIR / "namespaces"
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

# Basic Memory reads its configuration from the environment at startup, so these
# have to be in place before anything under basic_memory is imported.
os.environ.setdefault("BASIC_MEMORY_CONFIG_DIR", str(DATA_DIR / "config"))
os.environ.setdefault("BASIC_MEMORY_PROJECT_ROOT", str(PROJECTS_DIR))
# FTS retrieval only: semantic retrieval would pull embedding models at runtime.
os.environ.setdefault("BASIC_MEMORY_SEMANTIC_SEARCH_ENABLED", "false")
os.environ.setdefault("BASIC_MEMORY_UPDATE_PERMALINKS_ON_MOVE", "false")

import hmac  # noqa: E402
import re  # noqa: E402
import uuid  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402
from typing import Any, Optional  # noqa: E402

import httpx  # noqa: E402
from fastapi import Depends, FastAPI, Header, HTTPException, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from basic_memory.api.app import app as basic_memory_app  # noqa: E402

NOTE_DIRECTORY = "notes"
API_KEY_ENV = "VERGING_PRODUCT_KEY"


# --- wire models -------------------------------------------------------------


class CreateNamespaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    forceCreate: bool = True  # noqa: N815 - fixed by the Verging wire contract


class StoreRequest(BaseModel):
    content: str = Field(min_length=1)
    title: Optional[str] = Field(default=None, max_length=200)
    metadata: Optional[dict[str, Any]] = None


class UpdateRequest(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    metadata: Optional[dict[str, Any]] = None


class RecallRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: Optional[int] = Field(default=10, ge=1, le=100)


# --- helpers -----------------------------------------------------------------


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:24] or "namespace"


def _namespace_project_name(requested: str) -> str:
    """A collision-free Basic Memory project name for a requested namespace.

    Basic Memory derives the on-disk directory from this name under
    BASIC_MEMORY_PROJECT_ROOT, so the caller's string never reaches the
    filesystem unsanitized and cannot escape the root.
    """
    return f"ns-{_slug(requested)}-{uuid.uuid4().hex[:12]}"


def _valid_namespace_id(namespace_id: str) -> str:
    """Namespace ids are Basic Memory project UUIDs; anything else is rejected."""
    try:
        return str(uuid.UUID(namespace_id))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail="namespace not found")


def _valid_note_id(note_id: str) -> str:
    try:
        return str(uuid.UUID(note_id))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail="note not found")


def _title_for(content: str, title: Optional[str]) -> str:
    if title and title.strip():
        return title.strip()[:200]
    first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
    return (first_line[:80] or f"note-{uuid.uuid4().hex[:8]}").lstrip("#").strip() or "note"


def _client(request: Request) -> httpx.AsyncClient:
    return request.app.state.basic_memory


async def _require_credential(authorization: Optional[str] = Header(default=None)) -> None:
    expected = os.environ.get(API_KEY_ENV, "")
    if not expected:  # pragma: no cover - refuse to run unauthenticated
        raise HTTPException(status_code=503, detail="adapter credential not configured")
    scheme, _, presented = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not presented:
        raise HTTPException(status_code=401, detail="missing bearer credential")
    if not hmac.compare_digest(presented.strip(), expected):
        raise HTTPException(status_code=401, detail="invalid credential")


async def _namespace_exists(client: httpx.AsyncClient, namespace_id: str) -> dict[str, Any]:
    response = await client.get(f"/v2/projects/{namespace_id}")
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="namespace not found")
    _raise_for_upstream(response)
    return response.json()


def _raise_for_upstream(response: httpx.Response) -> None:
    if response.is_success:
        return
    detail = "basic-memory request failed"
    try:
        body = response.json()
        detail = body.get("detail", detail) if isinstance(body, dict) else detail
    except ValueError:  # pragma: no cover - non-JSON upstream error
        pass
    # 4xx from Basic Memory means the request itself was unusable; anything else
    # is an adapter-side failure.
    status = response.status_code if 400 <= response.status_code < 500 else 502
    raise HTTPException(status_code=status, detail=str(detail))


# --- app ---------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with basic_memory_app.router.lifespan_context(basic_memory_app):
        transport = httpx.ASGITransport(app=basic_memory_app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://basic-memory", timeout=120.0
        ) as client:
            app.state.basic_memory = client
            yield


app = FastAPI(
    title="Verging Memory CI adapter for Basic Memory",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": str(exc.detail)})


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content={"error": "internal adapter error"})


@app.get("/v1/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.post("/v1/namespaces", dependencies=[Depends(_require_credential)])
async def create_namespace(body: CreateNamespaceRequest, request: Request) -> dict[str, str]:
    client = _client(request)
    name = _namespace_project_name(body.name)
    response = await client.post(
        "/v2/projects/",
        json={"name": name, "path": str(PROJECTS_DIR / name), "set_default": False},
    )
    _raise_for_upstream(response)
    project = (response.json() or {}).get("new_project") or {}
    external_id = project.get("external_id")
    if not external_id:  # pragma: no cover - upstream contract change
        raise HTTPException(status_code=502, detail="basic-memory returned no namespace id")
    return {"id": external_id}


@app.post("/v1/namespaces/{namespace_id}/memory/store", dependencies=[Depends(_require_credential)])
async def store(namespace_id: str, body: StoreRequest, request: Request) -> dict[str, Any]:
    namespace = _valid_namespace_id(namespace_id)
    client = _client(request)
    await _namespace_exists(client, namespace)

    title = _title_for(body.content, body.title)
    payload: dict[str, Any] = {
        "title": title,
        "content": body.content,
        "directory": NOTE_DIRECTORY,
        "entity_metadata": body.metadata or None,
    }
    response = await client.post(f"/v2/projects/{namespace}/knowledge/entities", json=payload)
    if response.status_code == 409:
        # Same title already used in this namespace: keep both notes rather than
        # letting the second store overwrite the first.
        payload["title"] = f"{title} {uuid.uuid4().hex[:6]}"
        response = await client.post(f"/v2/projects/{namespace}/knowledge/entities", json=payload)
    _raise_for_upstream(response)
    note = response.json()
    return {"ok": True, "id": note["external_id"]}


@app.post(
    "/v1/namespaces/{namespace_id}/memory/update", dependencies=[Depends(_require_credential)]
)
async def update(namespace_id: str, body: UpdateRequest, request: Request) -> dict[str, Any]:
    namespace = _valid_namespace_id(namespace_id)
    note_id = _valid_note_id(body.id)
    client = _client(request)
    await _namespace_exists(client, namespace)

    existing = await client.get(f"/v2/projects/{namespace}/knowledge/entities/{note_id}")
    if existing.status_code == 404:
        raise HTTPException(status_code=404, detail="note not found")
    _raise_for_upstream(existing)
    current = existing.json()

    # Keep title and directory so the update replaces the note in place instead
    # of writing a second file.
    file_path = current.get("file_path") or f"{NOTE_DIRECTORY}/{current['title']}.md"
    directory = str(Path(file_path).parent)
    payload = {
        "title": current["title"],
        "content": body.content,
        "directory": NOTE_DIRECTORY if directory in ("", ".") else directory,
        "entity_metadata": body.metadata
        if body.metadata is not None
        else current.get("entity_metadata"),
    }
    response = await client.put(
        f"/v2/projects/{namespace}/knowledge/entities/{note_id}", json=payload
    )
    _raise_for_upstream(response)
    return {"ok": True, "id": response.json()["external_id"]}


@app.post(
    "/v1/namespaces/{namespace_id}/memory/recall", dependencies=[Depends(_require_credential)]
)
async def recall(namespace_id: str, body: RecallRequest, request: Request) -> dict[str, Any]:
    namespace = _valid_namespace_id(namespace_id)
    client = _client(request)
    await _namespace_exists(client, namespace)

    limit = body.limit or 10
    response = await client.post(
        f"/v2/projects/{namespace}/search/",
        params={"page": 1, "page_size": max(limit * 3, limit)},
        json={"text": body.query},
    )
    if response.status_code == 400:
        # Basic Memory rejected the raw full-text expression; retry with the
        # plain words of the query before giving up on it.
        relaxed = " ".join(re.findall(r"[\w']+", body.query))
        if relaxed:
            response = await client.post(
                f"/v2/projects/{namespace}/search/",
                params={"page": 1, "page_size": max(limit * 3, limit)},
                json={"text": relaxed},
            )
    _raise_for_upstream(response)

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hit in response.json().get("results", []):
        external_id = hit.get("external_id")
        if not external_id or external_id in seen:
            continue
        seen.add(external_id)
        note = await client.get(f"/v2/projects/{namespace}/knowledge/entities/{external_id}")
        if note.status_code != 200:  # pragma: no cover - note removed mid-search
            continue
        note_body = note.json()
        entry: dict[str, Any] = {
            "id": external_id,
            "content": note_body.get("content") or hit.get("content") or "",
        }
        metadata = note_body.get("entity_metadata")
        if metadata:
            entry["metadata"] = metadata
        results.append(entry)
        if len(results) >= limit:
            break
    return {"ok": True, "results": results}


@app.delete("/v1/namespaces/{namespace_id}", dependencies=[Depends(_require_credential)])
async def delete_namespace(namespace_id: str, request: Request) -> dict[str, Any]:
    namespace = _valid_namespace_id(namespace_id)
    client = _client(request)
    response = await client.delete(f"/v2/projects/{namespace}", params={"delete_notes": "true"})
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="namespace not found")
    _raise_for_upstream(response)
    return {"ok": True}


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        access_log=False,
    )
