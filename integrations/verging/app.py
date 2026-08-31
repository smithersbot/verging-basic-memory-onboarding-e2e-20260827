"""HTTP adapter serving the Verging Memory CI contract from real Basic Memory.

Wire shape (standardized adapter):

    GET    /v1/health                              -> {"ok": true}
    POST   /v1/namespaces                          -> {"id": ...}
    POST   /v1/namespaces/{id}/memory/store        -> {"ok": true, "id": ...}
    POST   /v1/namespaces/{id}/memory/update       -> {"ok": true, "id": ...}
    POST   /v1/namespaces/{id}/memory/recall       -> {"ok": true, "results": [...]}
    DELETE /v1/namespaces/{id}                     -> {"ok": true}

Every route but health requires ``Authorization: Bearer <VERGING_PRODUCT_KEY>``.

Nothing here simulates memory. A namespace is a real Basic Memory project, a
stored memory is a real markdown note written through the v2 knowledge API, and
recall is a real Basic Memory search over that project's index.
"""

from __future__ import annotations

import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import PurePosixPath
from typing import Annotated, Any, AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Request, status
from fastapi.responses import JSONResponse
from fastmcp.exceptions import ToolError
from httpx import AsyncClient
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from basic_memory.config import ConfigManager
from basic_memory.mcp.async_client import get_client
from basic_memory.mcp.clients import KnowledgeClient, ProjectClient, SearchClient
from basic_memory.schemas.base import Entity
from basic_memory.schemas.project_info import ProjectItem
from basic_memory.schemas.search import SearchQuery
from basic_memory.schemas.v2.entity import EntityResponseV2
from basic_memory.services.initialization import initialize_app

from . import PRODUCT_KEY_ENV

# Every project the adapter owns carries this prefix. Namespace routes refuse to
# address a project without it, so the adapter can never read, write or delete a
# Basic Memory project it did not create (including the default project).
NAMESPACE_PREFIX = "verging-ns-"

# Directory holding one namespace's notes. Each stored memory gets its own
# subdirectory keyed by a random token: Basic Memory derives a note's filename
# from its title, so two memories sharing a title would otherwise collide on one
# file and the second store would overwrite the first.
MEMORY_DIRECTORY = "memories"

DEFAULT_RECALL_LIMIT = 10
MAX_RECALL_LIMIT = 50
MAX_NAMESPACE_NAME_LENGTH = 128
MAX_TITLE_LENGTH = 120

_SLUG_SEPARATOR_PATTERN = re.compile(r"[^a-z0-9]+")


# --- Request and response models ---


class CreateNamespaceRequest(BaseModel):
    """Namespace creation request. ``forceCreate`` resets an existing namespace."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(min_length=1, max_length=MAX_NAMESPACE_NAME_LENGTH)
    force_create: bool = Field(default=False, alias="forceCreate")


class StoreRequest(BaseModel):
    """A memory to write into a namespace."""

    content: str = Field(min_length=1)
    title: str | None = Field(default=None, max_length=MAX_TITLE_LENGTH)
    metadata: dict[str, Any] | None = None


class UpdateRequest(BaseModel):
    """A full replacement of one previously stored memory."""

    id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    metadata: dict[str, Any] | None = None


class RecallRequest(BaseModel):
    """A search over one namespace's notes."""

    query: str = Field(min_length=1)
    limit: int | None = Field(default=None, ge=1)


# --- Credential and namespace guards ---


def _product_key() -> str:
    """The configured bearer credential, or fail loudly if the deployment has none."""
    key = os.environ.get(PRODUCT_KEY_ENV, "")
    if not key:
        raise RuntimeError(f"{PRODUCT_KEY_ENV} is not set; the adapter cannot authenticate callers")
    return key


