"""Creator service package.

This package owns the Creator API implementation and exposes a stable FastAPI
router for the legacy ``backend.routers.creator`` compatibility module.
"""

from .service import router

__all__ = ["router"]
