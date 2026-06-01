"""Model routing helpers for creator/sandbox LLM calls.

The router keeps *action judgment* in backend code while leaving *what to do*
in SKILL.md and generated prompts.  Skills remain compatible because they can
continue to describe tasks naturally; this module only maps an inferred task
kind to a configurable model profile.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from ..config import settings

logger = logging.getLogger(__name__)

TEXT_TASK = "text"
CODE_TASK = "code"
IMAGE_TASK = "image"
PLANNER_TASK = "planner"
VALIDATOR_TASK = "validator"


@dataclass(frozen=True)
class ModelRoute:
    """Resolved model routing decision with an auditable reason."""

    task: str
    model: str
    reason: str
    requested_model: str | None = None

    def ack(self, *, actual_model: str | None = None) -> dict[str, Any]:
        """Return a compact model acknowledgement payload for logs/SSE."""
        return {
            "task": self.task,
            "model": self.model,
            "requested_model": self.requested_model,
            "actual_model": actual_model or "",
            "matched": _models_match(self.model, actual_model) if actual_model else None,
            "reason": self.reason,
        }


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _configured_routes() -> dict[str, Any]:
    """Parse optional JSON routing overrides.

    Example:
    {
      "tasks": {"code": "qwen2.5-coder:32b", "image": "sdxl"},
      "creator_paths": {"scripts/*": "qwen2.5-coder:32b", "assets/*.png": "sdxl"}
    }
    """
    raw = (settings.model_routing_json or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("MODEL_ROUTING_JSON is invalid JSON; ignoring custom routing")
        return {}
    return data if isinstance(data, dict) else {}


def _models_match(expected: str, actual: str | None) -> bool:
    """Return whether a provider model ack matches the requested model.

    OpenAI-compatible local providers often echo canonicalized names, so exact
    equality is not required for non-strict informational acks.
    """
    if not actual:
        return False
    expected_norm = expected.strip().lower()
    actual_norm = actual.strip().lower()
    return actual_norm == expected_norm or actual_norm.endswith("/" + expected_norm)


def _model_for_task(task: str, requested_model: str | None = None) -> str:
    """Resolve the concrete model for a task kind.

    Explicit per-request model overrides remain honored for text/default calls,
    but specialized configured models win for code/image/planner/validator so a
    generic UI selection cannot accidentally force image/code work through the
    wrong model.
    """
    routes = _configured_routes()
    task_routes = routes.get("tasks") if isinstance(routes.get("tasks"), dict) else {}

    if task in task_routes and task_routes[task]:
        return str(task_routes[task])

    if task == CODE_TASK and settings.code_model:
        return settings.code_model
    if task == IMAGE_TASK and settings.image_model:
        return settings.image_model
    if task == PLANNER_TASK and settings.planner_model:
        return settings.planner_model
    if task == VALIDATOR_TASK and settings.validator_model:
        return settings.validator_model
    if task == TEXT_TASK and settings.text_model:
        return settings.text_model

    return requested_model or settings.default_model


def route_model(task: str, *, requested_model: str | None = None, reason: str = "") -> ModelRoute:
    """Route an already-classified task to a configured model."""
    normalized = task if task in {TEXT_TASK, CODE_TASK, IMAGE_TASK, PLANNER_TASK, VALIDATOR_TASK} else TEXT_TASK
    return ModelRoute(
        task=normalized,
        model=_model_for_task(normalized, requested_model),
        reason=reason or f"task={normalized}",
        requested_model=requested_model,
    )


def _code_extensions() -> set[str]:
    configured = _split_csv(settings.code_file_extensions)
    return {ext if ext.startswith(".") else f".{ext}" for ext in configured}


def _image_keywords() -> list[str]:
    return [kw.lower() for kw in _split_csv(settings.image_task_keywords)]


def infer_creator_file_task(file_path: str, purpose: str = "") -> str:
    """Infer task kind for creator file generation from file path/purpose."""
    routes = _configured_routes()
    path_routes = routes.get("creator_paths") if isinstance(routes.get("creator_paths"), dict) else {}
    for pattern, task_or_model in path_routes.items():
        if fnmatch.fnmatch(file_path, str(pattern)):
            # If a custom path maps to a known task name, return it; otherwise
            # treat it as code/text later via route_creator_file_model.
            if task_or_model in {TEXT_TASK, CODE_TASK, IMAGE_TASK, PLANNER_TASK, VALIDATOR_TASK}:
                return str(task_or_model)
            return TEXT_TASK

    lowered = f"{file_path}\n{purpose}".lower()
    suffix = "." + file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
    if file_path.startswith("scripts/") or suffix in _code_extensions():
        return CODE_TASK
    if any(keyword in lowered for keyword in _image_keywords()):
        return IMAGE_TASK
    return TEXT_TASK


def route_creator_file_model(
    *,
    file_path: str,
    purpose: str = "",
    requested_model: str | None = None,
) -> ModelRoute:
    """Resolve model for creator single-file generation."""
    routes = _configured_routes()
    path_routes = routes.get("creator_paths") if isinstance(routes.get("creator_paths"), dict) else {}
    for pattern, task_or_model in path_routes.items():
        if fnmatch.fnmatch(file_path, str(pattern)) and task_or_model not in {
            TEXT_TASK,
            CODE_TASK,
            IMAGE_TASK,
            PLANNER_TASK,
            VALIDATOR_TASK,
        }:
            return ModelRoute(
                task=TEXT_TASK,
                model=str(task_or_model),
                reason=f"creator path override matched {pattern}",
                requested_model=requested_model,
            )

    task = infer_creator_file_task(file_path, purpose)
    return route_model(task, requested_model=requested_model, reason=f"creator file {file_path}")


def infer_sandbox_response_task(*, body_prompt: str, user_text: str, plan: dict | None = None) -> str:
    """Infer the best response model for a sandbox skill turn.

    The skill decides *what* needs to happen in SKILL.md.  The backend only
    classifies the observed task surface (planned actions, resources, and the
    user's request) into a model capability.
    """
    plan = plan or {}
    text = f"{user_text}\n{body_prompt[:4000]}".lower()

    tasks = plan.get("tasks") if isinstance(plan, dict) else []
    if isinstance(tasks, list):
        for task in tasks:
            if not isinstance(task, dict):
                continue
            action = str(task.get("action") or "").lower()
            command = str(task.get("command") or task.get("path") or "").lower()
            if action in {"run_command", "write_file"} and re.search(r"\.(py|js|ts|sh|rb|go|rs|java|cpp|c|cs)\b", command):
                return CODE_TASK

    if any(keyword in text for keyword in _image_keywords()):
        return IMAGE_TASK
    return TEXT_TASK
