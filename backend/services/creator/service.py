"""Creator service public entrypoint.

This module composes the function-oriented Creator submodules and exposes the
FastAPI router consumed by the thin router compatibility layer.  Compatibility
attribute lookups are resolved from the imported modules without mutating their
globals, so module dependencies remain visible in each split file.
"""

from types import ModuleType
from typing import Any

from . import common, contracts, e2e, repair, generation, api

_MODULES: tuple[ModuleType, ...] = (common, contracts, e2e, repair, generation, api)
router = common.router


def __getattr__(name: str) -> Any:
    for module in _MODULES:
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(name)


__all__ = ["router"]
