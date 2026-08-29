"""Verging Memory CI product adapter for Basic Memory.

Verging Memory CI drives a memory product over a small, fixed HTTP contract:
create an isolated namespace, store and recall notes in it, then delete it.
This module serves that contract on top of the *real* Basic Memory
implementation in this repository — there is no mock store and no canned
answer anywhere in this file.

Shape of the thing:

- The outer FastAPI app below is the only public surface. It speaks the
  Verging contract and requires a scoped bearer credential.
- The real Basic Memory FastAPI app is run *inside* this process. Its lifespan
  is entered by our lifespan, so it gets its own container, database and watch
  coordinator exactly as `basic-memory api` would.
- Every adapter route reaches Basic Memory through an in-process httpx ASGI
  client against that app, which is the same transport MCP tools use
  (see CLAUDE.md, "Tools communicate to api routers via the httpx ASGI
  client (in process)"). Nothing reaches into services or repositories
  directly, so the adapter only depends on the supported v2 HTTP surface.

A Verging namespace maps onto a real Basic Memory project: its own directory
on disk, its own entities, its own search index rows. That is the isolation
boundary the product already documents, so we borrow it rather than inventing
a weaker one.
"""

import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Final

import frontmatter
import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from basic_memory.api.app import app as basic_memory_app

# --- Configuration ---

# Notes live here, never inside the repository checkout: Verging Memory CI
# commits report folders back into the checkout, and test data captured by
# such a commit would leak into the customer's git history.
DEFAULT_NAMESPACE_ROOT: Final = Path("/data/verging-namespaces")

# Every namespace project is named with this prefix so an adapter-created
# project can never be confused with (or collide with) an operator's own
# project in the same Basic Memory instance.
NAMESPACE_PROJECT_PREFIX: Final = "vgn-"

# All notes land in one directory inside the namespace. A fixed literal keeps
# untrusted request data out of the directory component of the file path.
NOTE_DIRECTORY: Final = "memories"

MAX_TITLE_LENGTH: Final = 120
MAX_CONTENT_BYTES: Final = 1_000_000
MAX_METADATA_BYTES: Final = 32_000
DEFAULT_RECALL_LIMIT: Final = 10
MAX_RECALL_LIMIT: Final = 50

# The in-process ASGI client needs a syntactically valid base URL; no packet
# ever leaves the process, so the host is arbitrary but must not be routable.
INTERNAL_BASE_URL: Final = "http://basic-memory.internal"

# Basic Memory materializes an accepted write to disk inside the request, but
# a large namespace can still make an individual call slow; keep the ceiling
# well above p99 rather than letting a slow write surface as a client error.
INTERNAL_TIMEOUT_SECONDS: Final = 120.0


@dataclass(frozen=True, slots=True)
class AdapterSettings:
    """Runtime configuration, read once at startup."""

    api_key: str
    namespace_root: Path


def settings_from_env() -> AdapterSettings:
    """Read adapter configuration from the environment.

    Fails fast: a deployment without a credential would serve a public,
    unauthenticated write API, so refuse to start instead of degrading.
    """
    api_key = os.environ.get("VERGING_PRODUCT_KEY", "")
    if not api_key:
        raise RuntimeError("VERGING_PRODUCT_KEY is required to start the Verging adapter")

    namespace_root = Path(
        os.environ.get("VERGING_ADAPTER_DATA_DIR", str(DEFAULT_NAMESPACE_ROOT))
    ).resolve()
    return AdapterSettings(api_key=api_key, namespace_root=namespace_root)


# --- Request schemas ---
#
# Pydantic is the validation boundary: anything that fails these models is
# rejected with FastAPI's 422 before a single Basic Memory call is made.


class CreateNamespaceRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    name: str = Field(min_length=1, max_length=200)
    force_create: bool = Field(default=False, alias="forceCreate")


class StoreRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    content: str = Field(min_length=1, max_length=MAX_CONTENT_BYTES)
    title: str | None = Field(default=None, max_length=MAX_TITLE_LENGTH)
    metadata: dict[str, Any] | None = None


class UpdateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=MAX_CONTENT_BYTES)
    metadata: dict[str, Any] | None = None


class RecallRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=DEFAULT_RECALL_LIMIT, ge=1, le=MAX_RECALL_LIMIT)


