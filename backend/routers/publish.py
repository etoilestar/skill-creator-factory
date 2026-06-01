"""Publish management API router.

Provides CRUD endpoints for managing publish configurations
from the frontend admin interface.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services.publish_config import (
    delete_publish_config,
    get_available_skills,
    get_publish_config,
    load_publish_configs,
    regenerate_api_key,
    save_publish_config,
    toggle_publish_config,
    validate_skills_available,
)

router = APIRouter(prefix="/api/publish", tags=["publish"])


class PublishConfigCreate(BaseModel):
    name: str
    enabled_skills: list[str] = []
    is_active: bool = False


class PublishConfigUpdate(BaseModel):
    name: str | None = None
    enabled_skills: list[str] | None = None
    is_active: bool | None = None


@router.get("/available-skills")
async def list_available_skills():
    """Get all approved skills available for publishing."""
    skills = get_available_skills()
    return {"skills": skills}


@router.get("/configs")
async def list_configs():
    """Get all publish configurations."""
    configs = load_publish_configs()
    return {"configs": configs}


@router.post("/configs")
async def create_config(payload: PublishConfigCreate):
    """Create a new publish configuration."""
    # Validate skill names
    valid_skills = validate_skills_available(payload.enabled_skills)

    config = {
        "endpoint_id": "",  # Will be generated
        "name": payload.name,
        "enabled_skills": valid_skills,
        "is_active": payload.is_active,
    }

    saved = save_publish_config(config)
    return saved


@router.put("/configs/{endpoint_id}")
async def update_config(endpoint_id: str, payload: PublishConfigUpdate):
    """Update an existing publish configuration."""
    existing = get_publish_config(endpoint_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Config not found")

    if payload.name is not None:
        existing["name"] = payload.name
    if payload.enabled_skills is not None:
        existing["enabled_skills"] = validate_skills_available(payload.enabled_skills)
    if payload.is_active is not None:
        existing["is_active"] = payload.is_active

    saved = save_publish_config(existing)
    return saved


@router.delete("/configs/{endpoint_id}")
async def remove_config(endpoint_id: str):
    """Delete a publish configuration."""
    deleted = delete_publish_config(endpoint_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Config not found")
    return {"ok": True}


@router.post("/configs/{endpoint_id}/toggle")
async def toggle_config(endpoint_id: str):
    """Toggle active state of a publish configuration."""
    result = toggle_publish_config(endpoint_id)
    if not result:
        raise HTTPException(status_code=404, detail="Config not found")
    return result


@router.post("/configs/{endpoint_id}/regenerate-key")
async def regenerate_key(endpoint_id: str):
    """Regenerate the API key for a publish configuration."""
    result = regenerate_api_key(endpoint_id)
    if not result:
        raise HTTPException(status_code=404, detail="Config not found")
    return result