def require_credential(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Reject any caller that does not present the scoped product credential."""
    expected = _product_key()
    scheme, _, presented = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not presented:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer credential",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # compare_digest keeps the comparison time independent of how much of the
    # credential a caller guessed correctly.
    if not secrets.compare_digest(presented.strip(), expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer credential",
            headers={"WWW-Authenticate": "Bearer"},
        )


CredentialDep = Annotated[None, Depends(require_credential)]


async def basic_memory_client() -> AsyncIterator[AsyncClient]:
    """One in-process Basic Memory API client per request."""
    async with get_client() as client:
        yield client


ClientDep = Annotated[AsyncClient, Depends(basic_memory_client)]

# Namespace ids are Basic Memory project external ids and are interpolated into
# an API path, so they are validated as UUIDs before they reach any request.
NamespaceIdPath = Annotated[str, Path(min_length=1, max_length=64)]


def _slugify(value: str) -> str:
    """Reduce untrusted text to the ``[a-z0-9-]`` alphabet Basic Memory names use."""
    return _SLUG_SEPARATOR_PATTERN.sub("-", value.strip().lower()).strip("-")


def _project_name(namespace_name: str) -> str:
    """Map a caller's namespace name onto the adapter's project namespace."""
    slug = _slugify(namespace_name)
    if not slug:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="namespace name must contain at least one letter or digit",
        )
    return f"{NAMESPACE_PREFIX}{slug}"


def _is_namespace_id(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


async def _find_project(client: AsyncClient, *, name: str) -> ProjectItem | None:
    """Look up one adapter-owned project by name."""
    projects = await ProjectClient(client).list_projects()
    for project in projects.projects:
        if project.name == name:
            return project
    return None


async def _require_namespace(client: AsyncClient, namespace_id: str) -> ProjectItem:
    """Resolve a namespace id to the adapter-owned project it names.

    Trigger: any namespace-scoped route.
    Why: an unknown, malformed or foreign project id must read as a missing
         namespace rather than reaching the Basic Memory API, which would surface
         it as a backend failure and could address a project the adapter does not
         own.
    Outcome: handlers below can assume an isolated, adapter-created project.
    """
    if not _is_namespace_id(namespace_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown namespace")

    projects = await ProjectClient(client).list_projects()
    for project in projects.projects:
        if project.external_id == namespace_id and project.name.startswith(NAMESPACE_PREFIX):
            return project
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown namespace")


# --- Application ---


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:  # pragma: no cover
    """Run Basic Memory's own initialization before serving any request."""
    # Fail fast at startup rather than 500-ing per request: a deployment without
    # the credential can never serve a Verging release.
    _product_key()

    config = ConfigManager().config
    await initialize_app(config)
    logger.info(
        "Verging Memory CI adapter ready",
        project_root=config.project_root,
        default_project=config.default_project,
    )
    yield


app = FastAPI(
    title="Verging Memory CI adapter for Basic Memory",
    description="Standardized Verging Memory CI adapter backed by real Basic Memory.",
    lifespan=lifespan,
)


@app.exception_handler(ToolError)
async def basic_memory_error_handler(request: Request, exc: ToolError) -> JSONResponse:
    """Report a failed Basic Memory call as a bad gateway without leaking internals."""
    logger.error(f"Basic Memory call failed for {request.method} {request.url.path}: {exc}")
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"ok": False, "error": "basic_memory_request_failed"},
    )


@app.get("/v1/health")
async def health() -> dict[str, bool]:
    """Public liveness probe. Deliberately unauthenticated."""
    return {"ok": True}


@app.post("/v1/namespaces", status_code=status.HTTP_201_CREATED)
async def create_namespace(
    request: CreateNamespaceRequest,
    client: ClientDep,
    _credential: CredentialDep,
) -> dict[str, str]:
    """Create an isolated Basic Memory project and return its stable id."""
    name = _project_name(request.name)
    project_client = ProjectClient(client)

    existing = await _find_project(client, name=name)
    if existing is not None:
        # Trigger: the namespace name is already in use.
        # Why: forceCreate asks for a namespace that starts empty, so the old
        #      project and its notes go away first; without it the call is
        #      idempotent and returns the namespace already in place.
        # Outcome: forceCreate always yields a fresh, empty namespace.
        if not request.force_create:
            return {"id": existing.external_id}
        await project_client.delete_project(existing.external_id, delete_notes=True)

    # set_default stays False so a namespace is always deletable: Basic Memory
    # refuses to delete the default project.
    created = await project_client.create_project(
        {"name": name, "path": name, "set_default": False}
    )
    project = created.new_project
    if project is None:  # pragma: no cover
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Basic Memory did not return the created project",
        )
    logger.info(f"Created namespace project name={project.name} id={project.external_id}")
    return {"id": project.external_id}


@app.delete("/v1/namespaces/{namespace_id}")
async def delete_namespace(
    namespace_id: NamespaceIdPath,
    client: ClientDep,
    _credential: CredentialDep,
) -> dict[str, bool]:
    """Delete a namespace and its notes. Deleting an absent namespace is a 404."""
    project = await _require_namespace(client, namespace_id)
    await ProjectClient(client).delete_project(project.external_id, delete_notes=True)
    logger.info(f"Deleted namespace project name={project.name} id={project.external_id}")
    return {"ok": True}


