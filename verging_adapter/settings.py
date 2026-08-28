"""Runtime settings and on-disk layout for the adapter.

The adapter owns one Basic Memory installation whose state lives entirely
outside the source checkout, so a Verging Memory CI report commit can never
capture test data.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

API_KEY_ENV = "VERGING_ADAPTER_API_KEY"
DATA_DIR_ENV = "VERGING_ADAPTER_DATA_DIR"

# The bearer credential is scoped to this non-production deployment, but it
# still guards a writable store on the public internet. Reject anything short
# enough to be guessed rather than starting with weak protection.
MIN_API_KEY_LENGTH = 16

_REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class AdapterSettings:
    """Resolved adapter configuration.

    Layout under ``data_dir``:

    - ``config/``     Basic Memory's config file and SQLite database
                      (``BASIC_MEMORY_CONFIG_DIR``).
    - ``bootstrap/``  A permanent, empty default project. Basic Memory refuses
                      to delete its default project, so namespaces must never
                      become the default.
    - ``namespaces/`` One directory per namespace, each a Basic Memory project.

    ``bootstrap`` and ``namespaces`` are siblings on purpose: Basic Memory
    rejects projects whose directory trees are nested inside one another.
    """

    data_dir: Path
    api_key: str

    @property
    def config_dir(self) -> Path:
        return self.data_dir / "config"

    @property
    def bootstrap_dir(self) -> Path:
        return self.data_dir / "bootstrap"

    @property
    def namespaces_dir(self) -> Path:
        return self.data_dir / "namespaces"

    @classmethod
    def from_env(cls) -> "AdapterSettings":
        """Build settings from the environment, failing fast on a bad setup."""
        api_key = os.environ.get(API_KEY_ENV, "").strip()
        if not api_key:
            raise RuntimeError(
                f"{API_KEY_ENV} is not set. The adapter refuses to serve an "
                "unauthenticated memory store."
            )
        if len(api_key) < MIN_API_KEY_LENGTH:
            raise RuntimeError(f"{API_KEY_ENV} must be at least {MIN_API_KEY_LENGTH} characters.")

        data_dir_value = os.environ.get(DATA_DIR_ENV, "").strip()
        if not data_dir_value:
            raise RuntimeError(
                f"{DATA_DIR_ENV} is not set. It must point outside the source checkout."
            )

        return cls(data_dir=Path(data_dir_value).expanduser().resolve(), api_key=api_key)

    def prepare(self) -> None:
        """Create the directory layout and point Basic Memory at it.

        Trigger: adapter startup, before any Basic Memory module reads config.
        Why: Basic Memory resolves its config directory (and therefore its
        database) from ``BASIC_MEMORY_CONFIG_DIR`` at first use; a stray default
        would put the store in the container's home directory instead.
        Outcome: every later Basic Memory call reads and writes under data_dir.
        """
        if self.data_dir == _REPO_ROOT or self.data_dir.is_relative_to(_REPO_ROOT):
            raise RuntimeError(
                f"{DATA_DIR_ENV} ({self.data_dir}) is inside the source checkout "
                f"({_REPO_ROOT}); adapter data must never be commitable."
            )

        for directory in (self.config_dir, self.bootstrap_dir, self.namespaces_dir):
            directory.mkdir(parents=True, exist_ok=True)

        os.environ["BASIC_MEMORY_CONFIG_DIR"] = str(self.config_dir)

        # Trigger: the host environment already names a Basic Memory home or
        # project root (a developer machine, or a shell that once ran the CLI).
        # Why: either would add a project this adapter did not create, and a
        # project root would override the namespace paths it asks for.
        # Outcome: the adapter's Basic Memory holds only its own projects.
        for inherited in ("BASIC_MEMORY_HOME", "BASIC_MEMORY_PROJECT_ROOT"):
            os.environ.pop(inherited, None)
