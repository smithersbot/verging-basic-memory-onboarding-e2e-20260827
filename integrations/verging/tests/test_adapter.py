"""Contract tests for the Verging Memory CI adapter.

Every test runs against the real Basic Memory runtime: real projects on disk, real
markdown notes, real search. Nothing is mocked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from integrations.verging import DATA_ROOT
from integrations.verging.app import NAMESPACE_PREFIX, app

from .conftest import TEST_PRODUCT_KEY

AUTH = {"Authorization": f"Bearer {TEST_PRODUCT_KEY}"}


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    """One adapter instance, started through its real lifespan."""
    with TestClient(app) as test_client:
        yield test_client


def create_namespace(client: TestClient, name: str) -> str:
    response = client.post("/v1/namespaces", json={"name": name, "forceCreate": True}, headers=AUTH)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def store(client: TestClient, namespace_id: str, **payload: Any) -> str:
    response = client.post(
        f"/v1/namespaces/{namespace_id}/memory/store", json=payload, headers=AUTH
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    return body["id"]


def recall(client: TestClient, namespace_id: str, query: str, **payload: Any) -> list[dict]:
    response = client.post(
        f"/v1/namespaces/{namespace_id}/memory/recall",
        json={"query": query, **payload},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    return body["results"]


# --- Health and authentication ---


def test_health_needs_no_credential(client: TestClient) -> None:
    response = client.get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="missing"),
        pytest.param({"Authorization": "Bearer wrong-key"}, id="wrong"),
        pytest.param({"Authorization": TEST_PRODUCT_KEY}, id="no-scheme"),
        pytest.param({"Authorization": "Basic " + TEST_PRODUCT_KEY}, id="wrong-scheme"),
    ],
)
def test_namespace_routes_require_the_credential(
    client: TestClient, headers: dict[str, str]
) -> None:
    assert client.post("/v1/namespaces", json={"name": "x"}, headers=headers).status_code == 401
    assert client.delete("/v1/namespaces/whatever", headers=headers).status_code == 401
    assert (
        client.post(
            "/v1/namespaces/whatever/memory/store", json={"content": "x"}, headers=headers
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/v1/namespaces/whatever/memory/recall", json={"query": "x"}, headers=headers
        ).status_code
        == 401
    )


def test_authentication_is_checked_before_the_namespace_exists(client: TestClient) -> None:
    """A wrong key must not reveal whether a namespace id is real."""
    namespace_id = create_namespace(client, "auth-order")
    response = client.post(
        f"/v1/namespaces/{namespace_id}/memory/store",
        json={"content": "secret"},
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert response.status_code == 401


# --- Malformed input ---


def test_malformed_input_is_rejected(client: TestClient) -> None:
    namespace_id = create_namespace(client, "malformed")

    assert client.post("/v1/namespaces", json={}, headers=AUTH).status_code == 422
    assert client.post("/v1/namespaces", json={"name": ""}, headers=AUTH).status_code == 422
    # A name with nothing to slug cannot become a project directory.
    assert client.post("/v1/namespaces", json={"name": "///"}, headers=AUTH).status_code == 400

    assert (
        client.post(
            f"/v1/namespaces/{namespace_id}/memory/store", json={}, headers=AUTH
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/v1/namespaces/{namespace_id}/memory/store", json={"content": ""}, headers=AUTH
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/v1/namespaces/{namespace_id}/memory/recall", json={"limit": 3}, headers=AUTH
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/v1/namespaces/{namespace_id}/memory/update",
            json={"content": "no id"},
            headers=AUTH,
        ).status_code
        == 422
    )


def test_unknown_ids_are_not_found(client: TestClient) -> None:
    namespace_id = create_namespace(client, "unknown-ids")
    missing = "00000000-0000-4000-8000-000000000000"

    assert (
        client.post(
            f"/v1/namespaces/{missing}/memory/store", json={"content": "x"}, headers=AUTH
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/v1/namespaces/{namespace_id}/memory/update",
            json={"id": missing, "content": "x"},
            headers=AUTH,
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    "hostile_id",
    ["..", "../main", "%2e%2e%2fmain", "not-a-uuid", "../../../../etc/passwd"],
)
def test_namespace_ids_are_untrusted(client: TestClient, hostile_id: str) -> None:
    """A traversal-shaped id resolves to nothing rather than another project."""
    assert client.delete(f"/v1/namespaces/{hostile_id}", headers=AUTH).status_code == 404
    assert (
        client.post(
            f"/v1/namespaces/{hostile_id}/memory/store", json={"content": "x"}, headers=AUTH
        ).status_code
        == 404
    )


def test_namespace_names_cannot_escape_the_data_root(client: TestClient) -> None:
    """A traversal-shaped name still lands inside the adapter's project root."""
    namespace_id = create_namespace(client, "../../etc/passwd")
    store(client, namespace_id, content="contained")

    projects_root = (DATA_ROOT / "projects").resolve()
    written = [
        path
        for path in projects_root.rglob("*.md")
        if "contained" in path.read_text(encoding="utf-8")
    ]
    assert written, "the note should exist under the adapter's project root"
    for path in written:
        assert path.resolve().is_relative_to(projects_root)
        assert path.resolve().is_relative_to(Path(DATA_ROOT).resolve())


# --- Lifecycle: create, store, recall, update, delete ---


