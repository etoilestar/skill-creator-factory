"""Shared chat data models used by creator/sandbox chat flows."""

from dataclasses import dataclass, field
from typing import Optional

from pydantic import BaseModel


class Message(BaseModel):
    """Single chat message exchanged between user and assistant."""

    role: str  # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    """Payload for chat endpoints.

    - messages: ordered chat history
    - model: optional model override (defaults to settings.default_model)
    - input_files: uploaded file descriptors with path/filename
    - execution_mode: "plan" (规划模式，预览后确认再执行) or "execute" (执行模式，直接执行)
      - Backward compatible: "craft" is treated as "execute"
    - sandbox_session_id: optional session identifier for step-skipping optimization;
      when provided, the backend can reuse cached step outputs across turns.
    """

    messages: list[Message]
    model: Optional[str] = None
    input_files: list[dict] = []  # [{"path": "inputs/session/file.csv", "filename": "file.csv"}, ...]
    execution_mode: Optional[str] = "execute"  # "plan" | "execute" | "craft"(deprecated)
    sandbox_session_id: Optional[str] = None

    def effective_execution_mode(self) -> str:
        """Return the normalized execution mode.

        Maps deprecated "craft" to "execute" for backward compatibility.
        """
        mode = (self.execution_mode or "execute").strip().lower()
        if mode == "craft":
            return "execute"
        return mode


@dataclass
class MarkdownBlock:
    """A fenced Markdown block extracted from the model output."""

    index: int
    lang: str
    code: str
    before_context: str
    after_context: str


@dataclass
class SandboxExecutionResult:
    """Standardized result from a sandbox task execution.

    Used to carry execution state between the retry loop and the caller,
    providing a uniform interface regardless of success/failure/retry.
    """

    success: bool = False
    action: str = ""
    attempt: int = 0
    max_retries: int = 3
    result: dict = field(default_factory=dict)
    corrected_task: dict | None = None
    error_message: str = ""

