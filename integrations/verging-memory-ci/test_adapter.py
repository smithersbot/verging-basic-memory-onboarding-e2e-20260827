"""Contract tests for the Verging Memory CI adapter.

Every test runs against the real Basic Memory implementation: a real project,
real markdown files under a temporary data directory, and the real search index.
Nothing here is mocked.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

_DATA_DIR = tempfile.mkdtemp(prefix="verging-adapter-test-")
os.environ["VERGING_ADAPTER_DATA_DIR"] = _DATA_DIR
os.environ["VERGING_PRODUCT_KEY"] = "test-product-key"
sys.path.insert(0, str(Path(__file__).parent))

import adapter  # noqa: E402

AUTH = {"Authorization": f"Bearer {os.environ['VERGING_PRODUCT_KEY']}"}


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with adapter.app.router.lifespan_context(adapter.app):
        async with AsyncClient(
            transport=ASGITransport(app=adapter.app), base_url="http://adapter", timeout=120.0
        ) as http_client:
            yield http_client


async def _namespace(client: AsyncClient, name: str) -> str:
    response = await client.post(
        "/v1/namespaces", json={"name": name, "forceCreate": True}, headers=AUTH
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def _store(client: AsyncClient, ns: str, **payload) -> str:
    response = await client.post(f"/v1/namespaces/{ns}/memory/store", json=payload, headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    return body["id"]


async def _recall(client: AsyncClient, ns: str, query: str, limit: int | None = None) -> list[dict]:
    payload: dict = {"query": query}
    if limit is not None:
        payload["limit"] = limit
    response = await client.post(f"/v1/namespaces/{ns}/memory/recall", json=payload, headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    return body["results"]


@pytest.mark.asyncio
async def test_health_is_public(client: AsyncClient) -> None:
    response = await client.get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong-key"}, {"Authorization": "test-product-key"}],
)
async def test_authentication_is_required(client: AsyncClient, headers: dict) -> None:
    response = await client.post(
        "/v1/namespaces", json={"name": "auth", "forceCreate": True}, headers=headers
    )
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


@pytest.mark.asyncio
async def test_store_and_recall_round_trip(client: AsyncClient) -> None:
    ns = await _namespace(client, "roundtrip")
    note_id = await _store(
        client,
        ns,
        content="The user prefers oat milk flat whites and dislikes drip coffee.",
        title="Coffee preference",
        metadata={"kind": "preference", "confidence": 3, "nested": {"a": True}},
    )

    results = await _recall(client, ns, "What kind of coffee does the user like?")
    assert [result["id"] for result in results] == [note_id]
    assert results[0]["content"] == (
        "The user prefers oat milk flat whites and dislikes drip coffee."
    )
    # Metadata round-trips with its original JSON types, not stringified.
    assert results[0]["metadata"] == {"kind": "preference", "confidence": 3, "nested": {"a": True}}

    await client.delete(f"/v1/namespaces/{ns}", headers=AUTH)


@pytest.mark.asyncio
async def test_recall_honours_limit(client: AsyncClient) -> None:
    ns = await _namespace(client, "limits")
    for index in range(4):
        await _store(client, ns, content=f"Sprint retro note number {index} about latency.")

    assert len(await _recall(client, ns, "latency retro", limit=2)) == 2
    assert len(await _recall(client, ns, "latency retro")) == 4

    await client.delete(f"/v1/namespaces/{ns}", headers=AUTH)


@pytest.mark.asyncio
async def test_update_replaces_without_duplicating(client: AsyncClient) -> None:
    ns = await _namespace(client, "updates")
    note_id = await _store(
        client, ns, content="Deploy with the canary strategy on Tuesdays.", title="Deploy runbook"
    )

    response = await client.post(
        f"/v1/namespaces/{ns}/memory/update",
        json={"id": note_id, "content": "Deploy with the blue-green strategy on Thursdays."},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "id": note_id}

    results = await _recall(client, ns, "deploy strategy")
    assert [result["id"] for result in results] == [note_id]
    assert results[0]["content"] == "Deploy with the blue-green strategy on Thursdays."
    # The superseded content must not linger anywhere in the namespace.
    assert await _recall(client, ns, "canary Tuesdays") == []

    # Exactly one markdown file backs the note on disk.
    project_dir = Path(_DATA_DIR) / "projects" / "vmci-updates"
    assert len(list(project_dir.rglob("*.md"))) == 1

    await client.delete(f"/v1/namespaces/{ns}", headers=AUTH)


@pytest.mark.asyncio
async def test_update_preserves_metadata_unless_replaced(client: AsyncClient) -> None:
    ns = await _namespace(client, "update-metadata")
    note_id = await _store(client, ns, content="Original body.", metadata={"rev": 1})

    await client.post(
        f"/v1/namespaces/{ns}/memory/update",
        json={"id": note_id, "content": "Kept metadata body."},
        headers=AUTH,
    )
    assert (await _recall(client, ns, "kept metadata body"))[0]["metadata"] == {"rev": 1}

    await client.post(
        f"/v1/namespaces/{ns}/memory/update",
        json={"id": note_id, "content": "New metadata body.", "metadata": {"rev": 2}},
        headers=AUTH,
    )
    assert (await _recall(client, ns, "new metadata body"))[0]["metadata"] == {"rev": 2}

    await client.delete(f"/v1/namespaces/{ns}", headers=AUTH)


@pytest.mark.asyncio
async def test_namespaces_are_isolated(client: AsyncClient) -> None:
    first = await _namespace(client, "tenant-one")
    second = await _namespace(client, "tenant-two")
    await _store(client, first, content="Tenant one keeps the pineapple recipe.")
    await _store(client, second, content="Tenant two keeps the artichoke recipe.")

    assert [r["content"] for r in await _recall(client, first, "recipe")] == [
        "Tenant one keeps the pineapple recipe."
    ]
    assert [r["content"] for r in await _recall(client, second, "recipe")] == [
        "Tenant two keeps the artichoke recipe."
    ]

    await client.delete(f"/v1/namespaces/{first}", headers=AUTH)
    await client.delete(f"/v1/namespaces/{second}", headers=AUTH)


@pytest.mark.asyncio
async def test_force_create_resets_a_named_namespace(client: AsyncClient) -> None:
    first = await _namespace(client, "reset-me")
    await _store(client, first, content="Stale content that must not survive.")
    assert await _recall(client, first, "stale content")

    second = await _namespace(client, "reset-me")
    assert await _recall(client, second, "stale content") == []

    await client.delete(f"/v1/namespaces/{second}", headers=AUTH)


@pytest.mark.asyncio
async def test_delete_is_idempotent_and_removes_data(client: AsyncClient) -> None:
    ns = await _namespace(client, "disposable")
    await _store(client, ns, content="Ephemeral note.")

    assert (await client.delete(f"/v1/namespaces/{ns}", headers=AUTH)).status_code == 204
    assert not (Path(_DATA_DIR) / "projects" / "vmci-disposable").exists()

    # Deleting again, and deleting an id that never existed, both succeed.
    assert (await client.delete(f"/v1/namespaces/{ns}", headers=AUTH)).status_code == 204
    assert (
        await client.delete("/v1/namespaces/00000000-0000-4000-8000-000000000000", headers=AUTH)
    ).status_code == 204

    # Operations against the deleted namespace no longer resolve.
    missing = await client.post(
        f"/v1/namespaces/{ns}/memory/store", json={"content": "x"}, headers=AUTH
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "namespace_id",
    ["../../etc", "..%2F..%2Fetc", "not-a-uuid", "vmci-roundtrip"],
)
async def test_untrusted_namespace_ids_are_rejected(client: AsyncClient, namespace_id: str) -> None:
    response = await client.post(
        f"/v1/namespaces/{namespace_id}/memory/recall", json={"query": "anything"}, headers=AUTH
    )
    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["../escape", "bad/name", "", "x" * 200, "?!"])
async def test_untrusted_namespace_names_are_rejected(client: AsyncClient, name: str) -> None:
    response = await client.post(
        "/v1/namespaces", json={"name": name, "forceCreate": True}, headers=AUTH
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_malformed_payloads_are_rejected(client: AsyncClient) -> None:
    ns = await _namespace(client, "malformed")

    assert (await client.post("/v1/namespaces", json={}, headers=AUTH)).status_code == 422
    assert (
        await client.post(
            f"/v1/namespaces/{ns}/memory/store", json={"title": "no content"}, headers=AUTH
        )
    ).status_code == 422
    assert (
        await client.post(f"/v1/namespaces/{ns}/memory/store", json={"content": ""}, headers=AUTH)
    ).status_code == 422
    assert (
        await client.post(f"/v1/namespaces/{ns}/memory/recall", json={}, headers=AUTH)
    ).status_code == 422
    assert (
        await client.post(
            f"/v1/namespaces/{ns}/memory/update", json={"content": "orphan"}, headers=AUTH
        )
    ).status_code == 422
    # A well-formed update for a note that does not exist is a 404, not a create.
    assert (
        await client.post(
            f"/v1/namespaces/{ns}/memory/update",
            json={"id": "00000000-0000-4000-8000-000000000000", "content": "ghost"},
            headers=AUTH,
        )
    ).status_code == 404

    await client.delete(f"/v1/namespaces/{ns}", headers=AUTH)


@pytest.mark.asyncio
async def test_recall_with_no_usable_terms_returns_empty(client: AsyncClient) -> None:
    ns = await _namespace(client, "empty-query")
    await _store(client, ns, content="Some content worth finding.")
    assert await _recall(client, ns, "?? !! ...") == []
    await client.delete(f"/v1/namespaces/{ns}", headers=AUTH)
