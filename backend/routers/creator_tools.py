"""Creator tool capability management endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services.creator_tool_registry import (
    RESOURCE_ROLES,
    TOOL_OVERRIDE_PERSISTENCE,
    capabilities_for_role,
    get_script_roles,
    get_tool_capability,
    list_tool_capabilities,
    set_tool_capability_override,
    tool_status,
)

router = APIRouter(prefix="/api/creator", tags=["creator-tools"])


class ToolPatchRequest(BaseModel):
    enabled: bool | None = None
    allow_creator_use: bool | None = None


class ToolTestRequest(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


def _tool_or_404(name: str):
    cap = get_tool_capability(name)
    if cap is None:
        raise HTTPException(status_code=404, detail=f"Unknown creator tool capability: {name}")
    return cap


@router.get("/tools")
def list_creator_tools() -> dict[str, Any]:
    return {
        "tools": [tool_status(cap) for cap in list_tool_capabilities()],
        "override_persistence": TOOL_OVERRIDE_PERSISTENCE,
        "note": "Tool toggles are process-memory overrides in this P0 registry layer; runtime helpers may still be missing until follow-up implementation.",
    }


@router.get("/tools/{name}")
def get_creator_tool(name: str) -> dict[str, Any]:
    return {"tool": tool_status(_tool_or_404(name))}


@router.patch("/tools/{name}")
def update_creator_tool(name: str, patch: ToolPatchRequest) -> dict[str, Any]:
    cap = set_tool_capability_override(
        name,
        enabled=patch.enabled,
        allow_creator_use=patch.allow_creator_use,
    )
    if cap is None:
        raise HTTPException(status_code=404, detail=f"Unknown creator tool capability: {name}")
    return {"tool": tool_status(cap)}


@router.post("/tools/{name}/test")
def test_creator_tool(name: str, request: ToolTestRequest | None = None) -> dict[str, Any]:
    cap = _tool_or_404(name)
    status = tool_status(cap)
    payload_keys = sorted((request.payload if request else {}).keys())
    configured = bool(status["configured"])
    runtime_ready = not status["missing_runtime_helpers"]
    creator_available = bool(status["creator_available"])
    success = configured and runtime_ready and creator_available
    if not creator_available:
        message = "tool is disabled for Creator use; no external side effect was performed"
    elif not configured:
        message = "tool configuration is incomplete; no external side effect was performed"
    elif not runtime_ready:
        message = "tool configuration is complete, but runtime helpers are not implemented; no external side effect was performed"
    else:
        message = "tool configuration and runtime helpers look ready; no external side effect was performed"
    return {
        "success": success,
        "tool": status,
        "trial_mode": cap.trial_mode,
        "dry_run": True,
        "side_effect_performed": False,
        "message": message,
        # Do not echo payload values: callers may pass secrets or sample PII.
        "payload_keys": payload_keys,
    }


@router.get("/tool-roles")
def list_creator_tool_roles() -> dict[str, Any]:
    return {
        "roles": [
            {
                "role": role,
                "required_capabilities": capabilities_for_role(role)[0],
                "forbidden_capabilities": capabilities_for_role(role)[1],
            }
            for role in get_script_roles()
        ],
        "resource_roles": sorted(RESOURCE_ROLES),
    }
