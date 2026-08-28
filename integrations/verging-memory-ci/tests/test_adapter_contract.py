"""Contract tests for the Verging Memory CI adapter.

These run against a real Basic Memory instance — real projects, real markdown
files on disk, real search. Nothing here is mocked; a passing run means the
deployed adapter answers the contract with live product behavior.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

KEY = "test-scoped-credential"
AUTH = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    os.environ["VERGING_PRODUCT_KEY"] = KEY
    data_root = tmp_path_factory.mktemp("adapter-data")

    from verging_memory_ci_adapter.app import create_app

    with TestClient(create_app(data_root=data_root)) as test_client:
        yield test_client


@pytest.fixture
def namespace(client):
    response = client.post(
        "/v1/namespaces", json={"name": "suite", "forceCreate": True}, headers=AUTH
    )
    assert response.status_code == 201, response.text
    namespace_id = response.json()["id"]
    yield namespace_id
    client.delete(f"/v1/namespaces/{namespace_id}", headers=AUTH)


def store(client, namespace_id, content, **kwargs):
    response = client.post(
        f"/v1/namespaces/{namespace_id}/memory/store",
        json={"content": content, **kwargs},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    return body["id"]


def recall(client, namespace_id, query, limit=10):
    response = client.post(
        f"/v1/namespaces/{namespace_id}/memory/recall",
        json={"query": query, "limit": limit},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    return body["results"]


# --- Health and authentication ---


def test_health_is_public_and_reports_ok(client):
    response = client.get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong-key"}, {"Authorization": KEY}],
    ids=["missing", "wrong", "no-scheme"],
)
def test_namespace_routes_require_the_credential(client, headers):
    response = client.post(
        "/v1/namespaces", json={"name": "x", "forceCreate": True}, headers=headers
    )
    assert response.status_code == 401
    assert response.json()["ok"] is False


def test_unauthenticated_malformed_body_still_answers_401(client):
    """Authentication is decided before body validation, so 401 wins over 422."""
    response = client.post("/v1/namespaces", json={"nope": 1})
    assert response.status_code == 401


# --- Namespace lifecycle ---


def test_create_namespace_returns_a_stable_isolated_id(client):
    first = client.post(
        "/v1/namespaces", json={"name": "same-name", "forceCreate": True}, headers=AUTH
    )
    second = client.post(
        "/v1/namespaces", json={"name": "same-name", "forceCreate": True}, headers=AUTH
    )
    assert first.status_code == second.status_code == 201
    first_id, second_id = first.json()["id"], second.json()["id"]
    assert uuid.UUID(first_id) != uuid.UUID(second_id)

    client.delete(f"/v1/namespaces/{first_id}", headers=AUTH)
    client.delete(f"/v1/namespaces/{second_id}", headers=AUTH)


def test_delete_namespace_is_idempotent(client, namespace):
    assert client.delete(f"/v1/namespaces/{namespace}", headers=AUTH).status_code == 204
    assert client.delete(f"/v1/namespaces/{namespace}", headers=AUTH).status_code == 204


def test_deleted_namespace_stops_serving_memories(client):
    created = client.post(
        "/v1/namespaces", json={"name": "gone", "forceCreate": True}, headers=AUTH
    ).json()["id"]
    store(client, created, "Ephemeral note about kayaking.")
    assert client.delete(f"/v1/namespaces/{created}", headers=AUTH).status_code == 204

    response = client.post(
        f"/v1/namespaces/{created}/memory/recall", json={"query": "kayaking"}, headers=AUTH
    )
    assert response.status_code == 404


# --- Store and recall ---


def test_store_then_recall_returns_the_real_note(client, namespace):
    memory_id = store(
        client,
        namespace,
        "The user prefers oat milk flat whites and never drinks dairy.",
        title="Coffee Preference",
        metadata={"kind": "preference"},
    )

    results = recall(client, namespace, "oat milk")
    assert [r["id"] for r in results] == [memory_id]
    assert "oat milk flat whites" in results[0]["content"]
    # Frontmatter is Basic Memory's storage detail, not part of the memory.
    assert not results[0]["content"].startswith("---")
    assert results[0]["metadata"]["kind"] == "preference"


def test_recall_honors_limit(client, namespace):
    for index in range(4):
        store(client, namespace, f"Sailing log entry number {index}.", title=f"Sailing {index}")

    assert len(recall(client, namespace, "sailing", limit=2)) == 2


def test_recall_without_matches_returns_no_results(client, namespace):
    store(client, namespace, "Note about gardening tools.")
    assert recall(client, namespace, "quantumchromodynamics") == []


def test_store_twice_with_one_title_keeps_both_memories(client, namespace):
    first = store(client, namespace, "First fact about hiking boots.", title="Gear")
    second = store(client, namespace, "Second fact about rain jackets.", title="Gear")
    assert first != second

    assert {r["id"] for r in recall(client, namespace, "hiking OR jackets")} == {first, second}


# --- Update ---


def test_update_replaces_content_without_duplicating(client, namespace):
    memory_id = store(client, namespace, "The user commutes by car every morning.", title="Commute")

    response = client.post(
        f"/v1/namespaces/{namespace}/memory/update",
        json={"id": memory_id, "content": "The user now commutes by bicycle every morning."},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "id": memory_id}

    results = recall(client, namespace, "commutes")
    assert [r["id"] for r in results] == [memory_id]
    assert "bicycle" in results[0]["content"]
    assert "by car" not in results[0]["content"]


def test_update_can_replace_metadata(client, namespace):
    memory_id = store(
        client, namespace, "Deadline is Friday.", title="Deadline", metadata={"status": "open"}
    )
    client.post(
        f"/v1/namespaces/{namespace}/memory/update",
        json={
            "id": memory_id,
            "content": "Deadline moved to Monday.",
            "metadata": {"status": "moved"},
        },
        headers=AUTH,
    )

    results = recall(client, namespace, "deadline")
    assert results[0]["metadata"]["status"] == "moved"


def test_update_of_an_unknown_memory_is_not_found(client, namespace):
    response = client.post(
        f"/v1/namespaces/{namespace}/memory/update",
        json={"id": str(uuid.uuid4()), "content": "nothing to replace"},
        headers=AUTH,
    )
    assert response.status_code == 404


# --- Isolation and untrusted input ---


def test_namespaces_cannot_read_each_others_memories(client):
    first = client.post(
        "/v1/namespaces", json={"name": "alpha", "forceCreate": True}, headers=AUTH
    ).json()["id"]
    second = client.post(
        "/v1/namespaces", json={"name": "beta", "forceCreate": True}, headers=AUTH
    ).json()["id"]

    secret_id = store(client, first, "The alpha namespace stores a pineapple secret.")

    assert recall(client, second, "pineapple") == []
    assert [r["id"] for r in recall(client, first, "pineapple")] == [secret_id]

    # An id from one namespace is not addressable through another.
    crossed = client.post(
        f"/v1/namespaces/{second}/memory/update",
        json={"id": secret_id, "content": "overwritten from the wrong namespace"},
        headers=AUTH,
    )
    assert crossed.status_code == 404

    client.delete(f"/v1/namespaces/{first}", headers=AUTH)
    client.delete(f"/v1/namespaces/{second}", headers=AUTH)


@pytest.mark.parametrize(
    "namespace_id",
    ["../../etc", "..%2F..%2Fetc", "not-a-uuid", str(uuid.uuid4())],
    ids=["traversal", "encoded-traversal", "garbage", "well-formed-unknown"],
)
def test_untrusted_namespace_ids_are_rejected(client, namespace_id):
    response = client.post(
        f"/v1/namespaces/{namespace_id}/memory/recall", json={"query": "anything"}, headers=AUTH
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/namespaces", {"forceCreate": True}),
        ("/v1/namespaces", {"name": "", "forceCreate": True}),
        ("/v1/namespaces/{ns}/memory/store", {"title": "no content"}),
        ("/v1/namespaces/{ns}/memory/store", {"content": ""}),
        ("/v1/namespaces/{ns}/memory/update", {"content": "no id"}),
        ("/v1/namespaces/{ns}/memory/recall", {"limit": 5}),
        ("/v1/namespaces/{ns}/memory/recall", {"query": "x", "limit": 0}),
    ],
)
def test_malformed_bodies_are_rejected(client, namespace, path, body):
    response = client.post(path.format(ns=namespace), json=body, headers=AUTH)
    assert response.status_code == 422


def test_stored_note_is_a_real_file_outside_the_checkout(client, namespace, tmp_path_factory):
    """The adapter's storage is real markdown, and it never lands in the repo."""
    from pathlib import Path

    store(client, namespace, "Durable note written to disk.", title="Durable")

    projects_root = Path(os.environ["BASIC_MEMORY_PROJECT_ROOT"])
    notes = list(projects_root.rglob("Durable.md"))
    assert notes, f"no markdown file written under {projects_root}"
    assert "Durable note written to disk." in notes[0].read_text()

    repo_root = Path(__file__).resolve().parents[3]
    assert repo_root not in projects_root.parents and projects_root != repo_root
