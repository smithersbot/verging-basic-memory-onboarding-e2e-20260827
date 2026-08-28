"""Fixtures for the Verging Memory CI adapter tests.

Every test here runs the real adapter against a real Basic Memory instance in
a temporary data directory: real projects, real markdown files, real search.
"""

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(REPO_ROOT))

from verging_adapter.app import create_app  # noqa: E402
from verging_adapter.settings import AdapterSettings  # noqa: E402

API_KEY = "test-scoped-product-key-0123456789"


@pytest.fixture
def settings(tmp_path, monkeypatch) -> AdapterSettings:
    """Point the adapter (and Basic Memory) at a throwaway data directory."""
    data_dir = tmp_path / "adapter-data"
    # Registered through monkeypatch so the value is restored for other tests;
    # create_app() sets the same variable for the process it serves.
    monkeypatch.setenv("BASIC_MEMORY_CONFIG_DIR", str(data_dir / "config"))
    monkeypatch.setenv("VERGING_ADAPTER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VERGING_ADAPTER_API_KEY", API_KEY)
    return AdapterSettings(data_dir=data_dir, api_key=API_KEY)


@pytest.fixture
def client(settings) -> Iterator[TestClient]:
    """A started adapter: Basic Memory's lifespan has run and is serving."""
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_KEY}"}


@pytest.fixture
def namespace(client, auth) -> str:
    response = client.post(
        "/v1/namespaces", json={"name": "verging suite", "forceCreate": True}, headers=auth
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]
