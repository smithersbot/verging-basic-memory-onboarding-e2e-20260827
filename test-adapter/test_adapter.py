"""End-to-end tests of the adapter contract against real Basic Memory."""

from pathlib import Path

import pytest

from verging_adapter.settings import AdapterSettings

# --- Health and authentication ---


def test_health_needs_no_credential(client):
    response = client.get("/v1/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="missing"),
        pytest.param({"Authorization": "Bearer wrong-key-wrong-key-wrong"}, id="wrong-key"),
        pytest.param(
            {"Authorization": "Basic test-scoped-product-key-0123456789"}, id="wrong-scheme"
        ),
    ],
)
def test_every_memory_route_requires_the_bearer_credential(client, headers, namespace):
    routes = [
        ("post", "/v1/namespaces", {"name": "x", "forceCreate": True}),
        ("post", f"/v1/namespaces/{namespace}/memory/store", {"content": "x"}),
        ("post", f"/v1/namespaces/{namespace}/memory/update", {"id": "x", "content": "y"}),
        ("post", f"/v1/namespaces/{namespace}/memory/recall", {"query": "x"}),
        ("delete", f"/v1/namespaces/{namespace}", None),
    ]

    for method, url, body in routes:
        response = client.request(method.upper(), url, json=body, headers=headers)
        assert response.status_code == 401, f"{method} {url} -> {response.status_code}"
        assert response.json()["ok"] is False


def test_a_wrong_credential_never_reaches_the_store(client, namespace, auth, settings):
    client.post(
        f"/v1/namespaces/{namespace}/memory/store",
        json={"content": "Ada prefers dark roast coffee"},
        headers=auth,
    )

    recalled = client.post(
        f"/v1/namespaces/{namespace}/memory/recall",
        json={"query": "coffee"},
        headers={"Authorization": "Bearer not-the-key-not-the-key"},
    )

    assert recalled.status_code == 401
    assert "results" not in recalled.json()


# --- Namespaces ---


def test_create_namespace_makes_a_real_basic_memory_project(client, auth, settings):
    response = client.post(
        "/v1/namespaces", json={"name": "Verging Suite 01", "forceCreate": True}, headers=auth
    )

    assert response.status_code == 201
    namespace_id = response.json()["id"]
    assert namespace_id

    # The namespace is a directory of its own under the adapter's data root,
    # which lives outside the source checkout.
    directories = list(settings.namespaces_dir.iterdir())
    assert len(directories) == 1
    assert directories[0].name.startswith("verging-suite-01-")
    assert not settings.data_dir.is_relative_to(Path(__file__).resolve().parent.parent)


def test_repeated_creates_with_one_name_stay_separate(client, auth):
    first = client.post(
        "/v1/namespaces", json={"name": "same name", "forceCreate": True}, headers=auth
    ).json()["id"]
    second = client.post(
        "/v1/namespaces", json={"name": "same name", "forceCreate": True}, headers=auth
    ).json()["id"]

    assert first != second


@pytest.mark.parametrize(
    "namespace_id",
    ["../../etc", "..%2f..%2fetc", "not-a-uuid", "00000000-0000-4000-8000-000000000000"],
)
def test_untrusted_namespace_ids_are_not_found(client, auth, namespace_id):
    response = client.post(
        f"/v1/namespaces/{namespace_id}/memory/store", json={"content": "x"}, headers=auth
    )

    assert response.status_code == 404


def test_the_bootstrap_project_holds_no_namespace_data(client, auth, namespace, settings):
    """The default project exists only so namespaces are never the default."""
    client.post(
        f"/v1/namespaces/{namespace}/memory/store",
        json={"content": "Ada prefers dark roast coffee"},
        headers=auth,
    )

    assert settings.bootstrap_dir.exists()
    assert not any(settings.bootstrap_dir.rglob("*.md"))


# --- Store and recall ---


