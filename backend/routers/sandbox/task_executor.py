"""单任务与批量任务执行。"""

import base64
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from ...config import settings
from ...services.kernel_loader import read_skill_resource_text
from ...services.skill_executor import build_skill_runtime_env
from ...services.artifact_validator import FileOutputValidationError, validate_stdout_file_outputs
from ..chat_utils import (
    _MAX_DEP_RETRY,
    _NODE_BUILTIN_MODULES,
    _expand_arg_env_vars,
    _extract_input_session_dir,
    _get_skill_venv_python,
    _has_creation_confirmation,
    _is_within_sandbox,
    _rewrite_argv_input_paths,
    _retry_install_node_dep,
    _retry_install_python_dep,
    _snapshot_dir_files,
    _validate_skill_md,
    _find_created_skill_roots,
    _correct_expanded_input_paths,
    _validate_input_file_paths,
)
from ..chat_models import ChatRequest, MarkdownBlock
from .path_resolution import (
    _resolve_planned_file_path,
    _infer_skill_root_from_tasks,
)
from .action_schema import (
    _validate_runtime_command_against_action_schema,
    _validate_runtime_asset_contract,
    _validate_stdout_against_action_entry,
)
from .command_executor import (
    _materialize_python_heredoc,
    _prepare_command_argv,
)
from .error_correction import (
    _output_files_from_stdout_json,
    _validate_html_asset_outputs_in_generated_dir,
)
from .stdout_render import _validate_success_stdout_json_if_structured

logger = logging.getLogger(__name__)


