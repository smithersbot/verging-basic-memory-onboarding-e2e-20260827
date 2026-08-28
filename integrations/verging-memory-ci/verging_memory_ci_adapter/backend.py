"""Verging Memory CI operations backed by a real Basic Memory instance.

Every operation here is a call into Basic Memory's own v2 API over the
in-process ASGI transport — the same interface the MCP tools use. Nothing is
mocked or reimplemented: a namespace is a real Basic Memory project, a memory is
a real markdown note on disk, and recall is Basic Memory's own search.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from httpx import AsyncClient, Response

# A namespace is a Basic Memory project whose name carries this prefix. The
# prefix is what makes "is this id one of ours?" answerable without a registry:
# an id that resolves to any other project is treated as unknown, so a caller
# cannot reach a project the adapter did not create.
NAMESPACE_PREFIX = "vmci-"

# Notes live in one directory inside the namespace project. Basic Memory derives
# the file path from the directory plus a sanitized title, so no caller-supplied
# string ever becomes a path segment on its own.
MEMORY_DIRECTORY = "memory"

# Frontmatter keys Basic Memory owns. They are stripped from returned metadata
# so a caller sees back what it stored, and stripped before a metadata update so
# an echoed value cannot fight the note's own identity.
INTRINSIC_METADATA_KEYS = frozenset({"title", "type", "permalink"})

_FRONTMATTER = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n?", re.DOTALL)


def is_namespace_id(value: str) -> bool:
    """True when the value is a Basic Memory external UUID, the only id shape we accept."""
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


class BackendError(RuntimeError):
    """A Basic Memory call failed in a way the adapter cannot map to a route result."""


class NamespaceNotFound(LookupError):
    """The namespace id does not name a namespace this adapter created."""


class MemoryNotFound(LookupError):
    """The memory id does not name a note inside the given namespace."""


@dataclass(frozen=True, slots=True)
class Namespace:
    id: str
    name: str
    path: str


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    id: str
    content: str
    metadata: dict[str, Any]


def strip_frontmatter(content: str | None) -> str:
    """Return the note body without the YAML frontmatter Basic Memory writes."""
    if not content:
        return ""
    return _FRONTMATTER.sub("", content, count=1).strip()


def user_metadata(entity_metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Return only the metadata the caller supplied, without Basic Memory's own keys."""
    if not entity_metadata:
        return {}
    return {k: v for k, v in entity_metadata.items() if k not in INTRINSIC_METADATA_KEYS}


def derive_title(content: str, title: str | None) -> str:
    """Pick a note title: the caller's, else the first line, else a generated one."""
    if title and title.strip():
        return title.strip()

    first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
    # Basic Memory sanitizes the title into a filename itself; the cap here only
    # keeps titles readable, it is not a safety boundary.
    candidate = first_line[:80].strip()
    return candidate or f"Memory {uuid.uuid4().hex[:8]}"