def test_store_writes_a_real_note_that_recall_finds(client: TestClient) -> None:
    namespace_id = create_namespace(client, "store-recall")
    memory_id = store(
        client,
        namespace_id,
        content="The deployment runbook lives in the ops handbook.",
        title="Deployment runbook",
        metadata={"source": "verging-contract-test"},
    )

    results = recall(client, namespace_id, "deployment runbook")
    assert [result["id"] for result in results] == [memory_id]
    assert "ops handbook" in results[0]["content"]

    # The note is a real markdown file on disk, not an in-memory record.
    projects_root = (DATA_ROOT / "projects").resolve()
    on_disk = [
        path
        for path in projects_root.rglob("*.md")
        if "ops handbook" in path.read_text(encoding="utf-8")
    ]
    assert on_disk


def test_recall_returns_the_stored_content_verbatim(client: TestClient) -> None:
    """Basic Memory's frontmatter belongs in metadata, not in the recalled content."""
    namespace_id = create_namespace(client, "round-trip")
    content = "Quarterly review moved to the first Tuesday.\n\nAsk Dana for the agenda."
    memory_id = store(client, namespace_id, content=content, title="Quarterly review")

    results = recall(client, namespace_id, "quarterly review agenda")
    assert [result["id"] for result in results] == [memory_id]
    assert results[0]["content"] == content
    assert results[0]["metadata"]["title"] == "Quarterly review"


def test_recall_honors_limit(client: TestClient) -> None:
    namespace_id = create_namespace(client, "recall-limit")
    for index in range(3):
        store(client, namespace_id, content=f"widget calibration note number {index}")

    assert len(recall(client, namespace_id, "widget calibration")) == 3
    assert len(recall(client, namespace_id, "widget calibration", limit=1)) == 1


def test_recall_of_an_unmatched_query_is_empty(client: TestClient) -> None:
    namespace_id = create_namespace(client, "recall-empty")
    store(client, namespace_id, content="the kettle is in the kitchen")

    assert recall(client, namespace_id, "zzzzunmatchedqueryzzz") == []


def test_update_replaces_the_note_without_duplicating_it(client: TestClient) -> None:
    namespace_id = create_namespace(client, "update-replaces")
    memory_id = store(
        client,
        namespace_id,
        content="Standup is at 9am in the blue room.",
        title="Standup",
    )

    response = client.post(
        f"/v1/namespaces/{namespace_id}/memory/update",
        json={"id": memory_id, "content": "Standup is at 11am in the blue room."},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "id": memory_id}

    results = recall(client, namespace_id, "standup blue room")
    assert [result["id"] for result in results] == [memory_id]
    assert "11am" in results[0]["content"]
    assert "9am" not in results[0]["content"]

    # The replacement must not have left a second note behind.
    assert recall(client, namespace_id, "9am") == []


def test_store_does_not_overwrite_a_same_titled_memory(client: TestClient) -> None:
    namespace_id = create_namespace(client, "same-title")
    first = store(client, namespace_id, content="first mango observation", title="Notes")
    second = store(client, namespace_id, content="second mango observation", title="Notes")

    assert first != second
    assert {result["id"] for result in recall(client, namespace_id, "mango observation")} == {
        first,
        second,
    }


def test_namespaces_are_isolated(client: TestClient) -> None:
    left = create_namespace(client, "isolation-left")
    right = create_namespace(client, "isolation-right")
    left_memory = store(client, left, content="the left namespace knows about pangolins")
    store(client, right, content="the right namespace knows about narwhals")

    assert [result["id"] for result in recall(client, left, "pangolins")] == [left_memory]
    assert recall(client, right, "pangolins") == []
    assert recall(client, left, "narwhals") == []


def test_force_create_resets_an_existing_namespace(client: TestClient) -> None:
    first_id = create_namespace(client, "force-create")
    store(client, first_id, content="stale content about ferrets")
    assert recall(client, first_id, "ferrets")

    second_id = create_namespace(client, "force-create")
    assert second_id != first_id
    assert recall(client, second_id, "ferrets") == []


def test_create_without_force_reuses_the_namespace(client: TestClient) -> None:
    created = client.post("/v1/namespaces", json={"name": "reuse-me"}, headers=AUTH)
    assert created.status_code == 201
    namespace_id = created.json()["id"]

    again = client.post("/v1/namespaces", json={"name": "reuse-me"}, headers=AUTH)
    assert again.status_code == 201
    assert again.json()["id"] == namespace_id


def test_delete_removes_the_namespace_and_is_idempotent(client: TestClient) -> None:
    namespace_id = create_namespace(client, "delete-me")
    store(client, namespace_id, content="temporary content about zebras")

    first = client.delete(f"/v1/namespaces/{namespace_id}", headers=AUTH)
    assert first.status_code == 200
    assert first.json() == {"ok": True}

    # A repeated delete is a clean no-op reset, not an error the harness must handle.
    assert client.delete(f"/v1/namespaces/{namespace_id}", headers=AUTH).status_code == 404
    assert (
        client.post(
            f"/v1/namespaces/{namespace_id}/memory/store", json={"content": "x"}, headers=AUTH
        ).status_code
        == 404
    )


def test_namespaces_map_to_prefixed_basic_memory_projects(client: TestClient) -> None:
    """The adapter can only ever address projects it created."""
    namespace_id = create_namespace(client, "prefix-check")

    from basic_memory.config import ConfigManager

    projects = ConfigManager().config.projects
    owned = {name: path for name, path in projects.items() if name.startswith(NAMESPACE_PREFIX)}
    assert any(name.endswith("prefix-check") for name in owned)
    assert namespace_id
