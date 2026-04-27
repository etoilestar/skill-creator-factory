"""Skill creation API routes. Pure routing — no business logic here."""
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.modules import (
    data_store,
    prompt_generator,
    skill_file_generator,
    state_machine,
    user_input_handler,
    packager,
)

router = APIRouter(prefix="/api")


class StartResponse(BaseModel):
    session_id: str
    first_prompt: dict


class ChatRequest(BaseModel):
    session_id: str
    field_name: str
    value: str


class ChatResponse(BaseModel):
    ok: bool
    error: str = ""
    next_prompt: dict = {}


class GenerateRequest(BaseModel):
    session_id: str


class GenerateResponse(BaseModel):
    skill_path: str
    skill_name: str


@router.post("/start", response_model=StartResponse)
def start():
    """Create a new session and return the first prompt."""
    session_id = str(uuid.uuid4())
    state_machine.reset_session(session_id)
    first_prompt = prompt_generator.generate_prompt(session_id)
    return {"session_id": session_id, "first_prompt": first_prompt}


@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    """Accept user input, validate, store, advance state, return next prompt."""
    ok, err = user_input_handler.save_user_input(req.session_id, req.field_name, req.value)
    if not ok:
        return {"ok": False, "error": err, "next_prompt": {}}

    # Save to persistent store too
    collected = state_machine.get_collected_data(req.session_id)
    data_store.save_session(req.session_id, collected)

    state_machine.next_step(req.session_id)
    next_prompt = prompt_generator.generate_prompt(req.session_id)
    return {"ok": True, "error": "", "next_prompt": next_prompt}


@router.post("/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest):
    """Generate the skill folder from collected session data."""
    collected = state_machine.get_collected_data(req.session_id)
    if not collected.get("name"):
        # Try loading from persistent store
        stored = data_store.load_session(req.session_id)
        if stored:
            collected = stored
    if not collected.get("name"):
        raise HTTPException(status_code=400, detail="Skill name not yet provided")
    try:
        path = skill_file_generator.generate_skill_folder(collected)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"skill_path": path, "skill_name": collected["name"]}


@router.get("/package/{skill_name}")
def package(skill_name: str):
    """Package and return the skill ZIP as a file download."""
    try:
        zip_path = packager.package_skill(skill_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Derive the download filename from the resolved filesystem path,
    # not from the raw user-supplied skill_name, to prevent header injection.
    zip_filename = Path(zip_path).name
    return FileResponse(
        path=zip_path,
        filename=zip_filename,
        media_type="application/zip",
    )


@router.get("/status/{session_id}")
def status(session_id: str):
    """Return current step state for a session."""
    step = state_machine.get_current_step(session_id)
    collected = state_machine.get_collected_data(session_id)
    return {
        "session_id": session_id,
        "phase": step["phase"],
        "step_index": step["step_index"],
        "completed": step["completed"],
        "collected_fields": list(collected.keys()),
    }


@router.get("/skills")
def list_skills():
    """List all generated skills."""
    return {"skills": data_store.list_all_skills()}