class BasicMemoryBackend:
    """Adapter operations expressed as Basic Memory v2 API calls."""

    def __init__(self, http_client: AsyncClient, projects_root: Path) -> None:
        self._http = http_client
        self._projects_root = projects_root

    # --- Plumbing ---

    async def _call(
        self,
        method: str,
        url: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        expected: tuple[int, ...],
    ) -> Response:
        """Issue one Basic Memory API call, failing fast on an unexpected status."""
        response = await self._http.request(method, url, json=json, params=params)
        if response.status_code not in expected:
            raise BackendError(
                f"{method} {url} returned {response.status_code}: {response.text[:500]}"
            )
        return response

    # --- Namespaces ---

    async def create_namespace(self) -> Namespace:
        """Create an isolated Basic Memory project and return its stable id.

        The project name is generated, never derived from caller input: with
        ``BASIC_MEMORY_PROJECT_ROOT`` set, Basic Memory builds the project
        directory from the project name, so a generated name is what keeps a
        namespace's directory inside the data root.
        """
        name = f"{NAMESPACE_PREFIX}{uuid.uuid4().hex}"
        response = await self._call(
            "POST",
            "/v2/projects/",
            json={
                "name": name,
                "path": str(self._projects_root / name),
                "set_default": False,
            },
            expected=(200, 201),
        )
        project = response.json().get("new_project") or {}
        external_id = project.get("external_id")
        if not external_id:
            raise BackendError("Basic Memory did not return an external_id for the new project")
        return Namespace(
            id=external_id, name=project.get("name", name), path=project.get("path", "")
        )

    async def resolve_namespace(self, namespace_id: str) -> Namespace:
        """Resolve a namespace id, rejecting ids that are not adapter-owned projects."""
        # Trigger: an id that is not a Basic Memory external UUID.
        # Why: namespace ids arrive from the network and are interpolated into an
        # API path; rejecting anything but a UUID keeps traversal and injection
        # attempts from reaching Basic Memory at all.
        # Outcome: treated as an unknown namespace, like any other bad id.
        if not is_namespace_id(namespace_id):
            raise NamespaceNotFound(namespace_id)

        response = await self._http.get(f"/v2/projects/{namespace_id}")
        if response.status_code == 404:
            raise NamespaceNotFound(namespace_id)
        if response.status_code != 200:
            raise BackendError(
                f"GET /v2/projects/{{id}} returned {response.status_code}: {response.text[:500]}"
            )

        project = response.json()
        name = project.get("name", "")
        # Trigger: the id resolves to a project the adapter did not create.
        # Why: namespace ids are the only capability a caller holds; without this
        # check a caller could read or delete any project on the deployment.
        # Outcome: indistinguishable from an unknown namespace.
        if not name.startswith(NAMESPACE_PREFIX):
            raise NamespaceNotFound(namespace_id)
        return Namespace(id=project["external_id"], name=name, path=project.get("path", ""))

    async def delete_namespace(self, namespace_id: str) -> bool:
        """Delete a namespace and its notes. Returns False if it was already gone."""
        try:
            namespace = await self.resolve_namespace(namespace_id)
        except NamespaceNotFound:
            return False

        await self._call(
            "DELETE",
            f"/v2/projects/{namespace.id}",
            params={"delete_notes": "true"},
            expected=(200, 204),
        )
        return True

    # --- Memories ---

    async def store(
        self,
        namespace_id: str,
        *,
        content: str,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Write a real markdown note into the namespace and return its stable id."""
        namespace = await self.resolve_namespace(namespace_id)
        chosen_title = derive_title(content, title)

        payload = {
            "title": chosen_title,
            "content": content,
            "directory": MEMORY_DIRECTORY,
            "entity_metadata": user_metadata(metadata) or None,
        }
        response = await self._http.post(
            f"/v2/projects/{namespace.id}/knowledge/entities", json=payload
        )

        # Trigger: a note with this title already exists in the namespace.
        # Why: store must add a memory, never fail because an earlier memory
        # happened to start with the same line; update is the route that replaces.
        # Outcome: retried once under a disambiguated title.
        if response.status_code == 409:
            payload["title"] = f"{chosen_title} {uuid.uuid4().hex[:8]}"
            response = await self._http.post(
                f"/v2/projects/{namespace.id}/knowledge/entities", json=payload
            )

        if response.status_code not in (200, 201, 202):
            raise BackendError(f"store failed with {response.status_code}: {response.text[:500]}")
        external_id = response.json().get("external_id")
        if not external_id:
            raise BackendError("Basic Memory did not return an external_id for the new note")
        return external_id

    async def update(
        self,
        namespace_id: str,
        *,
        memory_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Replace a note's content in place, keeping its id, title and file."""
        namespace = await self.resolve_namespace(namespace_id)
        if not is_namespace_id(memory_id):
            raise MemoryNotFound(memory_id)
        existing = await self._get_entity(namespace.id, memory_id)

        # Reusing the stored title and directory is what makes this a replacement:
        # Basic Memory derives the file path from them, so the same note is
        # rewritten instead of a second note appearing beside it.
        directory = existing["file_path"].rsplit("/", 1)[0] if "/" in existing["file_path"] else ""
        preserved = user_metadata(existing.get("entity_metadata"))
        payload = {
            "title": existing["title"],
            "content": content,
            "directory": directory,
            "entity_metadata": (user_metadata(metadata) if metadata is not None else preserved)
            or None,
        }
        response = await self._call(
            "PUT",
            f"/v2/projects/{namespace.id}/knowledge/entities/{memory_id}",
            json=payload,
            expected=(200, 201, 202),
        )
        return response.json().get("external_id", memory_id)

    async def recall(self, namespace_id: str, *, query: str, limit: int) -> list[MemoryRecord]:
        """Return the namespace's best matches for the query, best first."""
        namespace = await self.resolve_namespace(namespace_id)
        response = await self._call(
            "POST",
            f"/v2/projects/{namespace.id}/search/",
            json={"text": query, "entity_types": ["entity"]},
            params={"page": 1, "page_size": limit},
            expected=(200,),
        )

        records: list[MemoryRecord] = []
        seen: set[str] = set()
        for result in response.json().get("results", []):
            external_id = result.get("external_id")
            if not external_id or external_id in seen:
                continue
            seen.add(external_id)
            try:
                entity = await self._get_entity(namespace.id, external_id)
            except MemoryNotFound:  # pragma: no cover - index lags a deletion
                continue
            records.append(
                MemoryRecord(
                    id=external_id,
                    content=strip_frontmatter(entity.get("content")),
                    metadata=user_metadata(entity.get("entity_metadata")),
                )
            )
            if len(records) >= limit:
                break
        return records

    async def _get_entity(self, project_id: str, memory_id: str) -> dict[str, Any]:
        """Read one note, mapping a miss to MemoryNotFound instead of a 500."""
        response = await self._http.get(f"/v2/projects/{project_id}/knowledge/entities/{memory_id}")
        if response.status_code == 404:
            raise MemoryNotFound(memory_id)
        if response.status_code != 200:
            raise BackendError(f"GET entity returned {response.status_code}: {response.text[:500]}")
        return response.json()
