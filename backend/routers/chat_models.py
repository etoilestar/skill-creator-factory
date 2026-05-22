"""Shared chat data models used by creator/sandbox chat flows."""

from dataclasses import dataclass
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
    """

    messages: list[Message]
    model: Optional[str] = None
    input_files: list[dict] = []  # [{"path": "inputs/session/file.csv", "filename": "file.csv"}, ...]


@dataclass
class MarkdownBlock:
    """A fenced Markdown block extracted from the model output."""

    index: int
    lang: str
    code: str
    before_context: str
    after_context: str