# --- Identifier hygiene ---
#
# Namespace names and note ids arrive from the network. Two of them reach a
# filesystem path (the namespace directory) and all of them reach a URL path
# segment, so both are normalized to a closed character set before use.

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def namespace_slug(name: str) -> str:
    """Reduce an arbitrary namespace name to a safe, stable directory name.

    The result is drawn from ``[a-z0-9-]`` only, so it cannot contain a path
    separator, a parent-directory hop, a leading dash, or a Windows drive
    letter. An empty result means the caller sent a name with no usable
    characters at all, which is a client error rather than a reason to invent
    one.
    """
    slug = _SLUG_STRIP.sub("-", name.strip().lower()).strip("-")[:48].strip("-")
    if not slug:
        raise HTTPException(status_code=400, detail="namespace name has no usable characters")
    return slug


def namespace_directory(root: Path, project_name: str) -> Path:
    """Resolve a namespace's directory and prove it stays under the root.

    ``namespace_slug`` already makes traversal unrepresentable; this second
    check is what actually guarantees containment, so a future change to the
    slug rules cannot silently open an escape.

    The deployment additionally sets ``BASIC_MEMORY_PROJECT_ROOT`` to this same
    root, at which point Basic Memory ignores the submitted path entirely and
    derives ``<root>/<permalink(project name)>`` itself. Passing exactly that
    path keeps the two views of a namespace's location identical whether or not
    the product-side constraint is configured.
    """
    directory = (root / project_name).resolve()
    if directory != root and root not in directory.parents:
        raise HTTPException(status_code=400, detail="invalid namespace name")
    return directory


def validated_id(value: str) -> str:
    """Accept only a UUID as a Basic Memory external id.

    Basic Memory ids are UUIDs. Anything else cannot name an existing resource,
    so it is a 404 rather than a value we pass through to a URL path.
    """
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise HTTPException(status_code=404, detail="not found") from None


def note_body(content: str | None) -> str:
    """Return a note's markdown body without its YAML frontmatter.

    Basic Memory stores frontmatter (title, type, permalink, plus whatever
    metadata was supplied) at the top of the file, so the raw content of a
    stored note is not what `store` was given. Verging carries that metadata in
    the result's own `metadata` field, so repeating it inside `content` would
    both break the store/recall round trip and pad every recall result with the
    same boilerplate. Parsed with the product's own frontmatter library so the
    two agree on where the body starts.
    """
    if not content:
        return ""
    return frontmatter.loads(content).content


def note_title(requested: str | None) -> str:
    """Choose the note title, generating a unique one when none was sent.

    Basic Memory derives the filename from the title, so an absent title must
    still produce something that does not collide with an earlier store.
    """
    if requested is None:
        return f"memory-{uuid.uuid4().hex[:12]}"
    title = " ".join(requested.split())
    if not title:
        raise HTTPException(status_code=400, detail="title has no usable characters")
    return title[:MAX_TITLE_LENGTH]


def validated_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """Bound metadata size; it is written verbatim into note frontmatter."""
    if metadata is None:
        return None
    if len(str(metadata)) > MAX_METADATA_BYTES:
        raise HTTPException(status_code=400, detail="metadata too large")
    return metadata


# --- Basic Memory access ---
#
# One helper per Basic Memory call. Each raises on an unexpected status so a
# broken assumption surfaces as a 502 instead of a confusing empty result.


def _client(request: Request) -> httpx.AsyncClient:
    return request.app.state.basic_memory_client


BasicMemoryClient = Annotated[httpx.AsyncClient, Depends(_client)]


def _settings(request: Request) -> AdapterSettings:
    return request.app.state.adapter_settings


Settings = Annotated[AdapterSettings, Depends(_settings)]


def _unexpected(response: httpx.Response, operation: str) -> HTTPException:
    return HTTPException(
        status_code=502,
        detail=f"basic-memory {operation} failed with status {response.status_code}",
    )


async def resolve_project(client: httpx.AsyncClient, name: str) -> dict[str, Any] | None:
    """Look up a project by name; ``None`` when it does not exist."""
    response = await client.post("/v2/projects/resolve", json={"identifier": name})
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise _unexpected(response, "project resolve")
    return response.json()


async def delete_project(client: httpx.AsyncClient, external_id: str) -> bool:
    """Delete a project and its notes. ``False`` when it was already gone."""
    response = await client.delete(f"/v2/projects/{external_id}", params={"delete_notes": True})
    if response.status_code == 404:
        return False
    if response.status_code != 200:
        raise _unexpected(response, "project delete")
    return True


