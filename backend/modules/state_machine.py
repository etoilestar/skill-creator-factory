"""Pure in-memory conversation state machine. No external dependencies."""
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SessionState:
    session_id: str
    phase: int = 1
    step_index: int = 0
    collected_data: dict = field(default_factory=dict)
    completed: bool = False


# Global in-memory store: session_id -> SessionState
_sessions: dict[str, SessionState] = {}

# Total steps per phase (from skill_kernel_loader definition)
_PHASE_STEPS = {1: 4, 2: 4, 3: 3, 4: 2, 5: 1}
_MAX_PHASE = 5


def _get_or_create(session_id: str) -> SessionState:
    if session_id not in _sessions:
        _sessions[session_id] = SessionState(session_id=session_id)
    return _sessions[session_id]


def get_current_step(session_id: str) -> dict:
    """Return {phase, step_index, completed}."""
    s = _get_or_create(session_id)
    return {"phase": s.phase, "step_index": s.step_index, "completed": s.completed}


def save_field_data(session_id: str, key: str, value: Any) -> None:
    """Store a key-value pair for this session."""
    s = _get_or_create(session_id)
    s.collected_data[key] = value


def next_step(session_id: str) -> dict:
    """Advance to the next step/phase. Returns new state."""
    s = _get_or_create(session_id)
    max_steps = _PHASE_STEPS.get(s.phase, 1)
    s.step_index += 1
    if s.step_index >= max_steps:
        if s.phase < _MAX_PHASE:
            s.phase += 1
            s.step_index = 0
        else:
            s.completed = True
    return get_current_step(session_id)


def is_phase_completed(session_id: str) -> bool:
    """True when the entire workflow is done."""
    s = _get_or_create(session_id)
    return s.completed


def reset_session(session_id: str) -> None:
    """Remove and recreate session from scratch."""
    _sessions.pop(session_id, None)


def get_collected_data(session_id: str) -> dict:
    """Return all data collected so far."""
    s = _get_or_create(session_id)
    return dict(s.collected_data)
