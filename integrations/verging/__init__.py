"""Verging Memory CI product adapter for Basic Memory.

The adapter serves the standardized Verging Memory CI wire contract on top of the
real Basic Memory runtime. A namespace is a real Basic Memory project, a stored
memory is a real markdown note, and recall is a real Basic Memory search: every
route goes through the same in-process v2 API the MCP tools call.

Importing this package pins Basic Memory's state directories *before* any
``basic_memory`` module is imported. ``ConfigManager`` reads these environment
variables once and caches the resulting config, so the pinning has to happen at
package-import time; ``integrations.verging.app`` imports this ``__init__`` first,
which is what keeps notes, config and the index database outside the git checkout
that Verging's report commits are written into.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Environment variable holding the scoped bearer credential the adapter requires.
PRODUCT_KEY_ENV = "VERGING_PRODUCT_KEY"

# Environment variable overriding where Basic Memory keeps adapter state.
DATA_DIR_ENV = "VERGING_ADAPTER_DATA_DIR"


def _default_data_root() -> Path:
    """Data root used when the deployment does not pin one."""
    return Path(tempfile.gettempdir()) / "verging-memory-ci"


def configure_basic_memory_state() -> Path:
    """Point Basic Memory's config, database and note files at a private data root.

    The three variables are set unconditionally rather than with ``setdefault``:
    the repository's container image already exports ``BASIC_MEMORY_HOME`` and
    ``BASIC_MEMORY_PROJECT_ROOT`` pointing inside the deployed source tree, and a
    namespace's notes must never land in a checkout.

    Setting ``BASIC_MEMORY_PROJECT_ROOT`` also buys namespace isolation for free:
    Basic Memory then resolves every project directory as
    ``project_root/<permalink(project name)>`` and ignores the requested path, so
    an attacker-supplied namespace name cannot escape the data root.
    """
    data_root = Path(os.environ.get(DATA_DIR_ENV) or _default_data_root()).expanduser()
    state_dir = data_root / "state"
    projects_root = data_root / "projects"
    default_project_home = projects_root / "main"

    os.environ["BASIC_MEMORY_CONFIG_DIR"] = str(state_dir)
    os.environ["BASIC_MEMORY_PROJECT_ROOT"] = str(projects_root)
    os.environ["BASIC_MEMORY_HOME"] = str(default_project_home)

    for path in (state_dir, projects_root, default_project_home):
        path.mkdir(parents=True, exist_ok=True)

    return data_root


DATA_ROOT = configure_basic_memory_state()

__all__ = ["DATA_DIR_ENV", "DATA_ROOT", "PRODUCT_KEY_ENV", "configure_basic_memory_state"]
