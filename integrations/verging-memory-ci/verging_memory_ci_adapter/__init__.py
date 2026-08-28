"""Verging Memory CI adapter for Basic Memory."""

__all__ = ["create_app"]


def create_app(*args, **kwargs):  # pragma: no cover - thin re-export
    from verging_memory_ci_adapter.app import create_app as _create_app

    return _create_app(*args, **kwargs)
