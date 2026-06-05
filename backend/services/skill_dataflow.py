"""Generic SKILL.md/script stdout dataflow helpers.

The sandbox workflow runtime must derive variable flow from Action schema and
``{{placeholder}}`` usage rather than from business-specific field names.  This
module owns default parsing, context merging, placeholder resolution, generic
foreach expansion, and loop-output collection.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import shlex
from pathlib import Path
from typing import Any, Iterable

_COMMAND_BLOCK_RE = re.compile(r"```(?:bash|sh|shell)?\s*\n([\s\S]*?)\n```", re.IGNORECASE)
_PLACEHOLDER_RE = re.compile(r"{{\s*([A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*|\.[0-9]+)*)\s*}}")
_KEY_VALUE_RE = re.compile(r"(?P<key>[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*)\s*[：:=]\s*(?P<value>[^，。\n,;；]+)")


@dataclass(frozen=True)
class SkillCommandDataflow:
    """A command found in SKILL.md plus variables it consumes."""

    script_path: str
    command: str
    required_variables: tuple[str, ...]


class DataflowError(ValueError):
    """Base class for generic workflow dataflow errors."""


class MissingVariablesError(DataflowError):
    """Raised when placeholders cannot be resolved from context."""

    def __init__(self, missing: Iterable[str], *, message: str | None = None):
        self.missing = tuple(dict.fromkeys(str(item) for item in missing if str(item)))
        needed = ", ".join(f"{{{{{key}}}}}" for key in self.missing)
        super().__init__(message or f"dataflow_mismatch: 缺少变量 {needed}")


class LoopExpansionError(DataflowError):
    """Raised when a step's missing variables cannot be expanded from a list."""

    def __init__(self, missing: Iterable[str], *, message: str | None = None):
        self.missing = tuple(dict.fromkeys(str(item) for item in missing if str(item)))
        needed = ", ".join(f"{{{{{key}}}}}" for key in self.missing)
        super().__init__(message or f"dataflow_mismatch: 循环变量无法展开：{needed}")


def placeholder_pattern() -> re.Pattern[str]:
    return _PLACEHOLDER_RE


def extract_placeholders(value: Any) -> set[str]:
    """Return all placeholder paths referenced by a nested value."""
    keys: set[str] = set()
    if isinstance(value, str):
        keys.update(_PLACEHOLDER_RE.findall(value))
    elif isinstance(value, list):
        for item in value:
            keys.update(extract_placeholders(item))
    elif isinstance(value, dict):
        for item in value.values():
            keys.update(extract_placeholders(item))
    return keys


def context_has(context: dict[str, Any], path: str) -> bool:
    try:
        value = resolve_context_value(context, path)
    except KeyError:
        return False
    return _value_present(value)


def missing_placeholders(keys: Iterable[str], context: dict[str, Any]) -> list[str]:
    """Return placeholder paths that cannot be resolved from context."""
    return sorted(key for key in dict.fromkeys(keys) if not context_has(context, key))


def resolve_context_value(context: dict[str, Any], path: str) -> Any:
    """Resolve a plain or dotted placeholder path from a context object."""
    parts = [part for part in str(path or "").split(".") if part]
    if not parts:
        raise KeyError(path)
    current: Any = context
    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(path)
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            try:
                current = current[index]
            except IndexError as exc:
                raise KeyError(path) from exc
            continue
        raise KeyError(path)
    return current


def replace_placeholders_in_value(value: Any, context: dict[str, Any]) -> Any:
    """Replace placeholders in nested values using only the provided context."""
    if isinstance(value, str):
        exact = _PLACEHOLDER_RE.fullmatch(value.strip())
        if exact:
            key = exact.group(1)
            try:
                return resolve_context_value(context, key)
            except KeyError as exc:
                raise MissingVariablesError([key]) from exc

        missing = missing_placeholders(_PLACEHOLDER_RE.findall(value), context)
        if missing:
            raise MissingVariablesError(missing)

        def repl(match: re.Match[str]) -> str:
            replacement = resolve_context_value(context, match.group(1))
            if isinstance(replacement, (dict, list)):
                return json.dumps(replacement, ensure_ascii=False)
            return str(replacement)

        return _PLACEHOLDER_RE.sub(repl, value)
    if isinstance(value, list):
        return [replace_placeholders_in_value(item, context) for item in value]
    if isinstance(value, dict):
        return {key: replace_placeholders_in_value(item, context) for key, item in value.items()}
    return value


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


