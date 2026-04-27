"""JSON-file based session persistence with multi-session isolation."""
import json
import os
import re
import threading
from pathlib import Path
from typing import Optional

from .config import SKILL_DATA_PATH

_lock = threading.Lock()

# Only allow UUIDs and simple alphanumeric+hyphen session IDs
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]{0,127}$")


def _safe_session_id(session_id: str) -> str:
    """Validate session_id to prevent path traversal. Raises ValueError on bad input."""
    if not _SESSION_ID_RE.match(session_id):
        raise ValueError(f"Invalid session_id: {session_id!r}")
    safe = Path(session_id).name
    if safe != session_id or ".." in session_id:
        raise ValueError(f"Invalid session_id: {session_id!r}")
    return safe


def _sessions_dir() -> Path:
    d = Path(SKILL_DATA_PATH) / ".sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_session(session_id: str, data: dict) -> None:
    """Persist session data as JSON."""
    sid = _safe_session_id(session_id)
    path = _sessions_dir() / f"{sid}.json"
    with _lock:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_session(session_id: str) -> Optional[dict]:
    """Load session data. Returns None if not found."""
    sid = _safe_session_id(session_id)
    path = _sessions_dir() / f"{sid}.json"
    if not path.exists():
        return None
    with _lock:
        return json.loads(path.read_text(encoding="utf-8"))


def list_all_skills() -> list[str]:
    """Return names of all generated skill folders in skill-data/."""
    root = Path(SKILL_DATA_PATH)
    if not root.exists():
        return []
    return [
        d.name
        for d in root.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    ]


def delete_session(session_id: str) -> None:
    """Remove a session file."""
    sid = _safe_session_id(session_id)
    path = _sessions_dir() / f"{sid}.json"
    with _lock:
        path.unlink(missing_ok=True)
