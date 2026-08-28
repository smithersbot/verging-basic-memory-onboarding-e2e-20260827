"""Serve the adapter. Entry point for the container image."""

import os

import uvicorn

from verging_adapter.app import create_app


def main() -> None:
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
