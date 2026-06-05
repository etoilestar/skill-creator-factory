"""Sandbox session state management for step-skipping optimization.

Maintains in-memory per-session state that tracks which pipeline steps
have already been executed, caches their outputs, and provides a simple
heuristic intent classifier so that follow-up messages in the same
conversation can skip redundant work (metadata analysis, body loading,
resource selection, etc.).
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

class DialogIntent(enum.Enum):
    """Heuristic classification of the latest user message within a
    multi-turn sandbox conversation."""

    NEW_TASK = "new_task"        # brand-new request, likely unrelated
    CLARIFY = "clarify"          # supplementing / clarifying a previous request
    CORRECT = "correct"          # correcting a mistake
    CONTINUE = "continue"        # continuing the previous task
    QUESTION = "question"        # simple question about the skill


# Keyword lists for heuristic intent detection
_CLARIFY_KEYWORDS = frozenset({
    "补充", "补充一下", "另外", "还有", "加上", "增加", "更多信息",
    "具体", "详细", "说明", "解释", "是指", "意思是",
})
_CORRECT_KEYWORDS = frozenset({
    "不对", "错了", "不是", "应该是", "改成", "修改", "纠正",
    "重新", "换", "不要", "别",
})
_CONTINUE_KEYWORDS = frozenset({
    "继续", "接着", "下一步", "然后", "之后", "接下来", "还有呢",
})
_QUESTION_KEYWORDS = frozenset({
    "什么是", "怎么", "为什么", "请问", "如何", "能不能", "是否",
    "吗", "呢", "？", "?",
})


def classify_dialog_intent(messages: list) -> DialogIntent:
    """Classify the intent of the latest user message using heuristic rules.

    Parameters
    ----------
    messages : list
        The full message history (list of Message-like objects with
        ``role`` and ``content`` attributes).

    Returns
    -------
    DialogIntent
    """
    # First message is always a new task
    user_msgs = [m for m in messages if getattr(m, "role", None) == "user"]
    if len(user_msgs) <= 1:
        return DialogIntent.NEW_TASK

    last_content = getattr(user_msgs[-1], "content", "") or ""
    last_lower = last_content.lower()

    # Check keyword sets in priority order
    if any(kw in last_lower for kw in _CORRECT_KEYWORDS):
        return DialogIntent.CORRECT
    if any(kw in last_lower for kw in _CONTINUE_KEYWORDS):
        return DialogIntent.CONTINUE
    if any(kw in last_lower for kw in _CLARIFY_KEYWORDS):
        return DialogIntent.CLARIFY
    if any(kw in last_lower for kw in _QUESTION_KEYWORDS):
        return DialogIntent.QUESTION

    # Default: treat as a new task if the message is long (> 50 chars)
    # or if there are no recent assistant messages asking for clarification.
    if len(last_content.strip()) > 50:
        return DialogIntent.NEW_TASK

    # Short follow-up in an existing conversation → treat as clarification
    return DialogIntent.CLARIFY


# ---------------------------------------------------------------------------
# Step definitions
# ---------------------------------------------------------------------------

class StepName(enum.Enum):
    """Pipeline steps that can potentially be skipped."""

    METADATA = "metadata"
    LOAD_BODY = "load_body"
    CHILD_SKILL = "child_skill"
    RESOURCES = "resources"


# Steps that are safe to skip for each intent (when already cached).
# Steps NOT listed here will always be re-executed.
_SKIP_ELIGIBLE: Dict[DialogIntent, Set[StepName]] = {
    DialogIntent.CLARIFY: {StepName.METADATA, StepName.LOAD_BODY, StepName.CHILD_SKILL, StepName.RESOURCES},
    DialogIntent.CONTINUE: {StepName.METADATA, StepName.LOAD_BODY, StepName.CHILD_SKILL, StepName.RESOURCES},
    DialogIntent.QUESTION: {StepName.METADATA, StepName.LOAD_BODY, StepName.CHILD_SKILL, StepName.RESOURCES},
    DialogIntent.CORRECT: {StepName.METADATA, StepName.LOAD_BODY, StepName.CHILD_SKILL},
    # NEW_TASK: nothing is skipped
    DialogIntent.NEW_TASK: set(),
}


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

@dataclass
class SandboxSessionState:
    """In-memory state for a single sandbox conversation session.

    Tracks which pipeline steps have been completed and caches their
    outputs so that subsequent turns can skip redundant work.
    """

    session_id: str
    skill_name: str

    # Cached step outputs
    cached_artifacts: Dict[str, Any] = field(default_factory=dict)

    # Which steps have been completed at least once
    completed_steps: Set[str] = field(default_factory=set)

    # Timestamps for each completed step
    step_timestamps: Dict[str, float] = field(default_factory=dict)

    # The body_prompt loaded in the previous turn (may be reused)
    body_prompt: Optional[str] = None

    # The need_body decision from the metadata round
    need_body: Optional[bool] = None

    # The child_skill decision dict
    child_decision: Optional[dict] = None

    # The resource selection decision dict
    resource_decision: Optional[dict] = None

    # The body_prompt *after* appending child skill and resources
    augmented_body_prompt: Optional[str] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def cache_artifact(self, step: StepName, artifact: Any) -> None:
        """Record the output of a completed step."""
        key = step.value
        self.cached_artifacts[key] = artifact
        self.completed_steps.add(key)
        self.step_timestamps[key] = time.time()

    def get_cached(self, step: StepName) -> Optional[Any]:
        """Return the cached artifact for *step*, or ``None``."""
        return self.cached_artifacts.get(step.value)

    def should_skip(self, step: StepName, intent: DialogIntent) -> bool:
        """Decide whether *step* can be skipped given *intent*.

        A step is skipped only when:
        1. It has been completed before (output is cached).
        2. The current intent allows skipping it.
        """
        if step.value not in self.completed_steps:
            return False
        eligible = _SKIP_ELIGIBLE.get(intent, set())
        return step in eligible

    def invalidate(self) -> None:
        """Clear all cached state (e.g. when context changes drastically)."""
        self.cached_artifacts.clear()
        self.completed_steps.clear()
        self.step_timestamps.clear()
        self.body_prompt = None
        self.need_body = None
        self.child_decision = None
        self.resource_decision = None
        self.augmented_body_prompt = None


# ---------------------------------------------------------------------------
# Session store (in-memory)
# ---------------------------------------------------------------------------

_sessions: Dict[str, SandboxSessionState] = {}

# Auto-expire sessions older than this many seconds (30 min)
_SESSION_TTL = 30 * 60


def _cleanup_expired() -> None:
    """Remove sessions that have not been accessed recently."""
    now = time.time()
    expired = [
        sid
        for sid, state in _sessions.items()
        if now - max(state.step_timestamps.values(), default=now) > _SESSION_TTL
    ]
    for sid in expired:
        del _sessions[sid]


def get_or_create_session(session_id: str, skill_name: str) -> SandboxSessionState:
    """Retrieve an existing session or create a new one."""
    _cleanup_expired()
    state = _sessions.get(session_id)
    if state is None or state.skill_name != skill_name:
        state = SandboxSessionState(session_id=session_id, skill_name=skill_name)
        _sessions[session_id] = state
    return state


def delete_session(session_id: str) -> None:
    """Remove a session from the store."""
    _sessions.pop(session_id, None)
