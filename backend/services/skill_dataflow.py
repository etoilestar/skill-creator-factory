"""Dynamic SKILL.md/script stdout dataflow helpers.

The Creator and sandbox runtime intentionally avoid hard-coded output field
names.  These helpers derive dataflow requirements from the actual Markdown
commands and ``{{placeholder}}`` references authored in a business SKILL.md.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import shlex
from typing import Any, Iterable

_COMMAND_BLOCK_RE = re.compile(r"```(?:bash|sh|shell)?\s*\n([\s\S]*?)\n```", re.IGNORECASE)
_PLACEHOLDER_RE = re.compile(r"{{\s*([A-Za-z_][\w-]*)\s*}}")


@dataclass(frozen=True)
class SkillCommandDataflow:
    """A command found in SKILL.md plus variables it consumes."""

    script_path: str
    command: str
    required_variables: tuple[str, ...]


def _extract_script_path(command: str) -> str | None:
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    for part in parts:
        normalized = part.replace("\\", "/").lstrip("./")
        if normalized.startswith("scripts/"):
            return normalized
        idx = normalized.find("/scripts/")
        if idx >= 0:
            return normalized[idx + 1 :]
    return None


def extract_skill_commands(skill_md: str) -> list[SkillCommandDataflow]:
    """Parse bash-like SKILL.md command blocks and their ``{{variables}}``."""
    commands: list[SkillCommandDataflow] = []
    for match in _COMMAND_BLOCK_RE.finditer(skill_md or ""):
        command = match.group(1).strip()
        script_path = _extract_script_path(command)
        if not script_path:
            continue
        variables = tuple(dict.fromkeys(_PLACEHOLDER_RE.findall(command)))
        commands.append(SkillCommandDataflow(script_path=script_path, command=command, required_variables=variables))
    return commands


def parse_stdout_context(stdout: str) -> dict[str, Any]:
    """Return a script stdout JSON object, or raise ValueError with context."""
    try:
        payload = json.loads((stdout or "").strip())
    except json.JSONDecodeError as exc:
        raise ValueError("stdout 不是合法 JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError("stdout 必须是 JSON object")
    return payload


def merge_context(*contexts: dict[str, Any]) -> dict[str, Any]:
    """Merge prior stdout contexts for subsequent command placeholder rendering."""
    merged: dict[str, Any] = {}
    for context in contexts:
        merged.update(context)
    return merged


def missing_variables_for_command(command: SkillCommandDataflow, context: dict[str, Any]) -> list[str]:
    """Return placeholders required by ``command`` that context cannot satisfy."""
    return [name for name in command.required_variables if not _value_present(context.get(name))]


def _value_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return any(_value_present(item) for item in value)
    if isinstance(value, dict):
        return any(_value_present(item) for item in value.values())
    return True


def validate_dataflow_closed(commands: Iterable[SkillCommandDataflow], initial_context: dict[str, Any] | None = None, stdout_by_script: dict[str, str] | None = None) -> dict[str, Any]:
    """Validate that each command's placeholders are provided before use.

    ``stdout_by_script`` maps script paths to stdout captured after execution.
    The returned context is the merged initial context plus parsed stdout from
    scripts encountered in command order.
    """
    context = dict(initial_context or {})
    stdout_map = stdout_by_script or {}
    for command in commands:
        missing = missing_variables_for_command(command, context)
        if missing:
            raise ValueError(f"{command.script_path} 缺少上游变量: {', '.join(missing)}")
        if command.script_path in stdout_map:
            context = merge_context(context, parse_stdout_context(stdout_map[command.script_path]))
    return context