async def create_project(client: httpx.AsyncClient, name: str, path: Path) -> str:
    """Create a project rooted at ``path`` and return its external id."""
    path.mkdir(parents=True, exist_ok=True)
    response = await client.post(
        "/v2/projects/",
        json={"name": name, "path": str(path), "set_default": False},
    )
    if response.status_code not in (200, 201):
        raise _unexpected(response, "project create")
    created = response.json().get("new_project")
    if not created:
        raise _unexpected(response, "project create")
    return created["external_id"]


async def require_namespace(client: httpx.AsyncClient, namespace_id: str) -> str:
    """Resolve a namespace id, rejecting ids that are not adapter namespaces.

    A caller must not be able to reach an operator's own project (or the
    default project) through this adapter, so membership is checked by the
    namespace prefix rather than by id alone.
    """
    external_id = validated_id(namespace_id)
    project = await resolve_project(client, external_id)
    if project is None or not project["name"].startswith(NAMESPACE_PROJECT_PREFIX):
        raise HTTPException(status_code=404, detail="namespace not found")
    return external_id


async def create_note(
    client: httpx.AsyncClient,
    project_id: str,
    *,
    title: str,
    content: str,
    metadata: dict[str, Any] | None,
) -> str:
    """Write a real note and return its external id.

    Trigger: Basic Memory derives the file path from the title, so a repeated
      title is a 409 conflict.
    Why: `store` is an append-style operation in the Verging contract; a second
      store of the same title must not fail and must not overwrite the first.
    Outcome: retry once under a disambiguated title.
    """
    body = {
        "title": title,
        "content": content,
        "directory": NOTE_DIRECTORY,
        "entity_metadata": metadata,
    }
    response = await client.post(f"/v2/projects/{project_id}/knowledge/entities", json=body)
    if response.status_code == 409:
        body["title"] = f"{title[: MAX_TITLE_LENGTH - 13]} {uuid.uuid4().hex[:8]}"
        response = await client.post(f"/v2/projects/{project_id}/knowledge/entities", json=body)
    if response.status_code not in (200, 201, 202):
        raise _unexpected(response, "note create")
    return response.json()["external_id"]


async def get_note(
    client: httpx.AsyncClient, project_id: str, note_id: str
) -> dict[str, Any] | None:
    response = await client.get(f"/v2/projects/{project_id}/knowledge/entities/{note_id}")
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise _unexpected(response, "note read")
    return response.json()


async def replace_note(
    client: httpx.AsyncClient,
    project_id: str,
    note: dict[str, Any],
    *,
    content: str,
    metadata: dict[str, Any] | None,
) -> str:
    """Replace a note's content in place.

    The PUT targets the note's own external id and reuses its existing title
    and directory, so the same markdown file is rewritten. This is what keeps
    `update` from leaving a second copy behind.
    """
    body = {
        "title": note["title"],
        "content": content,
        "directory": Path(note["file_path"]).parent.as_posix().lstrip("."),
        "note_type": note.get("note_type") or "note",
        "entity_metadata": metadata if metadata is not None else note.get("entity_metadata"),
    }
    response = await client.put(
        f"/v2/projects/{project_id}/knowledge/entities/{note['external_id']}", json=body
    )
    if response.status_code not in (200, 202):
        raise _unexpected(response, "note update")
    return response.json()["external_id"]


async def search_notes(
    client: httpx.AsyncClient, project_id: str, query: str, limit: int
) -> list[str]:
    """Return note ids for the best matches, most relevant first.

    A full-text hit can be an observation or relation row rather than the note
    itself; every row carries the owning note's external id, so results are
    collapsed onto distinct notes while preserving the search service's order.
    """
    response = await client.post(
        f"/v2/projects/{project_id}/search/",
        json={"text": query},
        params={"page": 1, "page_size": limit},
    )
    if response.status_code != 200:
        raise _unexpected(response, "search")

    ordered: list[str] = []
    for result in response.json()["results"]:
        external_id = result.get("external_id")
        if external_id and external_id not in ordered:
            ordered.append(external_id)
    return ordered[:limit]


# --- Application ---