def parse_schema_input_item(item: str) -> tuple[str, Any | None]:
    """Parse one input/default declaration into ``(key, default)``."""
    raw = (item or "").strip().strip("'\"")
    if not raw:
        return "", None

    default: Any | None = None
    default_match = re.search(
        r"(?:\(|（)\s*(?:default|默认值?|缺省值?)\s*(?:[:：=])\s*([^()（）]+?)\s*(?:\)|）)",
        raw,
        re.I,
    )
    if default_match:
        default = coerce_default_value(default_match.group(1).strip())
        raw = (raw[: default_match.start()] + raw[default_match.end() :]).strip()

    inline_match = re.match(r"^\s*([A-Za-z_][\w./-]*)\??\s*(?:[:：=])\s*(.+?)\s*$", raw)
    if inline_match:
        key = _clean_key(inline_match.group(1).rstrip("?"))
        if default is None:
            default = coerce_default_value(inline_match.group(2).strip())
        return key, default

    key = re.split(r"\s*(?:：|:|=|（|\(|\s)\s*", raw, maxsplit=1)[0]
    return _clean_key(key.rstrip("?")), default


def coerce_default_value(value: str) -> Any:
    """Coerce a textual default without using field-name heuristics."""
    cleaned = (value or "").strip().strip("'\"")
    if cleaned == "":
        return ""
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    if re.fullmatch(r"-?\d+", cleaned):
        return int(cleaned)
    if re.fullmatch(r"-?\d+\.\d+", cleaned):
        return float(cleaned)
    lowered = cleaned.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none"}:
        return None
    return cleaned


def parse_schema_default_values(text: str, *, field_pattern_factory) -> dict[str, Any]:
    """Extract default values from an Action schema text block."""
    defaults: dict[str, Any] = {}
    inputs_match = re.search(field_pattern_factory("inputs"), text or "", re.I)
    if inputs_match:
        for raw_item in re.split(r"[,，、]\s*", inputs_match.group(1)):
            key, default = parse_schema_input_item(raw_item)
            if key and default is not None:
                defaults[key] = default

    for field in ("defaults", "default_values", "默认值", "默认参数"):
        for match in re.finditer(field_pattern_factory(field), text or "", re.I):
            for raw_item in re.split(r"[,，、]\s*", match.group(1)):
                key, default = parse_schema_input_item(raw_item)
                if key and default is not None:
                    defaults[key] = default
    return defaults


