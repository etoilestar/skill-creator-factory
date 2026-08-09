"""Single injectable gateway for profile-routed Creator model calls."""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Literal

from ..creator_model_profiles import complete_creator_role_once


async def creator_model_call(
    messages: list[dict[str, Any]], *, role: Literal["planner", "reviewer"],
    fallback_model: str, stage: str = "creator",
    model_call: Callable[..., Awaitable[str]] | None = None,
) -> str:
    return await complete_creator_role_once(
        messages, role, fallback_model=fallback_model, stage=stage,
        model_call=model_call,
    )