def create_app() -> FastAPI:
    settings = settings_from_env()
    settings.namespace_root.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Entering Basic Memory's own lifespan is what makes this the real
        # implementation: it builds the API container, runs migrations, opens
        # the database and starts the watch coordinator. The inner app is
        # never mounted publicly, so the only reachable routes are ours.
        async with basic_memory_app.router.lifespan_context(basic_memory_app):
            transport = httpx.ASGITransport(app=basic_memory_app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url=INTERNAL_BASE_URL,
                timeout=INTERNAL_TIMEOUT_SECONDS,
            ) as client:
                app.state.basic_memory_client = client
                yield

    app = FastAPI(title="Basic Memory — Verging Memory CI adapter", lifespan=lifespan)
    app.state.adapter_settings = settings

    @app.middleware("http")
    async def require_credential(request: Request, call_next):
        """Authenticate before anything else looks at the request.

        Trigger: any /v1 request other than the health probe.
        Why: as a route dependency this would run alongside body validation, so
          a malformed body on an unauthenticated request could answer 422 and
          confirm the route shape to an anonymous caller.
        Outcome: a missing or wrong credential is always a bare 401.
        """
        if request.url.path != "/v1/health":
            header = request.headers.get("authorization", "")
            scheme, _, token = header.partition(" ")
            if scheme.lower() != "bearer" or not secrets.compare_digest(token, settings.api_key):
                return JSONResponse(status_code=401, content={"error": "unauthorized"})
        return await call_next(request)

    # --- Health ---

    @app.get("/v1/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    # --- Namespace lifecycle ---

    @app.post("/v1/namespaces")
    async def create_namespace(
        body: CreateNamespaceRequest, client: BasicMemoryClient, config: Settings
    ) -> dict[str, str]:
        slug = namespace_slug(body.name)
        project_name = f"{NAMESPACE_PROJECT_PREFIX}{slug}"
        directory = namespace_directory(config.namespace_root, project_name)

        existing = await resolve_project(client, project_name)
        if existing is not None:
            # Trigger: the namespace name is already in use.
            # Why: forceCreate asks for a clean namespace, and a leftover from
            #   an earlier run would otherwise pollute recall results.
            # Outcome: drop the old project and its notes, then build fresh.
            if not body.force_create:
                return {"id": existing["external_id"]}
            await delete_project(client, existing["external_id"])

        return {"id": await create_project(client, project_name, directory)}

    @app.delete("/v1/namespaces/{namespace_id}")
    async def remove_namespace(namespace_id: str, client: BasicMemoryClient) -> JSONResponse:
        external_id = validated_id(namespace_id)
        project = await resolve_project(client, external_id)
        # A namespace that is already gone — or was never ours — is reported as
        # 404, which the contract accepts, so repeated deletes are safe.
        if project is None or not project["name"].startswith(NAMESPACE_PROJECT_PREFIX):
            return JSONResponse(status_code=404, content={"ok": False, "error": "not found"})
        await delete_project(client, external_id)
        return JSONResponse(status_code=200, content={"ok": True})

    # --- Memory operations ---

    @app.post("/v1/namespaces/{namespace_id}/memory/store")
    async def store(
        namespace_id: str, body: StoreRequest, client: BasicMemoryClient
    ) -> dict[str, Any]:
        project_id = await require_namespace(client, namespace_id)
        note_id = await create_note(
            client,
            project_id,
            title=note_title(body.title),
            content=body.content,
            metadata=validated_metadata(body.metadata),
        )
        return {"ok": True, "id": note_id}

    @app.post("/v1/namespaces/{namespace_id}/memory/update")
    async def update(
        namespace_id: str, body: UpdateRequest, client: BasicMemoryClient
    ) -> dict[str, Any]:
        project_id = await require_namespace(client, namespace_id)
        note = await get_note(client, project_id, validated_id(body.id))
        if note is None:
            raise HTTPException(status_code=404, detail="memory not found")
        note_id = await replace_note(
            client,
            project_id,
            note,
            content=body.content,
            metadata=validated_metadata(body.metadata),
        )
        return {"ok": True, "id": note_id}

    @app.post("/v1/namespaces/{namespace_id}/memory/recall")
    async def recall(
        namespace_id: str, body: RecallRequest, client: BasicMemoryClient
    ) -> dict[str, Any]:
        project_id = await require_namespace(client, namespace_id)
        results = []
        for note_id in await search_notes(client, project_id, body.query, body.limit):
            note = await get_note(client, project_id, note_id)
            # A note deleted between the search and this read is simply no
            # longer a result; the search index catches up on the next write.
            if note is None:
                continue
            results.append(
                {
                    "id": note["external_id"],
                    "content": note_body(note.get("content")),
                    "metadata": note.get("entity_metadata"),
                }
            )
        return {"ok": True, "results": results}

    return app
