"""Run the Verging Memory CI adapter with uvicorn.

Used as the deployment start command: `python -m verging_memory_ci_adapter`.
"""

from __future__ import annotations

import os

import uvicorn

from verging_memory_ci_adapter.app import create_app


def main() -> None:
    # The platform assigns the port; binding all interfaces is what makes the
    # container reachable through its edge proxy.
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(create_app(), host="0.0.0.0", port=port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