def initial_context_from_entries(entries: Iterable[dict[str, Any]], *, user_text: str = "", user_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build initial workflow context from schema defaults, user text, and planner context.

    User-provided values intentionally override SKILL.md defaults.  Free-form
    natural language is only stored under generic request keys; arbitrary field
    extraction must come from JSON or explicit ``key: value`` pairs.
    """
    context: dict[str, Any] = {}
    for entry in entries:
        defaults = entry.get("default_values") or {}
        if isinstance(defaults, dict):
            context.update(defaults)

    text = (user_text or "").strip()
    if text:
        context.update({"user_request": text, "input": text, "text": text})
        context.update(extract_inline_context_values(text))

    context.update(user_context or {})
    return context


def extract_inline_context_values(user_text: str) -> dict[str, Any]:
    """Extract generic JSON or explicit ``key: value`` user-provided values."""
    values: dict[str, Any] = {}
    try:
        maybe_json = json.loads(user_text)
    except json.JSONDecodeError:
        maybe_json = None
    if isinstance(maybe_json, dict):
        values.update(maybe_json)

    for match in _KEY_VALUE_RE.finditer(user_text or ""):
        key = match.group("key")
        raw_value = match.group("value").strip()
        values[key] = coerce_default_value(raw_value)
    return values


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
    """Merge context dictionaries from left to right."""
    merged: dict[str, Any] = {}
    for context in contexts:
        if isinstance(context, dict):
            merged.update(context)
    return merged


def merge_step_output(context: dict[str, Any], script_path: str, stdout_json: dict[str, Any]) -> dict[str, Any]:
    """Merge one script stdout JSON into flat and script-name namespaced context."""
    if not isinstance(stdout_json, dict):
        return context
    context.update(stdout_json)
    namespace = Path(script_path).stem
    if namespace:
        context[namespace] = stdout_json
    return context


def expand_step_contexts(entry: dict[str, Any], context: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one or more execution contexts for a workflow step.

    If all placeholders already resolve, the step runs once.  If some are
    missing, the runtime searches existing list values whose elements can supply
    the missing placeholders and expands the step once per list element.
    """
    placeholders = set(entry.get("placeholder_keys") or [])
    missing = missing_placeholders(placeholders, context)
    if not missing:
        return [context]

    for list_key, items in _iter_context_lists(context):
        expanded: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            child = dict(context)
            child["loop_item"] = item
            child["loop_index"] = index
            child[f"{list_key}_item"] = item
            if isinstance(item, dict):
                child.update(item)
            elif len(missing) == 1:
                child[missing[0]] = item
            if missing_placeholders(missing, child):
                expanded = []
                break
            expanded.append(child)
        if expanded:
            return expanded

    if any(isinstance(value, list) for value in context.values()):
        raise LoopExpansionError(missing)
    raise MissingVariablesError(missing)


def collect_loop_outputs(step_payloads: list[dict[str, Any]], entry: dict[str, Any]) -> dict[str, Any]:
    """Collect repeated step stdout values into generic aggregate variables."""
    if len(step_payloads) <= 1:
        return {}

    keys: list[str] = []
    for key in entry.get("outputs") or []:
        key_text = str(key or "").strip()
        if key_text:
            keys.append(key_text)
    for payload in step_payloads:
        if isinstance(payload, dict):
            for key in payload.keys():
                if key not in keys:
                    keys.append(key)

    collected: dict[str, Any] = {}
    for key in keys:
        values: list[Any] = []
        for payload in step_payloads:
            if not isinstance(payload, dict) or key not in payload:
                continue
            value = payload[key]
            if isinstance(value, list):
                values.extend(value)
            else:
                values.append(value)
        if not values:
            continue
        collected[key] = values
        collected[_pluralize_key(key)] = values
        collected[f"{key}_list"] = values
    return collected


def missing_variables_for_command(command: SkillCommandDataflow, context: dict[str, Any]) -> list[str]:
    """Return placeholders required by ``command`` that context cannot satisfy."""
    return [name for name in command.required_variables if not context_has(context, name)]


def validate_dataflow_closed(
    commands: Iterable[SkillCommandDataflow],
    initial_context: dict[str, Any] | None = None,
    stdout_by_script: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Validate that each command's placeholders are provided before use."""
    context = dict(initial_context or {})
    stdout_map = stdout_by_script or {}
    for command in commands:
        missing = missing_variables_for_command(command, context)
        if missing:
            raise ValueError(f"{command.script_path} 缺少上游变量: {', '.join(missing)}")
        if command.script_path in stdout_map:
            context = merge_context(context, parse_stdout_context(stdout_map[command.script_path]))
    return context


def _clean_key(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_./-]", "", key or "")


def _value_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return bool(value)
    if isinstance(value, dict):
        return bool(value)
    return True


def _iter_context_lists(context: dict[str, Any]):
    for key, value in context.items():
        if isinstance(value, list) and value:
            yield str(key), value


def _pluralize_key(key: str) -> str:
    if key.endswith("s"):
        return key
    if key.endswith("y") and len(key) > 1 and key[-2].lower() not in "aeiou":
        return key[:-1] + "ies"
    return key + "s"
