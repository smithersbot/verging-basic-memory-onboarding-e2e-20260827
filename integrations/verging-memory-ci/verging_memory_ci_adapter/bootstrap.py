"""Process-level environment bootstrap for the Verging Memory CI adapter.

The adapter drives a real Basic Memory instance in-process. Basic Memory reads
its configuration from the environment exactly once (``ConfigManager`` caches
the loaded config), so every value has to be in place *before* the first
``basic_memory`` import that touches config. Everything in this module therefore
runs ahead of the FastAPI app being constructed.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Trigger: no explicit data root configured.
# Why: namespace notes must never land inside the git checkout — the Verging
# Memory CI action commits report files from the repository root, and product
# data captured by that commit would leak into the customer's history.
# Outcome: data defaults to a system temp path that is outside any checkout.
DATA_ROOT_ENV = "VERGING_ADAPTER_DATA_ROOT"
DEFAULT_DATA_ROOT = Path(tempfile.gettempdir()) / "verging-memory-ci"

CREDENTIAL_ENV = "VERGING_PRODUCT_KEY"


def resolve_data_root() -> Path:
    """Resolve the directory that holds all adapter-owned Basic Memory state."""
    configured = os.environ.get(DATA_ROOT_ENV)
    return Path(configured).expanduser() if configured else DEFAULT_DATA_ROOT


def configure_basic_memory_environment(data_root: Path | None = None) -> Path:
    """Point Basic Memory at adapter-owned directories and return the data root.

    Idempotent: values already present in the environment win, so a deployment
    can override any individual path.
    """
    root = data_root if data_root is not None else resolve_data_root()

    home = root / "home"
    projects = root / "projects"
    config_dir = root / "config"
    for directory in (home, projects, config_dir):
        directory.mkdir(parents=True, exist_ok=True)

    # Storage paths are set unconditionally, not with setdefault: the repository's
    # container image already exports BASIC_MEMORY_HOME and
    # BASIC_MEMORY_PROJECT_ROOT pointing inside /app, and inheriting those would
    # put namespace notes inside the checkout. VERGING_ADAPTER_DATA_ROOT is the
    # single knob for relocating adapter storage.
    os.environ["BASIC_MEMORY_CONFIG_DIR"] = str(config_dir)
    os.environ["BASIC_MEMORY_HOME"] = str(home)
    # Constrains every project Basic Memory creates to `projects/`, which makes a
    # traversal escape from a namespace name structurally impossible.
    os.environ["BASIC_MEMORY_PROJECT_ROOT"] = str(projects)

    behavior_defaults = {
        # The adapter is the only writer; routing must never reach for cloud
        # credentials that this deployment does not have.
        "BASIC_MEMORY_FORCE_LOCAL": "true",
        # Semantic search would download an embedding model on first use. The
        # adapter relies on Basic Memory's FTS retrieval, which needs no model.
        "BASIC_MEMORY_SEMANTIC_SEARCH_ENABLED": "false",
        "LOGFIRE_IGNORE_NO_CONFIG": "1",
    }
    for key, value in behavior_defaults.items():
        os.environ.setdefault(key, value)

    return root


def read_credential() -> str:
    """Read the scoped bearer credential this deployment accepts.

    Fails fast: a deployment without a credential would otherwise serve an
    unauthenticated memory store on a public origin.
    """
    credential = os.environ.get(CREDENTIAL_ENV, "").strip()
    if not credential:
        raise RuntimeError(
            f"{CREDENTIAL_ENV} is not set. The adapter refuses to start without "
            "the scoped bearer credential it must require on every request."
        )
    return credential
