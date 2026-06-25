"""Compatibility router for Creator endpoints.

The Creator implementation lives in :mod:`backend.services.creator` so this
module stays as a thin FastAPI entrypoint that preserves the existing API routes
and legacy imports used by tests or integrations.
"""

from ..services.creator import service as _creator_service

router = _creator_service.router


def __getattr__(name: str):
    """Forward legacy attribute imports to the Creator service module."""
    return getattr(_creator_service, name)


__all__ = ["router"]
