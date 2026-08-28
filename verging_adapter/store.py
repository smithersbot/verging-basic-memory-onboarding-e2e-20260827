"""Namespace and memory operations, served by real Basic Memory.

Every call in this module crosses Basic Memory's own v2 HTTP API through an
in-process ASGI client — the same transport Basic Memory's MCP tools use
(see AGENTS.md, "Async Client Pattern"). Nothing here caches, shadows or
reimplements storage: namespaces are Basic Memory projects, memories are
markdown notes, and recall is Basic Memory's search index.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

import frontmatter
from httpx import AsyncClient, Response
from loguru import logger

# Basic Memory identifies projects and entities by external UUID. Ids arrive
# from the network, so they are matched against this pattern before they are
# ever interpolated into a URL or compared to a path.
_UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# Namespace directory names are built from the caller's name, so they are
# reduced to this alphabet before touching the filesystem.
_SLUG_ALLOWED = re.compile(r"[^a-z0-9]+")

_BOOTSTRAP_PROJECT_NAME = "verging-adapter-bootstrap"
_MEMORY_ROOT_DIRECTORY = "memories"
_MAX_TITLE_LENGTH = 120
_MAX_SEARCH_PAGE_SIZE = 50


class StoreError(Exception):
    """Basic Memory answered a request in a way the adapter cannot honor."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"{status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class NamespaceNotFound(Exception):
    """The requested namespace is not one of this adapter's namespaces."""


class MemoryNotFound(Exception):
    """The requested memory does not exist inside the given namespace."""


@dataclass(frozen=True)
class Namespace:
    """A Basic Memory project this adapter owns."""

    id: str
    name: str
    path: Path


@dataclass(frozen=True)
class Memory:
    """A stored note, as returned to Verging Memory CI."""

    id: str
    content: str
    metadata: dict[str, Any] | None


def _slugify(name: str) -> str:
    """Reduce a caller-supplied namespace name to a safe directory fragment."""
    slug = _SLUG_ALLOWED.sub("-", name.strip().lower()).strip("-")[:40].strip("-")
    return slug or "namespace"


def _note_body(markdown: str) -> str:
    """Return what the caller stored, without Basic Memory's frontmatter.

    A note's file is the memory plus the YAML block Basic Memory maintains
    (title, type, permalink, and any metadata passed at store time). Recall
    answers with the memory; the same fields come back separately as metadata.
    """
    return frontmatter.loads(markdown).content.strip()


def _derive_title(content: str) -> str:
    """Name an untitled memory after its opening line."""
    first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
    return first_line[:_MAX_TITLE_LENGTH] if first_line else "Memory"


class BasicMemoryStore:
    """Adapter-facing operations over a running Basic Memory API app."""

    def __init__(self, client: AsyncClient, namespaces_dir: Path) -> None:
        self._client = client
        self._namespaces_dir = namespaces_dir.resolve()

    # --- Startup ---

    async def ensure_bootstrap_project(self, bootstrap_dir: Path) -> None:
        """Guarantee a default project that is not a namespace.

        Trigger: adapter startup.
        Why: Basic Memory promotes the first project it sees to default and
        then refuses to delete it. Without a dedicated default, the first
        namespace created would become undeletable.
        Outcome: every namespace project can be deleted on reset.
        """
        response = await self._client.post(
            "/v2/projects/",
            json={
                "name": _BOOTSTRAP_PROJECT_NAME,
                "path": str(bootstrap_dir),
                "set_default": True,
            },
        )
        # 201 created, 200 when this container restarts onto an existing volume.
        if response.status_code not in (200, 201):
            raise StoreError(502, f"could not create the bootstrap project: {response.text}")

    # --- Namespaces ---

    async def create_namespace(self, name: str) -> Namespace:
        """Create an isolated Basic Memory project for this namespace.

        The caller's name is advisory: the directory (and the Basic Memory
        project name) always carries a fresh UUID suffix, so repeated creates
        with the same name never collide and never share a directory tree.
        """
        directory_name = f"{_slugify(name)}-{uuid4().hex}"
        path = (self._namespaces_dir / directory_name).resolve()
        # Defense in depth: the slug alphabet cannot escape, and this proves it.
        if not path.is_relative_to(self._namespaces_dir):
            raise StoreError(400, "namespace name does not resolve to a safe directory")

        response = await self._client.post(
            "/v2/projects/",
            json={"name": directory_name, "path": str(path), "set_default": False},
        )
        if response.status_code not in (200, 201):
            raise StoreError(502, f"could not create the namespace project: {response.text}")

        created = response.json()["new_project"]
        logger.info("Adapter created namespace", f"id={created['external_id']}")
        return Namespace(id=created["external_id"], name=created["name"], path=path)

    async def resolve_namespace(self, namespace_id: str) -> Namespace:
        """Look up a namespace by id, rejecting anything the adapter does not own."""
        if not _UUID_PATTERN.fullmatch(namespace_id):
            raise NamespaceNotFound(namespace_id)

        response = await self._client.get(f"/v2/projects/{namespace_id}")
        if response.status_code == 404:
            raise NamespaceNotFound(namespace_id)
        self._raise_for_status(response)

        item = response.json()
        path = Path(item["path"]).resolve()
        # Trigger: an id that resolves to a project outside the namespaces root
        # (the bootstrap project, or anything a future operator adds).
        # Why: adapter routes must never read or delete non-namespace state.
        # Outcome: such ids are indistinguishable from unknown ones.
        if not path.is_relative_to(self._namespaces_dir):
            raise NamespaceNotFound(namespace_id)

        return Namespace(id=item["external_id"], name=item["name"], path=path)

    async def delete_namespace(self, namespace_id: str) -> bool:
        """Delete one namespace and its notes. Returns False if it was already gone."""
        try:
            namespace = await self.resolve_namespace(namespace_id)
        except NamespaceNotFound:
            return False

        response = await self._client.delete(
            f"/v2/projects/{namespace.id}", params={"delete_notes": True}
        )
        if response.status_code == 404:
            return False
        self._raise_for_status(response)

        # Basic Memory removes the project directory with delete_notes=True;
        # clear any residue so a repeated reset always lands on a clean tree.
        shutil.rmtree(namespace.path, ignore_errors=True)
        logger.info("Adapter deleted namespace", f"id={namespace.id}")
        return True

    # --- Memories ---

    async def store_memory(
        self,
        namespace: Namespace,
        content: str,
        title: str | None,
        metadata: dict[str, Any] | None,
    ) -> str:
        """Write a real Basic Memory note and return its stable external id.

        Each memory gets its own directory under ``memories/``. Basic Memory
        derives a note's filename from its title, so a per-memory directory is
        what keeps two memories that share a title from colliding — without
        rewriting either title.
        """
        payload: dict[str, Any] = {
            "title": title or _derive_title(content),
            "content": content,
            "directory": f"{_MEMORY_ROOT_DIRECTORY}/{uuid4().hex}",
        }
        if metadata:
            payload["entity_metadata"] = metadata

        response = await self._client.post(
            f"/v2/projects/{namespace.id}/knowledge/entities", json=payload
        )
        self._raise_for_status(response)
        return response.json()["external_id"]

    async def update_memory(
        self,
        namespace: Namespace,
        memory_id: str,
        content: str,
        metadata: dict[str, Any] | None,
    ) -> str:
        """Replace an existing note's content in place.

        The note keeps its id, title and file path, so an update rewrites the
        one note rather than adding a second one next to it.
        """
        existing = await self._get_entity(namespace, memory_id)

        directory = str(PurePosixPath(existing["file_path"]).parent)
        payload: dict[str, Any] = {
            "title": existing["title"],
            "content": content,
            "directory": "" if directory == "." else directory,
        }
        if metadata is not None:
            payload["entity_metadata"] = metadata

        response = await self._client.put(
            f"/v2/projects/{namespace.id}/knowledge/entities/{memory_id}", json=payload
        )
        if response.status_code == 404:
            raise MemoryNotFound(memory_id)
        self._raise_for_status(response)
        return response.json()["external_id"]

    async def recall(self, namespace: Namespace, query: str, limit: int) -> list[Memory]:
        """Search the namespace and return the best matching notes."""
        # A note can match several times (its own row plus its observations and
        # relations), so ask for more rows than requested and dedupe by note.
        page_size = min(max(limit * 3, limit), _MAX_SEARCH_PAGE_SIZE)
        response = await self._client.post(
            f"/v2/projects/{namespace.id}/search/",
            json={"text": query},
            params={"page": 1, "page_size": page_size},
        )
        self._raise_for_status(response)

        ordered_ids: list[str] = []
        for result in response.json()["results"]:
            external_id = result.get("external_id")
            # Rows without an external id cannot be addressed by a later
            # update or recall, so they are not answers this contract can give.
            if external_id and external_id not in ordered_ids:
                ordered_ids.append(external_id)
            if len(ordered_ids) == limit:
                break

        memories: list[Memory] = []
        for external_id in ordered_ids:
            entity = await self._get_entity(namespace, external_id)
            memories.append(
                Memory(
                    id=entity["external_id"],
                    content=_note_body(entity.get("content") or ""),
                    metadata=entity.get("entity_metadata"),
                )
            )
        return memories

    # --- Internals ---

    async def _get_entity(self, namespace: Namespace, memory_id: str) -> dict[str, Any]:
        """Fetch one note, treating an unusable id as a missing memory."""
        if not _UUID_PATTERN.fullmatch(memory_id):
            raise MemoryNotFound(memory_id)

        response = await self._client.get(
            f"/v2/projects/{namespace.id}/knowledge/entities/{memory_id}"
        )
        if response.status_code == 404:
            raise MemoryNotFound(memory_id)
        self._raise_for_status(response)
        entity: dict[str, Any] = response.json()
        return entity

    @staticmethod
    def _raise_for_status(response: Response) -> None:
        """Surface a Basic Memory failure without inventing a fallback result."""
        if response.is_success:
            return
        detail = response.text[:500]
        # A rejected request is the caller's; anything else is ours to report.
        status_code = response.status_code if response.status_code < 500 else 502
        raise StoreError(status_code, detail)
