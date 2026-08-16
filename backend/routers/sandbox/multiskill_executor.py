"""Serial Child Skill Runtime adapter and public result boundary."""

from __future__ import annotations

import inspect
import re
import uuid
from copy import deepcopy
from typing import Awaitable, Callable

from ...services.skill_governance import resolve_skill_record
from .multiskill_plan import RESULT_CHANNELS, validate_multiskill_plan

PUBLIC_RESULT_FIELDS = {"status", "mode", "text", "structured_outputs", "artifacts", "output_files"}
ENVELOPE_FIELDS = {"user_request", "input", "text", "payload", "fields", "options", "input_files", "files", "resources"}


class MultiSkillBindingError(ValueError):
    pass


def normalize_child_skill_result(raw: dict, *, child_run_id: str, skill_name: str) -> dict:
    """Remove runtime plans, observations, stdout, host paths, and all other internals."""
    raw = raw if isinstance(raw, dict) else {}
    result = {key: deepcopy(raw.get(key)) for key in PUBLIC_RESULT_FIELDS}
    result.update({"child_run_id": child_run_id, "skill_name": skill_name})
    result["status"] = str(result.get("status") or ("completed" if raw.get("success", True) else "failed"))
    result["mode"] = str(result.get("mode") or "execute")
    result["text"] = str(result.get("text") or "")
    for key in ("artifacts", "output_files"):
        result[key] = result.get(key) if isinstance(result.get(key), list) else []
    if result.get("structured_outputs") is None:
        result["structured_outputs"] = {}
    return result


def _json_path(value, path: str):
    for name, index in re.findall(r"(?:^|\.)([A-Za-z0-9_-]+)|\[([0-9]+)\]", path):
        key = int(index) if index else name
        try:
            value = value[key]
        except (KeyError, IndexError, TypeError) as exc:
            raise MultiSkillBindingError(f"result path does not exist: {path}") from exc
    return deepcopy(value)


def _resolve(binding: dict, parent: dict, results: dict):
    source = binding["source_type"]
    if source == "default":
        return deepcopy(binding.get("value"))
    if source in {"envelope", "user_input", "derived_from_user_input"}:
        if source == "user_input":
            return parent.get("user_request", parent.get("text", ""))
        path = binding.get("path")
        value = parent if not path else _json_path(parent, path)
        return deepcopy(value)
    manifest = results[binding["step_id"]]
    channel = binding["channel"]
    if channel not in RESULT_CHANNELS:
        raise MultiSkillBindingError(f"invalid channel: {channel}")
    value = manifest[channel]
    return _json_path(value, binding["path"]) if binding.get("path") else deepcopy(value)


def build_child_input_envelope(step: dict, parent_envelope: dict, skill_results: dict) -> dict:
    child = {key: deepcopy(parent_envelope.get(key)) for key in ENVELOPE_FIELDS if key in parent_envelope}
    child["user_request"] = step["task"]
    child["input"] = child["text"] = step["task"]
    for target, binding in step.get("bindings", {}).items():
        if target not in ENVELOPE_FIELDS:
            raise MultiSkillBindingError(f"binding target is not an Input Envelope field: {target}")
        child[target] = _resolve(binding, parent_envelope, skill_results)
    child.setdefault("payload", None)
    for key, default in (("fields", {}), ("options", {}), ("input_files", []), ("files", []), ("resources", [])):
        child.setdefault(key, deepcopy(default))
    return child


async def invoke_child_skill(*, step: dict, parent_envelope: dict, skill_results: dict,
                             single_skill_runtime: Callable[..., Awaitable[dict] | dict],
                             child_run_id: str | None = None) -> dict:
    """Revalidate governance, then invoke the existing complete Single-Skill Runtime."""
    record = resolve_skill_record(step["skill_name"], mode="sandbox", require_visible=True, require_executable=True)
    run_id = child_run_id or f"child_{uuid.uuid4().hex}"
    envelope = build_child_input_envelope(step, parent_envelope, skill_results)
    raw = single_skill_runtime(skill_name=step["skill_name"], skill_root=record["root_path"],
                               input_envelope=envelope, child_run_id=run_id)
    if inspect.isawaitable(raw):
        raw = await raw
    return normalize_child_skill_result(raw, child_run_id=run_id, skill_name=step["skill_name"])


async def execute_multiskill_plan(*, plan: dict, activated_skill_names, parent_envelope: dict,
                                  single_skill_runtime: Callable[..., Awaitable[dict] | dict],
                                  event_sink: Callable[[dict], object] | None = None) -> dict:
    """Execute validated nodes serially; no fan-out, retry, replacement, or parent replan."""
    canonical = validate_multiskill_plan(plan, activated_skill_names)
    results, skills, trace, artifacts, output_files = {}, {}, [], [], []
    async def emit(event):
        if event_sink:
            value = event_sink(event)
            if inspect.isawaitable(value):
                await value
    await emit({"multiskill_plan": deepcopy(canonical)})
    for step in canonical["steps"]:
        run_id = f"child_{uuid.uuid4().hex}"
        skills[step["step_id"]] = {"child_run_id": run_id, "skill_name": step["skill_name"], "status": "running"}
        await emit({"skill_started": {"step_id": step["step_id"], "skill_name": step["skill_name"], "child_run_id": run_id}})
        result = await invoke_child_skill(step=step, parent_envelope=parent_envelope, skill_results=results,
                                          single_skill_runtime=single_skill_runtime, child_run_id=run_id)
        results[step["step_id"]] = result
        skills[step["step_id"]].update(status=result["status"], result=result)
        artifacts.extend(result["artifacts"]); output_files.extend(result["output_files"])
        if result["mode"] == "ask_user":
            trace.append({"step_id": step["step_id"], "skill_name": step["skill_name"], "child_run_id": run_id, "status": "ask_user"})
            await emit({"skill_ask_user": deepcopy(trace[-1])})
            return {"success": True, "mode": "ask_user", "paused_at_step_id": step["step_id"],
                    "child_run_id": run_id, "skill_name": step["skill_name"],
                    "reason": result.get("text", ""), "missing": result.get("structured_outputs", {}).get("missing", []),
                    "skill_results": results, "skills": skills, "artifacts": artifacts,
                    "output_files": output_files, "multi_skill_trace": trace}
        if result["status"] in {"failed", "error"}:
            trace.append({"step_id": step["step_id"], "skill_name": step["skill_name"], "child_run_id": run_id, "status": "failed"})
            await emit({"skill_failed": deepcopy(trace[-1])})
            return {"success": False, "mode": "multi_skill", "failed_skill_step_id": step["step_id"],
                    "child_run_id": run_id, "skill_results": results, "skills": skills,
                    "artifacts": artifacts, "output_files": output_files, "multi_skill_trace": trace}
        trace.append({"step_id": step["step_id"], "skill_name": step["skill_name"], "child_run_id": run_id, "status": "completed"})
        await emit({"skill_completed": deepcopy(trace[-1])})
    return {"success": True, "mode": "multi_skill", "skill_results": results, "skills": skills,
            "artifacts": artifacts, "output_files": output_files, "multi_skill_trace": trace}
