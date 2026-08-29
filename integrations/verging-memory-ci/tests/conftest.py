"""Isolated environment for the Verging adapter tests.

These env vars must be set before ``adapter`` (and through it ``basic_memory``)
is imported, so they are assigned at conftest import time rather than in a
fixture. Everything lands in a throwaway directory outside the checkout: the
adapter's whole point is that note data never enters the repository, and the
tests hold themselves to the same rule.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

_STATE_DIR = Path(tempfile.mkdtemp(prefix="verging-adapter-tests-"))

os.environ["BASIC_MEMORY_CONFIG_DIR"] = str(_STATE_DIR / "config")
os.environ["BASIC_MEMORY_PROJECT_ROOT"] = str(_STATE_DIR / "projects")
os.environ["BASIC_MEMORY_HOME"] = str(_STATE_DIR / "projects" / "main")
# FTS is Basic Memory's default retrieval mode and needs no model download,
# which keeps these tests hermetic and fast.
os.environ["BASIC_MEMORY_SEMANTIC_SEARCH_ENABLED"] = "false"
os.environ["VERGING_ADAPTER_DATA_DIR"] = str(_STATE_DIR / "projects")
os.environ["VERGING_PRODUCT_KEY"] = "test-only-adapter-key"

for _path in ("config", "projects", "projects/main"):
    (_STATE_DIR / _path).mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(scope="session", autouse=True)
def _cleanup_state_dir():
    yield
    shutil.rmtree(_STATE_DIR, ignore_errors=True)


@pytest.fixture(scope="session")
def namespace_root() -> Path:
    """Where namespace projects land on disk — outside the checkout."""
    return _STATE_DIR / "projects"


@pytest.fixture(scope="session")
def api_key() -> str:
    return os.environ["VERGING_PRODUCT_KEY"]