def _execute_single_task(
    task: dict,
    blocks: "list[MarkdownBlock]",
    request: "ChatRequest",
    *,
    execution_root: "Path | None" = None,
    inferred_skill_root: "Path | None" = None,
    skill_name: str = "",
    session_input_dir: "Path | None" = None,
    previous_output_files: "list[dict] | None" = None,
) -> "tuple[dict, list[Path]]":
    """Execute a single planned action task and return (result_dict, touched_paths).

    This is the per-task workhorse extracted from _execute_planned_actions so
    that callers (including the streaming execute loop in generate()) can run
    tasks one-at-a-time and observe results in real time.

    Returns:
        (result, touched) where *result* is the action result dict and
        *touched* is a (possibly empty) list of Path objects that were
        created or written during this task (used for post-loop validation).
    """
    if not isinstance(task, dict):
        return {}, []

    action = str(task.get("action") or "").strip()
    reason = str(task.get("reason") or "").strip()
    touched: list[Path] = []

    if action in {"display", "ignore"}:
        return {"action": action, "success": True, "reason": reason}, touched

    if action == "read_resource":
        rel_path = str(task.get("path") or "").strip()
        if not rel_path:
            raise ValueError("read_resource 任务缺少 path")
        if not skill_name and execution_root is None:
            raise ValueError("read_resource 任务缺少 skill_name 或 execution_root，无法确定读取哪个 Skill 的资源")

        # Validate sandbox boundary and file existence
        if execution_root is not None:
            resource_root = execution_root.resolve()
            resource_path = (resource_root / rel_path).resolve()
            if not _is_within_sandbox(resource_path, resource_root) or not resource_path.is_file():
                raise FileNotFoundError(f"read_resource resource does not exist in current skill: {rel_path}")
            if rel_path.startswith("assets/"):
                _validate_runtime_asset_contract(resource_path, root=resource_root)

        observation = read_skill_resource_text(
            skill_name, rel_path, max_chars=settings.skill_resource_max_chars
        )
        return {
            "action": action,
            "path": rel_path,
            "success": True,
            "content": observation.get("content", ""),
            "truncated": observation.get("truncated", False),
            "reason": reason,
        }, touched

    if action == "create_directory":
        raw_path = str(task.get("path") or "").strip()
        if not raw_path:
            raise ValueError("create_directory 任务缺少 path")
        path = _resolve_planned_file_path(
            raw_path,
            execution_root=execution_root,
            inferred_skill_root=inferred_skill_root,
        )
        path.mkdir(parents=True, exist_ok=True)
        touched.append(path)
        return {"action": action, "path": str(path), "success": True, "reason": reason}, touched

    if action == "write_file":
        raw_path = str(task.get("path") or "").strip()
        if not raw_path:
            raise ValueError("write_file 任务缺少 path")
        content = task.get("content", None)
        if content is None:
            block_index = int(task.get("block_index", -1))
            if 0 <= block_index < len(blocks):
                content = blocks[block_index].code
            else:
                raise ValueError("write_file 任务缺少 content，且没有合法 block_index")
        path = _resolve_planned_file_path(
            raw_path,
            execution_root=execution_root,
            inferred_skill_root=inferred_skill_root,
        )
        path.parent.mkdir(parents=True, exist_ok=True)

        file_encoding = str(task.get("encoding", "text")).strip().lower()
        if file_encoding == "base64":
            raw_bytes = base64.b64decode(str(content))
            path.write_bytes(raw_bytes)
            written_bytes = len(raw_bytes)
        else:
            path.write_text(str(content), encoding="utf-8")
            written_bytes = len(str(content).encode("utf-8"))

        touched.append(path)
        return {
            "action": action,
            "path": str(path),
            "success": True,
            "bytes": written_bytes,
            "reason": reason,
        }, touched

    if action == "run_command":
        command = str(task.get("command") or "").strip()
        if not command:
            raise ValueError("run_command 任务缺少 command")

        stdin_text = task.get("stdin", None)
        if stdin_text is not None:
            stdin_text = str(stdin_text)

        # Pre-execution: validate command against the Skill's declared Action schema
        action_entry = _validate_runtime_command_against_action_schema(command, execution_root=execution_root)

        cwd = execution_root or inferred_skill_root
        if cwd is not None and not cwd.exists():
            # Creator bootstrap commands may need to create the inferred Skill root.
            # Run those commands from the nearest existing parent instead of using
            # a not-yet-created cwd, while keeping later commands in the Skill root
            # once it exists.
            fallback_cwd = execution_root or cwd.parent
            if fallback_cwd.exists():
                cwd = fallback_cwd

        # Per-task snapshot taken *before* execution to detect new output files.
        pre_snapshot: set[str] = _snapshot_dir_files(cwd) if cwd else set()

        materialized = _materialize_python_heredoc(command)
        if materialized is not None:
            argv = materialized
            argv = _prepare_command_argv(
                " ".join(shlex.quote(part) for part in argv), base_dir=cwd
            )
        else:
            argv = _prepare_command_argv(command, base_dir=cwd)

        argv = _rewrite_argv_input_paths(
            argv,
            getattr(request, "input_files", []) or [],
            cwd,
            session_input_dir,
            previous_output_files=previous_output_files,
        )

        _run_cmd_extra_env: dict[str, str] = build_skill_runtime_env(
            execution_root=execution_root,
            session_input_dir=session_input_dir,
        )
        # Also expose cwd-based OUTPUT_DIR/INPUT_DIR for run_command tasks
        if cwd:
            _run_cmd_extra_env["OUTPUT_DIR"] = str(cwd / "outputs")
            _run_cmd_extra_env["INPUT_DIR"] = str(cwd / "inputs")

        _effective_env = {**os.environ, **_run_cmd_extra_env}
        argv = [_expand_arg_env_vars(arg, _effective_env) for arg in argv]

        # Correct placeholder file paths that the LLM may have used
        # (e.g., SKILL.md example filenames instead of real uploaded filenames)
        argv = _correct_expanded_input_paths(
            argv,
            input_files=getattr(request, "input_files", []) or [],
            execution_root=execution_root,
            session_input_dir=session_input_dir,
            previous_output_files=previous_output_files,
        )

        # Log warnings for any remaining non-existent input paths
        input_warnings = _validate_input_file_paths(argv, session_input_dir)
        for w in input_warnings:
            logger.warning("Sandbox input path warning: %s", w)

        # Error-driven retry: up to _MAX_DEP_RETRY times for missing deps.
        completed = None
        for _retry in range(_MAX_DEP_RETRY + 1):
            try:
                completed = subprocess.run(
                    argv,
                    shell=False,
                    input=stdin_text,
                    capture_output=True,
                    text=True,
                    timeout=settings.skill_command_timeout,
                    cwd=str(cwd) if cwd else None,
                    env={**os.environ, **_run_cmd_extra_env},
                )
            except FileNotFoundError as exc:
                raise ValueError(
                    "命令不可执行: " + command + "\n原因: " + str(exc)
                ) from exc
            except PermissionError as exc:
                raise ValueError(
                    "命令没有执行权限: " + command + "\n原因: " + str(exc)
                ) from exc

            if completed.returncode == 0 or _retry == _MAX_DEP_RETRY:
                break

            stderr = completed.stderr or ""
            retried = False

            py_missing = re.search(
                r"ModuleNotFoundError: No module named '([^']+)'", stderr
            )
            if py_missing and cwd is not None:
                module_name = py_missing.group(1).split(".")[0]
                try:
                    venv_python = _get_skill_venv_python(cwd)
                    if _retry_install_python_dep(module_name, venv_python):
                        retried = True
                except Exception as dep_exc:
                    logger.warning(
                        "skill-env: error-driven py dep install failed: %s", dep_exc
                    )

            node_missing = re.search(r"Cannot find module '([^']+)'", stderr)
            if node_missing and cwd is not None:
                raw_mod = node_missing.group(1)
                if raw_mod.startswith("@"):
                    parts = raw_mod.split("/")
                    module_name = "/".join(parts[:2]) if len(parts) >= 2 else parts[0]
                else:
                    module_name = raw_mod.split("/")[0]
                if module_name not in _NODE_BUILTIN_MODULES:
                    if _retry_install_node_dep(module_name, cwd):
                        retried = True

            if not retried and cwd is not None:
                chinese_missing = re.search(
                    r"缺少依赖[:：]\s*([^\n]+)",
                    (completed.stdout or "") + "\n" + stderr,
                )
                if chinese_missing:
                    raw_deps = chinese_missing.group(1)
                    pkg_list = [
                        p.strip()
                        for p in re.split(r"[,，、;；]\s*", raw_deps)
                        if p.strip()
                    ]
                    for dep in pkg_list:
                        if dep in _NODE_BUILTIN_MODULES:
                            continue
                        if (
                            dep.endswith(".js")
                            or (cwd / "node_modules").is_dir()
                            or shutil.which("node")
                        ):
                            if _retry_install_node_dep(dep, cwd):
                                retried = True
                        else:
                            try:
                                venv_python = _get_skill_venv_python(cwd)
                                if _retry_install_python_dep(dep, venv_python):
                                    retried = True
                            except Exception as dep_exc:
                                logger.warning(
                                    "skill-env: chinese dep install failed: %s", dep_exc
                                )

            if not retried:
                break

        assert completed is not None  # noqa: S101 – loop always runs at least once (range >= 1)
        success = completed.returncode == 0
        validation_error = ""
        validation_code = ""
        if success:
            try:
                _validate_success_stdout_json_if_structured(completed.stdout)
                _validate_stdout_against_action_entry(completed.stdout, action_entry)
                if cwd is not None:
                    validate_stdout_file_outputs(completed.stdout, skill_dir=cwd, cwd=cwd / "scripts")
                if action_entry and str(action_entry.get("role") or "") in {"html_asset_builder", "asset_builder"}:
                    _validate_html_asset_outputs_in_generated_dir(completed.stdout, cwd=cwd)
            except FileOutputValidationError as exc:
                success = False
                validation_error = str(exc)
                validation_code = exc.code
            except ValueError as exc:
                success = False
                validation_error = str(exc)

        result: dict = {
            "action": action,
            "command": command,
            "stdin_used": stdin_text is not None,
            "success": success,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": (completed.stderr or "") + ("\n" if completed.stderr and validation_error else "") + validation_error,
            "reason": reason,
        }
        if validation_error:
            result["message"] = validation_error
        if validation_code:
            result["error"] = validation_code

        # Detect newly created files and attach download metadata.
        effective_skill_name = skill_name or (cwd.name if cwd else "")
        if success and cwd and effective_skill_name:
            post_snapshot = _snapshot_dir_files(cwd)
            new_files = sorted(post_snapshot - pre_snapshot)
            if new_files:
                result["output_files"] = [
                    {
                        "path": f,
                        "url": f"/api/skills/{effective_skill_name}/files/{f}",
                    }
                    for f in new_files
                ]
            # Also extract output files declared in structured JSON stdout
            declared_output_files = _output_files_from_stdout_json(completed.stdout, cwd=cwd, skill_name=effective_skill_name)
            if declared_output_files:
                by_path = {item["path"]: item for item in result.get("output_files") or []}
                by_path.update({item["path"]: item for item in declared_output_files})
                result["output_files"] = list(by_path.values())

        return result, touched

    raise ValueError(f"不支持的规则动作: {action}")