def test_store_writes_a_markdown_note_and_recall_finds_it(client, auth, namespace, settings):
    stored = client.post(
        f"/v1/namespaces/{namespace}/memory/store",
        json={
            "title": "Coffee preference",
            "content": "Ada prefers dark roast coffee brewed as a pour over.",
            "metadata": {"source": "onboarding"},
        },
        headers=auth,
    )

    assert stored.status_code == 200
    body = stored.json()
    assert body["ok"] is True
    memory_id = body["id"]

    markdown_files = list(settings.namespaces_dir.rglob("*.md"))
    assert any("dark roast" in path.read_text() for path in markdown_files)

    recalled = client.post(
        f"/v1/namespaces/{namespace}/memory/recall",
        json={"query": "dark roast coffee", "limit": 5},
        headers=auth,
    )

    assert recalled.status_code == 200
    results = recalled.json()["results"]
    assert [result["id"] for result in results] == [memory_id]
    # Recall answers with the memory as it was stored: the note body, not the
    # markdown file's frontmatter, which comes back as metadata instead.
    assert results[0]["content"] == "Ada prefers dark roast coffee brewed as a pour over."
    assert results[0]["metadata"]["source"] == "onboarding"


def test_two_memories_sharing_a_title_both_survive(client, auth, namespace):
    first = client.post(
        f"/v1/namespaces/{namespace}/memory/store",
        json={"title": "Preferences", "content": "Ada takes notes in Markdown."},
        headers=auth,
    ).json()["id"]
    second = client.post(
        f"/v1/namespaces/{namespace}/memory/store",
        json={"title": "Preferences", "content": "Ada reviews pull requests on Fridays."},
        headers=auth,
    ).json()["id"]

    assert first != second

    recalled = client.post(
        f"/v1/namespaces/{namespace}/memory/recall",
        json={"query": "Ada", "limit": 10},
        headers=auth,
    ).json()["results"]

    assert {first, second} <= {result["id"] for result in recalled}


def test_recall_honors_limit(client, auth, namespace):
    for index in range(4):
        client.post(
            f"/v1/namespaces/{namespace}/memory/store",
            json={"content": f"Deployment note number {index} about the staging release"},
            headers=auth,
        )

    recalled = client.post(
        f"/v1/namespaces/{namespace}/memory/recall",
        json={"query": "staging release", "limit": 2},
        headers=auth,
    ).json()["results"]

    assert len(recalled) == 2


def test_recall_of_an_unknown_topic_is_empty(client, auth, namespace):
    client.post(
        f"/v1/namespaces/{namespace}/memory/store",
        json={"content": "Ada prefers dark roast coffee"},
        headers=auth,
    )

    recalled = client.post(
        f"/v1/namespaces/{namespace}/memory/recall",
        json={"query": "submarine navigation"},
        headers=auth,
    ).json()

    assert recalled == {"ok": True, "results": []}


# --- Update ---


def test_update_replaces_the_note_without_duplicating_it(client, auth, namespace):
    memory_id = client.post(
        f"/v1/namespaces/{namespace}/memory/store",
        json={"title": "Roast preference", "content": "Ada prefers dark roast coffee."},
        headers=auth,
    ).json()["id"]

    updated = client.post(
        f"/v1/namespaces/{namespace}/memory/update",
        json={"id": memory_id, "content": "Ada now prefers light roast coffee."},
        headers=auth,
    )

    assert updated.status_code == 200
    assert updated.json() == {"ok": True, "id": memory_id}

    recalled = client.post(
        f"/v1/namespaces/{namespace}/memory/recall",
        json={"query": "roast coffee", "limit": 10},
        headers=auth,
    ).json()["results"]

    assert [result["id"] for result in recalled] == [memory_id]
    assert "light roast" in recalled[0]["content"]
    assert "dark roast" not in recalled[0]["content"]


def test_update_of_an_unknown_memory_is_not_found(client, auth, namespace):
    response = client.post(
        f"/v1/namespaces/{namespace}/memory/update",
        json={"id": "00000000-0000-4000-8000-000000000000", "content": "x"},
        headers=auth,
    )

    assert response.status_code == 404


def test_a_memory_cannot_be_updated_through_another_namespace(client, auth, namespace):
    memory_id = client.post(
        f"/v1/namespaces/{namespace}/memory/store",
        json={"content": "Ada prefers dark roast coffee"},
        headers=auth,
    ).json()["id"]
    other = client.post(
        "/v1/namespaces", json={"name": "other", "forceCreate": True}, headers=auth
    ).json()["id"]

    response = client.post(
        f"/v1/namespaces/{other}/memory/update",
        json={"id": memory_id, "content": "hijacked"},
        headers=auth,
    )

    assert response.status_code == 404


