"""Contract tests for the Verging Memory CI adapter.

Every test here runs against the real Basic Memory implementation: the adapter
app enters ``basic_memory``'s own lifespan, so notes are written to real
markdown files, indexed by the real search service, and recalled through the
real v2 API. Nothing is mocked.
"""

from typing import Any

import httpx
import pytest

from adapter import create_app

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def anonymous():
    """A live adapter app with no credential attached."""
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://adapter.test", timeout=120.0
        ) as http:
            yield http


@pytest.fixture
def client(anonymous, api_key):
    """The same app, with the scoped credential attached to every request."""
    anonymous.headers["Authorization"] = f"Bearer {api_key}"
    return anonymous


async def make_namespace(client: httpx.AsyncClient, name: str) -> str:
    response = await client.post("/v1/namespaces", json={"name": name, "forceCreate": True})
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def store(client: httpx.AsyncClient, namespace: str, content: str, **kwargs) -> str:
    response = await client.post(
        f"/v1/namespaces/{namespace}/memory/store", json={"content": content, **kwargs}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    return body["id"]


async def recall(
    client: httpx.AsyncClient, namespace: str, query: str, **kwargs
) -> list[dict[str, Any]]:
    response = await client.post(
        f"/v1/namespaces/{namespace}/memory/recall", json={"query": query, **kwargs}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    return body["results"]


# --- Health and authentication ---


async def test_health_is_public(anonymous):
    unauthenticated = await anonymous.get("/v1/health")
    assert unauthenticated.status_code == 200
    assert unauthenticated.json() == {"ok": True}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-key"},
        {"Authorization": "test-only-adapter-key"},
        {"Authorization": "Basic test-only-adapter-key"},
    ],
    ids=["missing", "wrong", "no-scheme", "wrong-scheme"],
)
async def test_credential_is_required(anonymous, headers):
    response = await anonymous.post(
        "/v1/namespaces", json={"name": "auth", "forceCreate": True}, headers=headers
    )
    assert response.status_code == 401


async def test_credential_is_checked_before_the_body(anonymous):
    """A malformed body must not turn an anonymous request into a 422."""
    response = await anonymous.post("/v1/namespaces", json={"nope": 1})
    assert response.status_code == 401


# --- Malformed input ---


async def test_malformed_namespace_request_is_rejected(client):
    assert (await client.post("/v1/namespaces", json={})).status_code == 422
    assert (await client.post("/v1/namespaces", json={"name": ""})).status_code == 422


async def test_malformed_store_request_is_rejected(client):
    namespace = await make_namespace(client, "malformed-store")
    assert (
        await client.post(f"/v1/namespaces/{namespace}/memory/store", json={})
    ).status_code == 422
    assert (
        await client.post(
            f"/v1/namespaces/{namespace}/memory/recall", json={"query": "x", "limit": 0}
        )
    ).status_code == 422


async def test_namespace_name_without_usable_characters_is_rejected(client):
    response = await client.post("/v1/namespaces", json={"name": "!!!", "forceCreate": True})
    assert response.status_code == 400


# --- Namespace creation and isolation on disk ---


async def test_namespace_creates_a_real_directory(client, namespace_root):
    namespace = await make_namespace(client, "Real Directory")
    assert (namespace_root / "vgn-real-directory").is_dir()
    assert namespace


async def test_traversal_in_namespace_name_stays_under_the_root(client, namespace_root):
    root = namespace_root.resolve()
    namespace = await make_namespace(client, "../../../../etc/passwd")
    await store(client, namespace, "traversal probe", title="Traversal Probe")

    created = [path for path in root.iterdir() if path.is_dir()]
    assert created, "namespace directory was not created under the root"
    for path in created:
        assert root in path.resolve().parents
    assert not (root.parent / "etc").exists()


async def test_unknown_namespace_is_not_found(client):
    missing = "00000000-0000-4000-8000-000000000000"
    response = await client.post(f"/v1/namespaces/{missing}/memory/store", json={"content": "x"})
    assert response.status_code == 404

    malformed = await client.post("/v1/namespaces/not-a-uuid/memory/store", json={"content": "x"})
    assert malformed.status_code == 404


# --- Store and recall ---


async def test_store_then_recall_returns_the_note(client):
    namespace = await make_namespace(client, "store-recall")
    note_id = await store(
        client,
        namespace,
        "The deployment runbook lives in the operations handbook.",
        title="Deployment Runbook",
        metadata={"source": "handbook"},
    )

    results = await recall(client, namespace, "deployment runbook")
    assert [result["id"] for result in results] == [note_id]
    assert "operations handbook" in results[0]["content"]
    assert results[0]["metadata"]["source"] == "handbook"