@app.post("/v1/namespaces/{namespace_id}/memory/store")
async def store_memory(
    namespace_id: NamespaceIdPath,
    request: StoreRequest,
    client: ClientDep,
    _credential: CredentialDep,
) -> dict[str, Any]:
    """Write one memory as a real markdown note in the namespace's project."""
    project = await _require_namespace(client, namespace_id)

    entity = Entity(
        title=_note_title(request.title, request.content),
        directory=f"{MEMORY_DIRECTORY}/{uuid.uuid4().hex[:12]}",
        content=request.content,
        entity_metadata=request.metadata or None,
    )
    created = await KnowledgeClient(client, project.external_id).create_entity(entity.model_dump())
    return {"ok": True, "id": _entity_id(created.external_id)}


@app.post("/v1/namespaces/{namespace_id}/memory/update")
async def update_memory(
    namespace_id: NamespaceIdPath,
    request: UpdateRequest,
    client: ClientDep,
    _credential: CredentialDep,
) -> dict[str, Any]:
    """Replace one stored memory in place, keeping its id and file path."""
    project = await _require_namespace(client, namespace_id)
    existing = await _read_entity(client, project_id=project.external_id, entity_id=request.id)

    # Reusing the stored title and directory keeps the note's file path stable, so
    # the replacement lands on the same file instead of adding a second note that
    # recall would return alongside the stale one.
    directory = PurePosixPath(existing.file_path).parent.as_posix()
    entity = Entity(
        title=existing.title,
        directory="" if directory == "." else directory,
        note_type=existing.note_type,
        content=request.content,
        entity_metadata=request.metadata if request.metadata is not None else None,
    )
    updated = await KnowledgeClient(client, project.external_id).update_entity(
        existing.external_id, entity.model_dump()
    )
    return {"ok": True, "id": _entity_id(updated.external_id)}


@app.post("/v1/namespaces/{namespace_id}/memory/recall")
async def recall_memory(
    namespace_id: NamespaceIdPath,
    request: RecallRequest,
    client: ClientDep,
    _credential: CredentialDep,
) -> dict[str, Any]:
    """Return the namespace's best real Basic Memory matches for a query."""
    project = await _require_namespace(client, namespace_id)
    limit = min(request.limit or DEFAULT_RECALL_LIMIT, MAX_RECALL_LIMIT)

    query = SearchQuery(text=request.query)
    found = await SearchClient(client, project.external_id).search(
        query.model_dump(), page=1, page_size=limit
    )

    # A search hit can be the note itself or one of its observations/relations,
    # and every hit carries the owning note's external id. Collapse them to one
    # entry per note, keeping the ranking Basic Memory returned.
    ranked_ids: list[str] = []
    for hit in found.results:
        if hit.external_id and hit.external_id not in ranked_ids:
            ranked_ids.append(hit.external_id)

    results: list[dict[str, Any]] = []
    for entity_id in ranked_ids[:limit]:
        entity = await _read_entity(client, project_id=project.external_id, entity_id=entity_id)
        results.append(
            {
                "id": entity.external_id,
                "content": entity.content or "",
                "metadata": entity.entity_metadata or {},
            }
        )
    return {"ok": True, "results": results}


# --- Basic Memory helpers ---


async def _read_entity(client: AsyncClient, *, project_id: str, entity_id: str) -> EntityResponseV2:
    """Read one note, distinguishing an unknown id from a backend failure.

    The typed knowledge client raises one ToolError for every non-2xx status, and
    this adapter has to answer 404 for a memory id that does not exist while still
    reporting a real backend failure as 502. So the lookup goes through the same
    in-process API path with its status code kept intact.
    """
    if not _is_namespace_id(entity_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown memory id")

    response = await client.get(f"/v2/projects/{project_id}/knowledge/entities/{entity_id}")
    if response.status_code == status.HTTP_404_NOT_FOUND:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown memory id")
    if not response.is_success:
        logger.error(
            f"Basic Memory entity read failed status={response.status_code} entity={entity_id}"
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="basic_memory_request_failed"
        )
    return EntityResponseV2.model_validate(response.json())


def _entity_id(external_id: str | None) -> str:
    """Every v2 note carries an external id; a missing one is a backend contract break."""
    if not external_id:  # pragma: no cover
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Basic Memory returned a note without an id",
        )
    return external_id


def _note_title(title: str | None, content: str) -> str:
    """Use the caller's title, else the content's first line, else a constant."""
    if title and title.strip():
        return title.strip()[:MAX_TITLE_LENGTH]
    for line in content.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:MAX_TITLE_LENGTH]
    return "Memory"  # pragma: no cover


__all__ = ["app"]


def main() -> None:  # pragma: no cover
    """Serve the adapter. Railway supplies the port through ``PORT``."""
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",  # noqa: S104 - the platform terminates TLS in front of the container
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