# --- Isolation and deletion ---


def test_namespaces_are_isolated(client, auth):
    first = client.post(
        "/v1/namespaces", json={"name": "tenant a", "forceCreate": True}, headers=auth
    ).json()["id"]
    second = client.post(
        "/v1/namespaces", json={"name": "tenant b", "forceCreate": True}, headers=auth
    ).json()["id"]

    client.post(
        f"/v1/namespaces/{first}/memory/store",
        json={"content": "Tenant A keeps its deployment runbook here"},
        headers=auth,
    )

    from_first = client.post(
        f"/v1/namespaces/{first}/memory/recall", json={"query": "runbook"}, headers=auth
    ).json()["results"]
    from_second = client.post(
        f"/v1/namespaces/{second}/memory/recall", json={"query": "runbook"}, headers=auth
    ).json()["results"]

    assert len(from_first) == 1
    assert from_second == []


def test_delete_removes_the_namespace_and_is_safe_to_repeat(client, auth, settings):
    namespace_id = client.post(
        "/v1/namespaces", json={"name": "disposable", "forceCreate": True}, headers=auth
    ).json()["id"]
    client.post(
        f"/v1/namespaces/{namespace_id}/memory/store",
        json={"content": "Data that must not survive the reset"},
        headers=auth,
    )

    first_delete = client.delete(f"/v1/namespaces/{namespace_id}", headers=auth)
    second_delete = client.delete(f"/v1/namespaces/{namespace_id}", headers=auth)

    assert first_delete.status_code == 204
    assert second_delete.status_code in (200, 202, 204, 404)
    assert not any(settings.namespaces_dir.rglob("*.md"))

    gone = client.post(
        f"/v1/namespaces/{namespace_id}/memory/recall", json={"query": "data"}, headers=auth
    )
    assert gone.status_code == 404


def test_deleting_one_namespace_leaves_the_others_alone(client, auth):
    kept = client.post(
        "/v1/namespaces", json={"name": "kept", "forceCreate": True}, headers=auth
    ).json()["id"]
    removed = client.post(
        "/v1/namespaces", json={"name": "removed", "forceCreate": True}, headers=auth
    ).json()["id"]
    client.post(
        f"/v1/namespaces/{kept}/memory/store",
        json={"content": "A memory that must outlive the other namespace"},
        headers=auth,
    )

    assert client.delete(f"/v1/namespaces/{removed}", headers=auth).status_code == 204

    recalled = client.post(
        f"/v1/namespaces/{kept}/memory/recall", json={"query": "outlive"}, headers=auth
    ).json()["results"]
    assert len(recalled) == 1


# --- Malformed input ---


@pytest.mark.parametrize(
    "url_suffix,body",
    [
        ("memory/store", {}),
        ("memory/store", {"content": ""}),
        ("memory/store", {"content": 17}),
        ("memory/update", {"content": "no id"}),
        ("memory/recall", {"query": ""}),
        ("memory/recall", {"query": "ok", "limit": 0}),
        ("memory/recall", {"query": "ok", "limit": "many"}),
    ],
)
def test_malformed_bodies_are_rejected(client, auth, namespace, url_suffix, body):
    response = client.post(f"/v1/namespaces/{namespace}/{url_suffix}", json=body, headers=auth)

    assert response.status_code == 422
    assert response.json()["ok"] is False


def test_namespace_creation_requires_a_name(client, auth):
    response = client.post("/v1/namespaces", json={"forceCreate": True}, headers=auth)

    assert response.status_code == 422


# --- Settings guardrails ---


def test_settings_refuse_a_data_directory_inside_the_checkout(tmp_path):
    inside = Path(__file__).resolve().parent.parent / "adapter-data"

    with pytest.raises(RuntimeError, match="inside the source checkout"):
        AdapterSettings(data_dir=inside, api_key="a" * 32).prepare()


def test_settings_require_a_credible_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("VERGING_ADAPTER_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("VERGING_ADAPTER_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="VERGING_ADAPTER_API_KEY is not set"):
        AdapterSettings.from_env()

    monkeypatch.setenv("VERGING_ADAPTER_API_KEY", "short")
    with pytest.raises(RuntimeError, match="at least"):
        AdapterSettings.from_env()
