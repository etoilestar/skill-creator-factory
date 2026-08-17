"""Serial Child Skill Runtime adapter and public result boundary."""

from __future__ import annotations

import inspect
import mimetypes
import re
import shutil
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Awaitable, Callable

from ...services.skill_governance import resolve_skill_record
from ..chat_utils import _is_within_sandbox
from .child_skill_runtime import execute_child_skill_runtime
from .multiskill_plan import RESULT_CHANNELS, validate_multiskill_plan

CONTROL_FIELDS = {"status", "mode", "success", "completed", "paused_for_user", "reason", "missing"}
DATA_FIELDS = {"text", "structured_outputs", "artifacts", "output_files"}
PUBLIC_RESULT_FIELDS = CONTROL_FIELDS | DATA_FIELDS
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
    paused = bool(result.get("paused_for_user") or result["mode"] == "ask_user")
    result["success"] = None if paused else bool(raw.get("success", result["status"] == "completed"))
    result["completed"] = False if paused else bool(raw.get("completed", result["success"] is True))
    result["paused_for_user"] = paused
    result["reason"] = str(result.get("reason") or "")
    result["missing"] = list(result.get("missing") or [])
    result["text"] = str(result.get("text") or "")
    for key in ("artifacts", "output_files"):
        result[key] = result.get(key) if isinstance(result.get(key), list) else []
    if not isinstance(result.get("structured_outputs"), list):
        result["structured_outputs"] = []
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
    if source == "derived_from_user_input":
        return deepcopy(binding.get("value"))
    if source in {"envelope", "user_input"}:
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


def materialize_child_artifacts_for_input(*, items: list[dict], source_skill_name: str,
                                          target_skill_name: str, child_run_id: str) -> list[dict]:
    """Host-validate executor artifacts and copy them into the target input boundary."""
    source_record = resolve_skill_record(source_skill_name, mode="sandbox", require_visible=True, require_executable=True)
    target_record = resolve_skill_record(target_skill_name, mode="sandbox", require_visible=True, require_executable=True)
    source_root = Path(source_record["root_path"]).resolve()
    return _materialize_files_for_child(items=items, source_root=source_root,
        target_root=Path(target_record["root_path"]).resolve(), child_run_id=child_run_id)


def _materialize_files_for_child(*, items: list[dict], source_root: Path,
                                 target_root: Path, child_run_id: str) -> list[dict]:
    """Common Host bridge for platform inputs and executor-confirmed artifacts."""
    source_root, target_root = source_root.resolve(), target_root.resolve()
    input_dir = (target_root / "inputs" / child_run_id).resolve()
    if not _is_within_sandbox(input_dir, target_root):
        raise MultiSkillBindingError("invalid target input boundary")
    input_dir.mkdir(parents=True, exist_ok=True)
    manifests = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise MultiSkillBindingError("artifact entry must be an object")
        relative = str(item.get("path") or "")
        source = (source_root / relative).resolve()
        if not relative or not _is_within_sandbox(source, source_root) or not source.is_file():
            raise MultiSkillBindingError("artifact is not an executor-confirmed local file")
        filename = Path(str(item.get("name") or item.get("filename") or source.name)).name
        target = input_dir / f"{index}-{filename}"
        collision = 1
        while target.exists():
            target = input_dir / f"{index}-{collision}-{filename}"
            collision += 1
        if not _is_within_sandbox(target.resolve(), input_dir):
            raise MultiSkillBindingError("materialized target escapes child input boundary")
        shutil.copy2(source, target)
        mime = str(item.get("mime_type") or mimetypes.guess_type(filename)[0] or "application/octet-stream")
        relative_path = target.relative_to(target_root).as_posix()
        manifests.append({
            "path": relative_path, "filename": filename, "size": target.stat().st_size,
            "mime_type": mime, "media_family": mime.partition("/")[0] + "/*",
            "model_access": "path_only", "relative_to_input_dir": target.name,
            "relative_to_input_session_dir": target.name,
        })
    return manifests


def materialize_parent_files_for_child(*, items: list[dict], source_root: Path,
                                       target_skill_name: str, child_run_id: str) -> list[dict]:
    """Validate Platform-upload descriptors and materialize them for one child."""
    target_record = resolve_skill_record(target_skill_name, mode="sandbox", require_visible=True, require_executable=True)
    return _materialize_files_for_child(items=items, source_root=Path(source_root),
        target_root=Path(target_record["root_path"]), child_run_id=child_run_id)


def build_child_input_envelope(step: dict, parent_envelope: dict, skill_results: dict,
                               *, child_run_id: str) -> dict:
    # task is the only implicit Parent -> Child value. Every business input is
    # admitted solely through an explicit, validated binding below.
    child = {
        "user_request": step["task"], "input": step["task"], "text": step["task"],
        "payload": None, "fields": {}, "options": {},
        "input_files": [], "files": [], "resources": [],
    }
    for target, binding in step.get("bindings", {}).items():
        if target not in ENVELOPE_FIELDS:
            raise MultiSkillBindingError(f"binding target is not an Input Envelope field: {target}")
        value = _resolve(binding, parent_envelope, skill_results)
        if target in {"input_files", "files"} and binding.get("source_type") == "envelope" and binding.get("path") in {"input_files", "files"}:
            source_root = parent_envelope.get("_platform_input_root")
            if not source_root:
                raise MultiSkillBindingError("platform input boundary is unavailable")
            value = materialize_parent_files_for_child(items=value, source_root=Path(source_root),
                target_skill_name=step["skill_name"], child_run_id=child_run_id)
        if target in {"input_files", "files"} and binding.get("source_type") == "skill_result" and binding.get("channel") in {"artifacts", "output_files"}:
            source_result = skill_results[binding["step_id"]]
            value = materialize_child_artifacts_for_input(items=value,
                source_skill_name=source_result["skill_name"], target_skill_name=step["skill_name"],
                child_run_id=child_run_id)
        child[target] = value
    return child