def _execute_planned_actions(
    plan: dict,
    blocks: list[MarkdownBlock],
    request: ChatRequest,
    *,
    require_confirmation: bool = True,
    execution_root: Path | None = None,
    skill_name: str = "",
) -> dict:
    """执行结构化 action plan，并返回 executor observation。"""
    if require_confirmation and not _has_creation_confirmation(request):
        return {
            "executed": False,
            "reason": "未检测到用户明确确认开始创建，因此不会执行规划任务。",
            "plan": plan,
            "results": [],
            "logs": [],
        }

    inferred_skill_root = _infer_skill_root_from_tasks(
        plan,
        execution_root=execution_root,
    )

    # Pre-compute session input dir once (used for all run_command tasks).
    cwd_for_session = execution_root or inferred_skill_root
    session_input_dir = _extract_input_session_dir(
        getattr(request, "input_files", []) or [], cwd_for_session
    )

    touched: list[Path] = []
    results: list[dict] = []
    logs: list[str] = []
    accumulated_output_files: list[dict] = []  # output_files from all prior tasks

    for task in plan.get("tasks", []):
        if not isinstance(task, dict):
            continue

        action = str(task.get("action") or "").strip()

        result, task_touched = _execute_single_task(
            task,
            blocks,
            request,
            execution_root=execution_root,
            inferred_skill_root=inferred_skill_root,
            skill_name=skill_name,
            session_input_dir=session_input_dir,
            previous_output_files=accumulated_output_files or None,
        )

        touched.extend(task_touched)
        results.append(result)

        # Collect output files from this task for subsequent tasks to reference
        if result.get("output_files"):
            accumulated_output_files.extend(result["output_files"])

        # Build logs from the result dict.
        if action == "read_resource":
            logs.append(f"读取资源成功: {result.get('path')}")
        elif action == "create_directory":
            logs.append(f"创建目录: {result.get('path')}")
        elif action == "write_file":
            logs.append(f"写入文件: {result.get('path')}")
        elif action == "run_command":
            command = str(task.get("command") or "").strip()
            stdin_used = result.get("stdin_used", False)
            if result.get("output_files"):
                logs.append(
                    "新生成文件: " + ", ".join(f["path"] for f in result["output_files"])
                )
            if not result.get("success", True):
                logs.append(
                    f"执行命令失败: {command}\n"
                    f"returncode={result.get('returncode')}\n"
                    f"stdin_used={stdin_used}\n"
                    f"stderr: {(result.get('stderr') or '').strip()}\n"
                    f"stdout: {(result.get('stdout') or '').strip()}"
                )
            else:
                logs.append(
                    f"执行命令成功: {command}\n"
                    f"stdin_used={stdin_used}\n"
                    f"输出: {(result.get('stdout') or '').strip()}"
                )

    validation_logs: list[str] = []

    for root in _find_created_skill_roots(touched):
        skill_md = root / "SKILL.md"
        if skill_md.exists():
            _validate_skill_md(skill_md)
            validation_logs.append(f"校验通过: {skill_md}")

    logs.extend(validation_logs)

    # 汇总所有 run_command 任务产生的新文件
    all_output_files: list[dict] = []
    for r in results:
        all_output_files.extend(r.get("output_files") or [])

    return {
        "executed": bool(results or touched),
        "reason": "已根据结构化 action plan 执行任务。" if (results or touched) else "规划中没有需要执行的任务。",
        "plan": plan,
        "results": results,
        "logs": logs,
        "output_files": all_output_files,
        "touched_paths": [str(path) for path in touched],
    }


# Public alias
execute_single_task = _execute_single_task