async def test_recall_returns_the_stored_body_without_frontmatter(client):
    """`content` must round-trip what `store` was given, not the raw file."""
    namespace = await make_namespace(client, "round-trip")
    body = "The pager rotation hands over at 09:00 UTC on Mondays."
    await store(client, namespace, body, title="Pager Rotation", metadata={"team": "platform"})

    result = (await recall(client, namespace, "pager rotation"))[0]
    assert result["content"].strip() == body
    assert "---" not in result["content"]
    # The frontmatter is not lost, it just belongs in the metadata field.
    assert result["metadata"]["team"] == "platform"


async def test_recall_respects_limit(client):
    namespace = await make_namespace(client, "recall-limit")
    for index in range(4):
        await store(client, namespace, f"shared marker term entry {index}", title=f"Entry {index}")

    assert len(await recall(client, namespace, "marker", limit=2)) == 2
    assert len(await recall(client, namespace, "marker")) == 4


async def test_store_without_a_title_does_not_collide(client):
    namespace = await make_namespace(client, "untitled")
    first = await store(client, namespace, "anonymous note about kestrels")
    second = await store(client, namespace, "anonymous note about kestrels")
    assert first != second
    assert len(await recall(client, namespace, "kestrels")) == 2


async def test_repeated_title_does_not_overwrite_the_first_note(client):
    namespace = await make_namespace(client, "same-title")
    first = await store(client, namespace, "first body about otters", title="Shared Title")
    second = await store(client, namespace, "second body about otters", title="Shared Title")

    assert first != second
    bodies = " ".join(result["content"] for result in await recall(client, namespace, "otters"))
    assert "first body" in bodies
    assert "second body" in bodies


# --- Update ---


async def test_update_replaces_content_without_duplicating(client, namespace_root):
    namespace = await make_namespace(client, "update-no-duplicate")
    note_id = await store(client, namespace, "the office plant is a ficus", title="Office Plant")

    response = await client.post(
        f"/v1/namespaces/{namespace}/memory/update",
        json={"id": note_id, "content": "the office plant is a monstera"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "id": note_id}

    results = await recall(client, namespace, "office plant")
    assert [result["id"] for result in results] == [note_id]
    assert "monstera" in results[0]["content"]
    assert "ficus" not in results[0]["content"]

    files = list((namespace_root / "vgn-update-no-duplicate").rglob("*.md"))
    assert len(files) == 1, files


async def test_update_of_unknown_note_is_not_found(client):
    namespace = await make_namespace(client, "update-missing")
    response = await client.post(
        f"/v1/namespaces/{namespace}/memory/update",
        json={"id": "00000000-0000-4000-8000-000000000000", "content": "x"},
    )
    assert response.status_code == 404


# --- Cross-namespace isolation ---


async def test_namespaces_do_not_see_each_other(client):
    left = await make_namespace(client, "isolation-left")
    right = await make_namespace(client, "isolation-right")

    secret_id = await store(
        client, left, "the vault combination is quinoa", title="Vault Combination"
    )
    await store(client, right, "unrelated note about bicycles", title="Bicycles")

    assert await recall(client, right, "quinoa") == []
    assert [result["id"] for result in await recall(client, left, "quinoa")] == [secret_id]

    leaked = await client.post(
        f"/v1/namespaces/{right}/memory/update",
        json={"id": secret_id, "content": "overwritten from another namespace"},
    )
    assert leaked.status_code == 404


async def test_force_create_resets_an_existing_namespace(client):
    first = await make_namespace(client, "reset-me")
    await store(client, first, "stale content about penguins", title="Penguins")
    assert await recall(client, first, "penguins")

    second = await make_namespace(client, "reset-me")
    assert await recall(client, second, "penguins") == []


# --- Deletion ---


async def test_delete_is_idempotent_and_removes_the_data(client, namespace_root):
    namespace = await make_namespace(client, "delete-me")
    await store(client, namespace, "ephemeral note about comets", title="Comets")
    directory = namespace_root / "vgn-delete-me"
    assert directory.exists()

    first = await client.delete(f"/v1/namespaces/{namespace}")
    assert first.status_code == 200
    assert first.json()["ok"] is True

    second = await client.delete(f"/v1/namespaces/{namespace}")
    assert second.status_code == 404

    assert (
        await client.post(f"/v1/namespaces/{namespace}/memory/recall", json={"query": "comets"})
    ).status_code == 404
