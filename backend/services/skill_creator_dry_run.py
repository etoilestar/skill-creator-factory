"""Creator-side workflow dry-run.

Run the generated scripts according to WorkflowContract before publishing a Skill.
This catches cross-step mismatch that single-file trial runs cannot catch.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .skill_contract import WorkflowContract, StepContract
from .skill_contract_validator import (
    ContractIssue,
    build_downstream_requirements,
    parse_stdout_json_object,
    validate_stdout_against_output_schema,
)


@dataclass
class DryRunStepTrace:
    step_id: str
    script_path: str
    command: str
    stdout: str
    stderr: str
    returncode: int
    payload: dict[str, Any] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DryRunResult:
    ok: bool
    context: dict[str, Any]
    traces: list[DryRunStepTrace]
    issues: list[dict[str, Any]]
    output_files: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "context": self.context,
            "traces": [
                {
                    "step_id": t.step_id,
                    "script_path": t.script_path,
                    "command": t.command,
                    "stdout": t.stdout,
                    "stderr": t.stderr,
                    "returncode": t.returncode,
                    "payload": t.payload,
                    "issues": t.issues,
                }
                for t in self.traces
            ],
            "issues": self.issues,
            "output_files": self.output_files,
        }


def run_creator_workflow_dry_run(
    *,
    skill_dir: Path,
    contract: WorkflowContract,
    sample_input: dict[str, Any] | None = None,
    python_executable: str | None = None,
    timeout_seconds: int = 120,
) -> DryRunResult:
    skill_dir = skill_dir.resolve()
    python_executable = python_executable or sys.executable
    context: dict[str, Any] = dict(sample_input or {})
    traces: list[DryRunStepTrace] = []
    all_issues: list[ContractIssue] = []
    output_files: list[str] = []
    downstream = build_downstream_requirements(contract)

    # Seed defaults.
    for step in contract.steps:
        for key, value in step.default_values.items():
            context.setdefault(key, value)
        for key, spec in step.inputs.items():
            if spec.default is not None:
                context.setdefault(key, spec.default)

    for index, step in enumerate(contract.steps):
        step_contexts = _materialize_step_contexts(step, context)

        for step_context in step_contexts:
            command = _render_command(step, step_context)
            completed = subprocess.run(
                command,
                cwd=str(skill_dir),
                shell=True,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                env={**os.environ},
            )

            trace = DryRunStepTrace(
                step_id=step.id,
                script_path=step.script_path,
                command=command,
                stdout=completed.stdout,
                stderr=completed.stderr,
                returncode=completed.returncode,
            )
            traces.append(trace)

            if completed.returncode != 0:
                issue = ContractIssue(
                    "dry_run_command_failed",
                    f"脚本返回非 0: {completed.returncode}",
                    step_id=step.id,
                    script_path=step.script_path,
                    details={"stderr": completed.stderr[-2000:]},
                )
                all_issues.append(issue)
                trace.issues.append(issue.to_dict())
                continue

            payload, parse_issue = parse_stdout_json_object(completed.stdout)
            if parse_issue:
                parse_issue.step_id = step.id
                parse_issue.script_path = step.script_path
                all_issues.append(parse_issue)
                trace.issues.append(parse_issue.to_dict())
                continue

            assert payload is not None
            trace.payload = payload

            issues = validate_stdout_against_output_schema(
                payload,
                step,
                execution_root=skill_dir,
                downstream_requirements=downstream.get(step.id) or {},
            )
            all_issues.extend(issues)
            trace.issues.extend([x.to_dict() for x in issues])

            _merge_payload(context, step, payload)
            _collect_outputs(context, step, payload)

            for value in payload.values():
                _collect_file_paths(value, skill_dir, output_files)

    return DryRunResult(
        ok=not any(issue.severity == "error" for issue in all_issues),
        context=context,
        traces=traces,
        issues=[x.to_dict() for x in all_issues],
        output_files=output_files,
    )


def _materialize_step_contexts(step: StepContract, context: dict[str, Any]) -> list[dict[str, Any]]:
    if not step.foreach:
        return [_resolve_inputs(step, context)]

    collection = _resolve_context_path(context, step.foreach.collection)
    if not isinstance(collection, list) or not collection:
        raise ValueError(f"foreach collection 不可展开: {step.foreach.collection}")

    out: list[dict[str, Any]] = []
    for idx, item in enumerate(collection):
        child = dict(context)
        child[step.foreach.item_name] = item
        child["loop_item"] = item
        child["loop_index"] = idx
        if isinstance(item, dict):
            child.update(item)
        out.append(_resolve_inputs(step, child))
    return out


def _resolve_inputs(step: StepContract, context: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, spec in step.inputs.items():
        if spec.source:
            payload[key] = _resolve_context_path(context, spec.source)
        elif key in context:
            payload[key] = context[key]
        elif key in step.default_values:
            payload[key] = step.default_values[key]
        elif spec.default is not None:
            payload[key] = spec.default
        elif spec.required:
            raise ValueError(f"缺少输入 {key} for {step.script_path}")
    merged = dict(context)
    merged.update(payload)
    return merged


def _render_command(step: StepContract, context: dict[str, Any]) -> str:
    if step.command_template:
        return _replace_placeholders(step.command_template, context)

    payload = {key: context.get(key) for key in step.inputs.keys()}
    return f"{shlex.quote(sys.executable)} {shlex.quote(step.script_path)} {shlex.quote(json.dumps(payload, ensure_ascii=False))}"


def _replace_placeholders(template: str, context: dict[str, Any]) -> str:
    import re

    def repl(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        value = _resolve_context_path(context, key)
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    return re.sub(r"\{\{\s*([^}]+)\s*\}\}", repl, template)


def _resolve_context_path(context: dict[str, Any], path: str) -> Any:
    path = path.strip()
    for prefix in ("context.", "user."):
        if path.startswith(prefix):
            path = path[len(prefix):]
    parts = [p for p in path.split(".") if p]
    value: Any = context
    for part in parts:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit():
            value = value[int(part)]
        else:
            raise KeyError(path)
    return value


def _merge_payload(context: dict[str, Any], step: StepContract, payload: dict[str, Any]) -> None:
    context.update(payload)
    context[step.id] = payload


def _collect_outputs(context: dict[str, Any], step: StepContract, payload: dict[str, Any]) -> None:
    for collect in step.collect:
        source = collect.source
        if source.startswith("each."):
            source = source[len("each."):]
        try:
            value = _resolve_context_path(payload, source)
        except Exception:
            continue

        if collect.type in {"array", "file_paths"}:
            bucket = context.setdefault(collect.target, [])
            if not isinstance(bucket, list):
                bucket = []
                context[collect.target] = bucket
            if isinstance(value, list):
                bucket.extend(value)
            else:
                bucket.append(value)
        else:
            context[collect.target] = value


def _collect_file_paths(value: Any, skill_dir: Path, output_files: list[str]) -> None:
    if isinstance(value, str):
        p = Path(value)
        candidates = [p, skill_dir / p] if not p.is_absolute() else [p]
        for c in candidates:
            if c.is_file():
                rel = str(c.relative_to(skill_dir)) if c.is_relative_to(skill_dir) else str(c)
                if rel not in output_files:
                    output_files.append(rel)
                return
    elif isinstance(value, list):
        for item in value:
            _collect_file_paths(item, skill_dir, output_files)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_file_paths(item, skill_dir, output_files)
