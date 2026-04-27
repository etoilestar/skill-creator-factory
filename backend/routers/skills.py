from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services.skill_manager import delete_skill, get_skill, list_skills, save_skill

router = APIRouter(prefix="/api/skills", tags=["skills"])


class SaveSkillRequest(BaseModel):
    name: str
    content: str


@router.get("")
async def get_all_skills():
    """List all skills in the skills directory."""
    return list_skills()


@router.get("/{skill_name}")
async def get_one_skill(skill_name: str):
    """Get a single skill's metadata and SKILL.md content."""
    try:
        return get_skill(skill_name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("")
async def create_or_update_skill(request: SaveSkillRequest):
    """Create or overwrite a skill with provided SKILL.md content."""
    return save_skill(request.name, request.content)


@router.delete("/{skill_name}")
async def remove_skill(skill_name: str):
    """Delete a skill directory."""
    try:
        delete_skill(skill_name)
        return {"message": f"Skill '{skill_name}' deleted"}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
