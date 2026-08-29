"""Entrypoint for the Verging Memory CI adapter deployment.

Railway starts the container with this file (see ``railway.json``). It reads
the port the platform assigns and serves the adapter defined in
``adapter.py``; all other configuration comes from the service's environment
variables.
"""

import os
import sys
from pathlib import Path

import uvicorn

# The adapter lives beside this file rather than inside the ``basic_memory``
# package: it is integration code, not part of the product's public surface.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from adapter import create_app  # noqa: E402


def main() -> None:
    # Basic Memory needs its config directory and default project directory to
    # exist before its lifespan reconciles projects. On a fresh container these
    # live on a blank filesystem, so create them here rather than depending on
    # an image that happens to have made them.
    for variable in ("BASIC_MEMORY_CONFIG_DIR", "BASIC_MEMORY_HOME"):
        if configured := os.environ.get(variable):
            Path(configured).mkdir(parents=True, exist_ok=True)

    uvicorn.run(
        create_app,
        factory=True,
        host="0.0.0.0",  # noqa: S104 - the platform terminates TLS in front of us
        port=int(os.environ.get("PORT", "8000")),
        access_log=False,
    )


if __name__ == "__main__":
    main()
