"""HTTP surface of the Verging Memory CI product adapter.

The routes below are the contract Verging Memory CI calls. They hold no state:
each one authenticates the caller, validates its input, and hands the work to
Basic Memory through :mod:`verging_adapter.store`.
"""

from __future__ import annotations

import secrets
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Annotated, Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient, Timeout
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from verging_adapter.settings import AdapterSettings
from verging_adapter.store import (
    BasicMemoryStore,
    MemoryNotFound,
    Namespace,
    NamespaceNotFound,
    StoreError,
)

MAX_CONTENT_LENGTH = 200_000
MAX_QUERY_LENGTH = 2_000


# --- Request models ---


class NamespaceCreateRequest(BaseModel):
    """``{name, forceCreate: true}``."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    name: str = Field(min_length=1, max_length=200)
    # Accepted for wire compatibility. Every create already makes a fresh,
    # uniquely named project, so there is nothing for it to force.
    force_create: bool = Field(default=False, alias="forceCreate")


class StoreRequest(BaseModel):
    """``{content, title?, metadata?}``."""

    model_config = ConfigDict(extra="ignore")

    content: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH)
    title: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] | None = None


class UpdateRequest(BaseModel):
    """``{id, content, metadata?}``."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH)
    metadata: dict[str, Any] | None = None


class RecallRequest(BaseModel):
    """``{query, limit?}``."""

    model_config = ConfigDict(extra="ignore")

    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH)
    limit: int = Field(default=10, ge=1, le=50)


# --- Dependencies ---


def _settings(request: Request) -> AdapterSettings:
    settings: AdapterSettings = request.app.state.settings
    return settings


def _store(request: Request) -> BasicMemoryStore:
    store: BasicMemoryStore = request.app.state.store
    return store


async def authorize(request: Request) -> None:
    """Require the scoped product key as ``Authorization: Bearer <key>``."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    # compare_digest keeps a wrong key from being narrowed down by timing.
    if scheme.lower() != "bearer" or not secrets.compare_digest(
        token.strip(), _settings(request).api_key
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid bearer credential",
            headers={"WWW-Authenticate": "Bearer"},
        )


Authorized = Annotated[None, Depends(authorize)]
StoreDep = Annotated[BasicMemoryStore, Depends(_store)]


async def _namespace(namespace_id: str, store: StoreDep) -> Namespace:
    """Resolve an untrusted path id to a namespace this adapter owns."""
    try:
        return await store.resolve_namespace(namespace_id)
    except NamespaceNotFound:
        raise HTTPException(status_code=404, detail="namespace not found")


NamespaceDep = Annotated[Namespace, Depends(_namespace)]


# --- Lifecycle ---


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run Basic Memory in-process for the life of the adapter."""
    settings: AdapterSettings = app.state.settings

    # Deferred import: Basic Memory resolves its config directory the first
    # time it reads config, so settings.prepare() must have set
    # BASIC_MEMORY_CONFIG_DIR before this module is imported.
    from basic_memory.api.app import app as basic_memory_app

    async with AsyncExitStack() as stack:
        # Basic Memory's own lifespan runs migrations, caches the database
        # connections its routes depend on, and starts the watch coordinator.
        await stack.enter_async_context(basic_memory_app.router.lifespan_context(basic_memory_app))
        client = await stack.enter_async_context(
            AsyncClient(
                transport=ASGITransport(app=basic_memory_app),
                base_url="http://basic-memory.internal",
                timeout=Timeout(connect=10.0, read=60.0, write=60.0, pool=60.0),
            )
        )

        store = BasicMemoryStore(client, settings.namespaces_dir)
        await store.ensure_bootstrap_project(settings.bootstrap_dir)
        app.state.store = store
        logger.info("Verging adapter ready", f"data_dir={settings.data_dir}")
        yield


def create_app(settings: AdapterSettings | None = None) -> FastAPI:
    """Build the adapter app, preparing Basic Memory's environment first."""
    resolved = settings or AdapterSettings.from_env()
    resolved.prepare()

    app = FastAPI(
        title="Verging Memory CI adapter for Basic Memory",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = resolved

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"ok": False, "error": exc.detail},
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Literal 422: Starlette is mid-rename between UNPROCESSABLE_ENTITY and
        # UNPROCESSABLE_CONTENT, and either constant warns on one of the versions
        # this repository pins.
        return JSONResponse(
            status_code=422,
            content={"ok": False, "error": "invalid request body", "detail": exc.errors()},
        )

    @app.exception_handler(StoreError)
    async def store_error_handler(_: Request, exc: StoreError) -> JSONResponse:
        logger.warning("Basic Memory rejected an adapter request", f"detail={exc.detail}")
        return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": exc.detail})

    _register_routes(app)
    return app


# --- Routes ---


def _register_routes(app: FastAPI) -> None:
    @app.get("/v1/health")
    async def health() -> dict[str, bool]:
        """Liveness for the deployment and for Verging Memory CI's reachability check."""
        return {"ok": True}

    @app.post("/v1/namespaces", status_code=status.HTTP_201_CREATED)
    async def create_namespace(
        body: NamespaceCreateRequest, _: Authorized, store: StoreDep
    ) -> dict[str, str]:
        namespace = await store.create_namespace(body.name)
        return {"id": namespace.id}

    @app.post("/v1/namespaces/{namespace_id}/memory/store")
    async def store_memory(
        body: StoreRequest, _: Authorized, store: StoreDep, namespace: NamespaceDep
    ) -> dict[str, Any]:
        memory_id = await store.store_memory(
            namespace, content=body.content, title=body.title, metadata=body.metadata
        )
        return {"ok": True, "id": memory_id}

    @app.post("/v1/namespaces/{namespace_id}/memory/update")
    async def update_memory(
        body: UpdateRequest, _: Authorized, store: StoreDep, namespace: NamespaceDep
    ) -> dict[str, Any]:
        try:
            memory_id = await store.update_memory(
                namespace, memory_id=body.id, content=body.content, metadata=body.metadata
            )
        except MemoryNotFound:
            raise HTTPException(status_code=404, detail="memory not found")
        return {"ok": True, "id": memory_id}

    @app.post("/v1/namespaces/{namespace_id}/memory/recall")
    async def recall(
        body: RecallRequest, _: Authorized, store: StoreDep, namespace: NamespaceDep
    ) -> dict[str, Any]:
        memories = await store.recall(namespace, query=body.query, limit=body.limit)
        return {
            "ok": True,
            "results": [
                {"id": memory.id, "content": memory.content, "metadata": memory.metadata}
                for memory in memories
            ],
        }

    @app.delete("/v1/namespaces/{namespace_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_namespace(namespace_id: str, _: Authorized, store: StoreDep) -> Response:
        # Deletion is a reset: an id that is already gone is a success, so the
        # same request can be repeated safely.
        await store.delete_namespace(namespace_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
