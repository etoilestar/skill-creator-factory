"""Production adapter from a Child Skill invocation to the existing runtime."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from ...services.skill_governance import resolve_skill_record
from ..chat_models import ChatRequest
from .action_schema import _build_runtime_action_schema
from .resource_catalog import _extract_runtime_resource_catalog
from .result_manifest import build_result_manifest
from .runtime_execution_plan import RuntimePlanError
from .workflow_dataflow import execute_skill_workflow


def build_child_runtime_result(*, workflow_result: dict, execution_root: Path,
                               child_run_id: str, skill_name: str, final_text: str = "") -> dict:
    """Adapt runtime truth into separate control- and data-plane fields."""
    manifest = build_result_manifest(workflow_result, execution_root)
    success = workflow_result.get("success")
    paused = bool(workflow_result.get("paused_for_user") or workflow_result.get("mode") == "ask_user")
    completed = bool(workflow_result.get("completed", success is True)) and not paused
    status = "ask_user" if paused else ("completed" if success is True else "failed")
    return {
        "child_run_id": child_run_id, "skill_name": skill_name,
        "status": status, "mode": "ask_user" if paused else str(workflow_result.get("mode") or "execute"),
        "success": None if paused else bool(success), "completed": completed,
        "paused_for_user": paused, "reason": str(workflow_result.get("reason") or workflow_result.get("stderr") or ""),
        "missing": list(workflow_result.get("missing") or []), "text": str(final_text or workflow_result.get("text") or ""),
        "structured_outputs": manifest["structured_outputs"], "artifacts": manifest["artifacts"],
        "output_files": list(workflow_result.get("output_files") or []),
    }


async def execute_child_skill_runtime(*, skill_name: str, input_envelope: dict, child_run_id: str,
                                      model: str | None = None, event_sink=None,
                                      dataflow_plan: dict | None = None,
                                      adaptive_policy: dict | None = None) -> dict:
    """Assemble and invoke #463/#464; it never executes a child command itself."""
    record = resolve_skill_record(skill_name, mode="sandbox", require_visible=True, require_executable=True)
    root = Path(record["root_path"]).resolve()
    skill_file = root / "SKILL.md"
    if not skill_file.is_file():
        return build_child_runtime_result(workflow_result={"success": False, "stderr": "SKILL.md not found"},
            execution_root=root, child_run_id=child_run_id, skill_name=skill_name)
    skill_text = skill_file.read_text(encoding="utf-8", errors="replace")
    action_schema = _build_runtime_action_schema(skill_text, execution_root=root)
    if action_schema.get("errors"):
        return build_child_runtime_result(workflow_result={
            "success": False, "stderr": "Skill Action Schema validation failed",
            "protocol_errors": action_schema["errors"],
        }, execution_root=root, child_run_id=child_run_id, skill_name=skill_name)
    resources = _extract_runtime_resource_catalog(skill_text, execution_root=root)

    async def yield_func(event):
        if event_sink is not None:
            value = event_sink(event)
            if hasattr(value, "__await__"):
                await value

    request = ChatRequest(
        messages=[{"role": "user", "content": str(input_envelope.get("user_request") or "")}],
        model=model, input_files=deepcopy(input_envelope.get("input_files") or []),
    )
    try:
        workflow_result = await execute_skill_workflow(
            execution_root=root, action_schema=action_schema, user_context=input_envelope,
            request=request, skill_name=skill_name, dataflow_plan=dataflow_plan, model=model,
            yield_func=yield_func if event_sink else None, resource_catalog=resources,
            adaptive_policy=adaptive_policy,
        )
    except RuntimePlanError as exc:
        workflow_result = {"success": False, "stderr": str(exc), "protocol_error": exc.code}
    return build_child_runtime_result(workflow_result=workflow_result, execution_root=root,
        child_run_id=child_run_id, skill_name=skill_name)
