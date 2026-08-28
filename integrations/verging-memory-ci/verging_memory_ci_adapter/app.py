"""HTTP surface of the Verging Memory CI adapter for Basic Memory.

Routes follow the standardized Verging adapter contract: create an isolated
namespace, store / update / recall memories inside it, then delete it. Every
route except the health probe requires the deployment's scoped bearer
credential.
"""

from __future__ import annotations

import hmac
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from verging_memory_ci_adapter.backend import (
    BasicMemoryBackend,
    MemoryNotFound,
    NamespaceNotFound,
)
from verging_memory_ci_adapter.bootstrap import (
    configure_basic_memory_environment,
    read_credential,
)

HEALTH_PATH = "/v1/health"
MAX_RECALL_LIMIT = 50


# --- Request models ---


class CreateNamespaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    forceCreate: bool = False  # noqa: N815 - the contract's wire name


class StoreRequest(BaseModel):
    content: str = Field(min_length=1)
    title: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] | None = None


class UpdateRequest(BaseModel):
    id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    metadata: dict[str, Any] | None = None


class RecallRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: int | None = Field(default=10, ge=1, le=MAX_RECALL_LIMIT)


# --- Application wiring ---


def get_backend(request: Request) -> BasicMemoryBackend:
    return request.app.state.backend


BackendDep = Depends(get_backend)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:  # pragma: no cover - exercised live
    """Start a real Basic Memory instance and hold one in-process client open."""
    # Imported here, after bootstrap has populated the environment, so Basic
    # Memory's cached config is built from the adapter's directories.
    from basic_memory.config import ConfigManager
    from basic_memory.index.local_schedulers import drain_background_tasks
    from basic_memory.index.note_content_materialization import drain_pending_materializations
    from basic_memory.mcp.async_client import get_client
    from basic_memory.services.initialization import initialize_app

    config = ConfigManager().config
    await initialize_app(config)

    async with AsyncExitStack() as stack:
        http_client = await stack.enter_async_context(get_client())
        app.state.backend = BasicMemoryBackend(http_client, app.state.projects_root)
        try:
            yield
        finally:
            # A note write is accepted before its markdown file is written.
            # Draining both queues on shutdown keeps an accepted write from
            # being lost when the platform stops the container.
            await drain_pending_materializations()
            await drain_background_tasks()


def create_app(data_root: Path | None = None) -> FastAPI:
    """Build the adapter app, pointing Basic Memory at adapter-owned storage."""
    root = configure_basic_memory_environment(data_root)
    credential = read_credential()

    app = FastAPI(
        title="Verging Memory CI adapter for Basic Memory",
        version="1",
        lifespan=lifespan,
    )
    app.state.projects_root = root / "projects"

    @app.middleware("http")
    async def require_credential(request: Request, call_next):
        """Authenticate before anything else looks at the request.

        Running as middleware rather than a dependency matters: a dependency is
        resolved alongside body validation, so a malformed body would answer 422
        to an unauthenticated caller. Here an unauthenticated caller always
        learns exactly one thing — that it is unauthenticated.
        """
        if request.url.path.rstrip("/") == HEALTH_PATH:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(presented.strip(), credential):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.exception_handler(NamespaceNotFound)
    async def namespace_not_found(request: Request, exc: NamespaceNotFound) -> JSONResponse:
        return JSONResponse({"ok": False, "error": "namespace_not_found"}, status_code=404)

    @app.exception_handler(MemoryNotFound)
    async def memory_not_found(request: Request, exc: MemoryNotFound) -> JSONResponse:
        return JSONResponse({"ok": False, "error": "memory_not_found"}, status_code=404)

    # --- Routes ---

    @app.get(HEALTH_PATH)
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/v1/namespaces", status_code=201)
    async def create_namespace(
        body: CreateNamespaceRequest,
        backend: BasicMemoryBackend = BackendDep,
    ) -> dict[str, str]:
        # forceCreate is always honored: every call creates a fresh, isolated
        # Basic Memory project, so a repeated name never reuses another
        # namespace's notes.
        namespace = await backend.create_namespace()
        return {"id": namespace.id}

    @app.post("/v1/namespaces/{namespace_id}/memory/store")
    async def store(
        namespace_id: str,
        body: StoreRequest,
        backend: BasicMemoryBackend = BackendDep,
    ) -> dict[str, Any]:
        memory_id = await backend.store(
            namespace_id,
            content=body.content,
            title=body.title,
            metadata=body.metadata,
        )
        return {"ok": True, "id": memory_id}

    @app.post("/v1/namespaces/{namespace_id}/memory/update")
    async def update(
        namespace_id: str,
        body: UpdateRequest,
        backend: BasicMemoryBackend = BackendDep,
    ) -> dict[str, Any]:
        memory_id = await backend.update(
            namespace_id,
            memory_id=body.id,
            content=body.content,
            metadata=body.metadata,
        )
        return {"ok": True, "id": memory_id}

    @app.post("/v1/namespaces/{namespace_id}/memory/recall")
    async def recall(
        namespace_id: str,
        body: RecallRequest,
        backend: BasicMemoryBackend = BackendDep,
    ) -> dict[str, Any]:
        records = await backend.recall(
            namespace_id,
            query=body.query,
            limit=body.limit or 10,
        )
        return {
            "ok": True,
            "results": [
                {"id": r.id, "content": r.content, "metadata": r.metadata} for r in records
            ],
        }

    @app.delete("/v1/namespaces/{namespace_id}", status_code=204)
    async def delete_namespace(
        namespace_id: str,
        backend: BasicMemoryBackend = BackendDep,
    ) -> Response:
        # Deleting an unknown namespace succeeds: reset is idempotent, and a
        # namespace that is already gone is the state the caller asked for.
        await backend.delete_namespace(namespace_id)
        return Response(status_code=204)

    @app.exception_handler(HTTPException)
    async def http_exception(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"ok": False, "error": exc.detail}, status_code=exc.status_code)

    return app