def build_single_skill_input_envelope(parent_envelope: dict, *, skill_name: str,
                                      child_run_id: str) -> dict:
    """Build the full-task fast-path envelope while keeping Host metadata private."""
    child = {
        "user_request": str(parent_envelope.get("user_request") or ""),
        "input": deepcopy(parent_envelope.get("input", parent_envelope.get("user_request", ""))),
        "text": str(parent_envelope.get("text", parent_envelope.get("user_request", "")) or ""),
        "payload": deepcopy(parent_envelope.get("payload")),
        "fields": deepcopy(parent_envelope.get("fields") or {}),
        "options": deepcopy(parent_envelope.get("options") or {}),
        "input_files": [], "files": [],
        "resources": deepcopy(parent_envelope.get("resources") or []),
    }
    parent_files = parent_envelope.get("input_files") or parent_envelope.get("files") or []
    if parent_files:
        source_root = parent_envelope.get("_platform_input_root")
        if not source_root:
            raise MultiSkillBindingError("platform input boundary is unavailable")
        manifests = materialize_parent_files_for_child(items=parent_files, source_root=Path(source_root),
            target_skill_name=skill_name, child_run_id=child_run_id)
        child["input_files"] = manifests
        child["files"] = deepcopy(manifests)
    return child


async def invoke_child_skill(*, step: dict, parent_envelope: dict, skill_results: dict,
                             single_skill_runtime: Callable[..., Awaitable[dict] | dict] | None = None,
                             child_run_id: str | None = None, model: str | None = None,
                             child_event_sink=None) -> dict:
    """Revalidate governance, then invoke the existing complete Single-Skill Runtime."""
    resolve_skill_record(step["skill_name"], mode="sandbox", require_visible=True, require_executable=True)
    run_id = child_run_id or f"child_{uuid.uuid4().hex}"
    envelope = build_child_input_envelope(step, parent_envelope, skill_results, child_run_id=run_id)
    runtime = single_skill_runtime or execute_child_skill_runtime
    raw = runtime(skill_name=step["skill_name"], input_envelope=envelope, child_run_id=run_id,
                  model=model, event_sink=child_event_sink)
    if inspect.isawaitable(raw):
        raw = await raw
    return normalize_child_skill_result(raw, child_run_id=run_id, skill_name=step["skill_name"])


async def execute_multiskill_plan(*, plan: dict, activated_skill_names, parent_envelope: dict,
                                  single_skill_runtime: Callable[..., Awaitable[dict] | dict] | None = None,
                                  event_sink: Callable[[dict], object] | None = None,
                                  model: str | None = None) -> dict:
    """Execute validated nodes serially; no fan-out, retry, replacement, or parent replan."""
    canonical = validate_multiskill_plan(plan, activated_skill_names)
    results, skills, trace, artifacts, output_files = {}, {}, [], [], []
    async def emit(event):
        if event_sink:
            value = event_sink(event)
            if inspect.isawaitable(value):
                await value
    await emit({"multiskill_plan": deepcopy(canonical)})
    if canonical["missing_required_inputs"]:
        return {"success": None, "completed": False, "paused_for_user": True,
                "mode": "ask_user", "reason": "Multi-Skill plan requires additional input.",
                "missing": deepcopy(canonical["missing_required_inputs"]), "skill_results": {},
                "skills": {}, "artifacts": [], "output_files": [], "multi_skill_trace": []}
    for step in canonical["steps"]:
        run_id = f"child_{uuid.uuid4().hex}"
        skills[step["step_id"]] = {"child_run_id": run_id, "skill_name": step["skill_name"], "status": "running"}
        await emit({"skill_started": {"step_id": step["step_id"], "skill_name": step["skill_name"], "child_run_id": run_id}})
        async def child_sink(event):
            await emit({"child_runtime_event": {"child_run_id": run_id, "event": event}})
        result = await invoke_child_skill(step=step, parent_envelope=parent_envelope, skill_results=results,
                                          single_skill_runtime=single_skill_runtime, child_run_id=run_id,
                                          model=model, child_event_sink=child_sink)
        results[step["step_id"]] = result
        skills[step["step_id"]].update(status=result["status"], result=result)
        artifacts.extend(result["artifacts"]); output_files.extend(result["output_files"])
        if result["mode"] == "ask_user":
            trace.append({"step_id": step["step_id"], "skill_name": step["skill_name"], "child_run_id": run_id, "status": "ask_user"})
            await emit({"skill_ask_user": deepcopy(trace[-1])})
            return {"success": None, "completed": False, "paused_for_user": True,
                    "mode": "ask_user", "paused_at_step_id": step["step_id"],
                    "child_run_id": run_id, "skill_name": step["skill_name"],
                    "reason": result.get("reason") or result.get("text", ""), "missing": result.get("missing") or [],
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
    return {"success": True, "completed": True, "paused_for_user": False,
            "mode": "multi_skill", "skill_results": results, "skills": skills,
            "artifacts": artifacts, "output_files": output_files, "multi_skill_trace": trace}
