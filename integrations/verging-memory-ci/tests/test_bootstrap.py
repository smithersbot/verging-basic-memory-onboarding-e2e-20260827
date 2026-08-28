"""Bootstrap decides where adapter storage lives, so it gets its own tests."""

from __future__ import annotations

import os

import pytest

from verging_memory_ci_adapter.bootstrap import (
    CREDENTIAL_ENV,
    configure_basic_memory_environment,
    read_credential,
    resolve_data_root,
)


def test_data_root_follows_the_single_env_knob(monkeypatch, tmp_path):
    monkeypatch.setenv("VERGING_ADAPTER_DATA_ROOT", str(tmp_path / "elsewhere"))
    assert resolve_data_root() == tmp_path / "elsewhere"


def test_storage_paths_override_the_container_images_values(monkeypatch, tmp_path):
    """The image exports /app paths; inheriting them would store notes in the checkout."""
    monkeypatch.setenv("BASIC_MEMORY_HOME", "/app/data/basic-memory")
    monkeypatch.setenv("BASIC_MEMORY_PROJECT_ROOT", "/app/data")

    root = configure_basic_memory_environment(tmp_path / "data")

    assert root == tmp_path / "data"
    assert os.environ["BASIC_MEMORY_PROJECT_ROOT"] == str(tmp_path / "data" / "projects")
    assert os.environ["BASIC_MEMORY_HOME"] == str(tmp_path / "data" / "home")
    assert os.environ["BASIC_MEMORY_CONFIG_DIR"] == str(tmp_path / "data" / "config")
    assert not os.environ["BASIC_MEMORY_PROJECT_ROOT"].startswith("/app")


def test_behavior_flags_stay_overridable(monkeypatch, tmp_path):
    monkeypatch.setenv("BASIC_MEMORY_SEMANTIC_SEARCH_ENABLED", "true")
    configure_basic_memory_environment(tmp_path / "data")
    assert os.environ["BASIC_MEMORY_SEMANTIC_SEARCH_ENABLED"] == "true"


def test_missing_credential_refuses_to_start(monkeypatch):
    monkeypatch.delenv(CREDENTIAL_ENV, raising=False)
    with pytest.raises(RuntimeError, match=CREDENTIAL_ENV):
        read_credential()


def test_blank_credential_refuses_to_start(monkeypatch):
    monkeypatch.setenv(CREDENTIAL_ENV, "   ")
    with pytest.raises(RuntimeError):
        read_credential()
