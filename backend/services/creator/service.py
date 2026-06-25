"""Creator service public entrypoint.

This module composes the function-oriented Creator submodules, hydrates their
legacy helper namespace for backwards-compatible call paths, and exposes the
FastAPI router consumed by the thin router compatibility layer.
"""

from types import ModuleType
from typing import Any

from . import common, contracts, e2e, repair, generation, api

_MODULES: tuple[ModuleType, ...] = (common, contracts, e2e, repair, generation, api)


def _exported_namespace() -> dict[str, Any]:
    namespace: dict[str, Any] = {}
    for module in _MODULES:
        namespace.update({k: v for k, v in module.__dict__.items() if not k.startswith("__")})
    return namespace


def _hydrate_legacy_globals() -> dict[str, Any]:
    """Share helper globals across the mechanically split Creator modules.

    The previous Creator implementation resolved many private helpers from a
    single module namespace.  Hydrating each split module with the union keeps
    those call paths stable while allowing the code to live in smaller
    responsibility-based files.
    """
    namespace = _exported_namespace()
    for module in _MODULES:
        module.__dict__.update(namespace)
    globals().update(namespace)
    return namespace


_hydrated_namespace = _hydrate_legacy_globals()
router = common.router


def __getattr__(name: str) -> Any:
    try:
        return _hydrated_namespace[name]
    except KeyError as exc:
        raise AttributeError(name) from exc


__all__ = ["router"]
