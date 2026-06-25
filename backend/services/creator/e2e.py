"""E2E workflow validation, script static checks, and trial-run helpers."""

from .common import *  # noqa: F403
from . import common as _common

globals().update({k: v for k, v in _common.__dict__.items() if not k.startswith("__")})

def validate_workflow_e2e(
    skill_name: str,
    *,
    external_context: dict[str, Any] | None = None,
    source_skill_dir: Path | None = None,
    requested_model: str | None = None,
) -> list[str]:
    """Second-round Creator validator.

    第二轮负责：
    - SKILL.md workflow 能否真实执行；
    - 上下游 JSON 字段能否串起来；
    - 当前 step 接口是否对齐；
    - 最后一步 stdout 是否符合现有 sandbox 平台协议；
    - artifact 是否真实存在并基础合法。

    不写业务字段词表。
    不重新做第一轮责任审查。
    """

    return _run_skill_workflow_e2e_once(
        skill_name,
        external_context=external_context,
        source_skill_dir=source_skill_dir,
        requested_model=requested_model,
    )

def _run_e2e_sandbox_acceptance_gate(
    *,
    skill_name: str,
    candidate_skill_dir: Path,
    patched_file: str,
    original_errors: list[str],
    external_context: dict[str, Any] | None = None,
    requested_model: str | None = None,
) -> dict[str, Any]:
    """Second-round E2E acceptance gate.

    E2E 是否通过，必须由真实沙盒试运行决定。
    不写平台字段词表。
    """

    candidate_skill_dir = candidate_skill_dir.resolve()

    if not candidate_skill_dir.is_dir():
        return {
            "accepted": False,
            "phase": "e2e_sandbox",
            "patched_file": patched_file,
            "reason": f"candidate skill dir 不存在：{candidate_skill_dir}",
            "errors": [f"candidate skill dir 不存在：{candidate_skill_dir}"],
            "original_errors": original_errors,
        }

    errors = _run_skill_workflow_e2e_once(
        skill_name,
        external_context=external_context,
        source_skill_dir=candidate_skill_dir,
        requested_model=requested_model,
    )

    if errors:
        return {
            "accepted": False,
            "phase": "e2e_sandbox",
            "patched_file": patched_file,
            "reason": "candidate 在简单沙盒 E2E 试运行中失败。",
            "errors": errors,
            "original_errors": original_errors,
        }

    return {
        "accepted": True,
        "phase": "e2e_sandbox",
        "patched_file": patched_file,
        "reason": "candidate 已通过现有 sandbox/E2E workflow 试运行。",
        "errors": [],
        "original_errors": original_errors,
    }

def _raise_file_contract_failures(results: list[ContractCheckResult]) -> None:
    failed = [result for result in results if not result.passed]
    if failed:
        raise ContractValidationError(
            "文件级合同校验未通过：\n" + _format_contract_checks(results, passed=False),
            results,
        )

def _script_paths_in_shell_fenced_blocks(skill_md: str) -> set[str]:
    """Return scripts/*.py paths that appear inside shell fenced blocks."""
    paths: set[str] = set()

    for info, body in _iter_markdown_fenced_blocks(skill_md):
        if not _is_shell_fence_info(info):
            continue

        for match in re.finditer(
            r"(?<![\w./-])(scripts/[A-Za-z0-9_./-]+\.py)(?![\w./-])",
            body.replace("\\", "/"),
        ):
            paths.add(match.group(1))

    return paths


def _script_paths_outside_shell_fenced_blocks(skill_md: str) -> set[str]:
    """Return scripts/*.py paths mentioned outside shell fenced blocks.

    This is not used to decide whether a script is part of the blueprint.
    It only catches a bad SKILL.md style:
    mentioning scripts/foo.py in prose without an executable ```bash block.
    """
    text = skill_md or ""

    shell_block_bodies: list[str] = []
    for info, body in _iter_markdown_fenced_blocks(text):
        if _is_shell_fence_info(info):
            shell_block_bodies.append(body)

    text_without_shell_blocks = text
    for body in shell_block_bodies:
        text_without_shell_blocks = text_without_shell_blocks.replace(body, "")

    paths: set[str] = set()
    for match in re.finditer(
        r"(?<![\w./-])(scripts/[A-Za-z0-9_./-]+\.py)(?![\w./-])",
        text_without_shell_blocks.replace("\\", "/"),
    ):
        paths.add(match.group(1))

    return paths


def _validate_command_is_single_shell_json_invocation(
    *,
    command: str,
    script_path: str,
    entry: SkillPlanEntry,
    upstream_available_outputs: set[str] | None = None,
) -> list[ContractCheckResult]:
    """Validate one shell fenced command under flexible command protocol.

    C 方案：
    - 阻断：不是单行命令；
    - 阻断：不能解析成 python/python3 调用 scripts/*.py；
    - 阻断：如果它看起来使用 JSON argv，但 JSON 不合法；
    - 不阻断：使用 argparse flags、普通位置参数、无参数。
    """
    results: list[ContractCheckResult] = []
    target = script_path

    raw_command = command or ""
    lines = [line.strip() for line in raw_command.strip().splitlines() if line.strip()]
    one_line = len(lines) == 1

    results.append(ContractCheckResult(
        id="skill_md.command_block.single_command",
        passed=one_line,
        target=target,
        message=(
            f"{script_path} 命令块只包含一条命令。"
            if one_line
            else f"{script_path} 命令块应只包含一条命令，不要在一个 block 里写多条命令或解释。"
        ),
        expected="每个 ```bash fenced block 内只放一条真实 shell 命令。",
        minimal_edit="把解释移出 fenced block；一个 block 只保留一条调用 scripts/*.py 的 shell 命令。",
    ))

    if not one_line:
        return results

    command_line = lines[0]

    try:
        command_sig = _command_signature(command_line, script_path)
    except Exception as exc:
        logger.warning(
            "[Creator][skill_md] command signature parser crashed script=%s command=%s error=%s",
            script_path,
            command_line,
            exc,
        )
        command_sig = None

    parsed_ok = command_sig is not None

    results.append(ContractCheckResult(
        id="skill_md.command_block.signature_parseable",
        passed=parsed_ok,
        target=target,
        message=(
            f"{script_path} 命令块可解析为真实 scripts/*.py shell 调用。"
            if parsed_ok
            else f"{script_path} 命令块无法解析为真实 scripts/*.py shell 调用。"
        ),
        expected=(
            "命令应是一条真实 shell 命令，并直接调用 scripts/*.py。"
            "参数形态由脚本真实接口决定，可以是 JSON argv，也可以是 argparse flags。"
        ),
        minimal_edit=(
            f"改为调用真实脚本的 shell 命令，例如：python {script_path} '<JSON object>' "
            f"或 python {script_path} --arg value。具体参数由脚本接口决定。"
        ),
    ))

    if not command_sig:
        return results

    arg_mode = str(command_sig.get("arg_mode") or "")
    args = list(command_sig.get("args") or [])

    # 只有“看起来想用 JSON argv 但 JSON 坏了”的情况才阻断。
    # argparse flags / no_args / positional_args 不在第一轮误杀。
    json_arg_ok = arg_mode != "invalid_json_arg"

    results.append(ContractCheckResult(
        id="skill_md.command_block.args_parseable",
        passed=json_arg_ok,
        target=target,
        message=(
            f"{script_path} 命令参数形态可接受：{arg_mode or 'unknown'}。"
            if json_arg_ok
            else f"{script_path} 看起来使用 JSON argv，但 JSON 无法解析。"
        ),
        expected=(
            "如果使用 JSON argv，则脚本路径后传一个 json.loads 可解析的 JSON object；"
            "如果脚本使用 argparse，则使用该脚本声明的 flags。"
        ),
        minimal_edit=(
            "只修当前命令参数。不要固定套用 payload/user_request/fields/options/input_files。"
        ),
        details={
            "arg_mode": arg_mode,
            "args": args,
        },
    ))

    try:
        runtime_matches = _command_runtime_matches(command_line, script_path, entry)
    except Exception as exc:
        runtime_matches = False
        logger.warning(
            "[Creator][skill_md] runtime match check crashed script=%s command=%s error=%s",
            script_path,
            command_line,
            exc,
        )

    if not runtime_matches:
        logger.info(
            "[Creator][skill_md] non-blocking runtime mismatch script=%s inferred_runtime=%s command=%s",
            script_path,
            getattr(entry, "runtime", ""),
            command_line,
        )

    return results


def _check_skill_md_fenced_command_contracts(
    *,
    content: str,
    blueprint_text: str,
    required_script_paths: list[str] | None = None,
) -> list[ContractCheckResult]:
    """Validate fenced command style for SKILL.md.

    这里是格式/可解析性校验，不做蓝图语义判断。
    任何内部异常都转换成 ContractCheckResult，避免直接崩溃。
    """
    results: list[ContractCheckResult] = []

    required = {
        path.replace("\\", "/").strip()
        for path in (required_script_paths or [])
        if isinstance(path, str) and path.replace("\\", "/").strip().startswith("scripts/")
    }

    mentioned = {
        path
        for path in _skill_local_paths_in_markdown(content)
        if isinstance(path, str) and path.startswith("scripts/")
    }

    scripts_to_check = sorted(required or mentioned)

    entries_by_path: dict[str, SkillPlanEntry] = {}
    try:
        parsed = parse_blueprint([{"role": "assistant", "content": blueprint_text}])
        if parsed.skill_plan:
            entries_by_path = {
                entry.path: entry
                for entry in parsed.skill_plan.files
                if entry.file_type == "script"
            }
    except Exception as exc:
        logger.warning("[Creator][skill_md] failed to parse blueprint SkillPlan for command validation: %s", exc)

    if entries_by_path:
        scripts_to_check = [entry.path for entry in entries_by_path.values() if entry.path in scripts_to_check]

    prior_outputs: set[str] = set()

    for script_path in scripts_to_check:
        try:
            commands = _extract_script_command_templates(content, script_path)
        except Exception as exc:
            results.append(ContractCheckResult(
                id="skill_md.command_block.extract_crashed",
                passed=False,
                target=script_path,
                message=f"{script_path} 命令块提取失败：{exc}",
                expected="能够从 SKILL.md 中提取该脚本对应的标准 ```bash fenced code block。",
                minimal_edit=(
                    f"为 {script_path} 添加独立、无缩进的标准命令块，例如：\n"
                    f"```bash\npython {script_path} '{{\"arg_name\":\"arg_value_or_placeholder\"}}'\n```"
                ),
            ))
            continue

        has_fenced = bool(commands)

        results.append(ContractCheckResult(
            id="skill_md.command_block.fenced_exists",
            passed=has_fenced,
            target=script_path,
            message=(
                f"{script_path} 已使用 ```bash fenced code block 表达可执行命令。"
                if has_fenced
                else f"{script_path} 缺少可执行 Markdown 命令块：标准 ```bash fenced code block。"
            ),
            expected=(
                "真实脚本必须用标准 Markdown fenced code block 表示，例如：\n"
                f"```bash\npython {script_path} '{{\"arg_name\":\"arg_value_or_placeholder\"}}'\n```"
            ),
            minimal_edit=(
                f"为 {script_path} 添加独立、无缩进的 ```bash fenced block；"
                "不要只在正文中写“调用脚本”。"
            ),
        ))

        if not commands:
            continue

        try:
            entry = entries_by_path.get(script_path) or _skill_plan_entry_for_file(
                file_path=script_path,
                blueprint_text=blueprint_text,
            )
        except Exception as exc:
            logger.warning(
                "[Creator][skill_md] failed to infer SkillPlanEntry for %s: %s",
                script_path,
                exc,
            )
            entry = SkillPlanEntry(
                path=script_path,
                role="generic_script",
                file_type="python",
                purpose="Inferred fallback entry for command validation.",
                runtime="python",
                inputs=[],
                outputs=[],
                dependencies=[],
            )

        for command in commands:
            try:
                results.extend(_validate_command_is_single_shell_json_invocation(
                    command=command,
                    script_path=script_path,
                    entry=entry,
                    upstream_available_outputs=prior_outputs,
                ))
            except Exception as exc:
                logger.exception(
                    "[Creator][skill_md] command validation crashed script=%s command=%s",
                    script_path,
                    command,
                )
                results.append(ContractCheckResult(
                    id="skill_md.command_block.validation_crashed",
                    passed=False,
                    target=script_path,
                    message=f"{script_path} 命令块校验内部异常：{exc}",
                    expected="命令块应能被解析为 runner + scripts 路径 + JSON object argv。",
                    minimal_edit=(
                        f"将命令改为标准形式：\n"
                        f"```bash\npython {script_path} '{{\"arg_name\":\"arg_value_or_placeholder\"}}'\n```"
                    ),
                ))

        prior_outputs.update(entry.outputs or [])

    return results

def _basic_markdown_format_failures(file_path: str, content: str, *, require_frontmatter: bool) -> list[dict[str, Any]]:
    """Very small Markdown format gate.

    只检查最基础格式：
    1. frontmatter 是否存在；
    2. frontmatter 是否有收尾 ---；
    3. frontmatter YAML 是否能解析成 dict；
    4. fenced block 数量是否成对。

    不检查内容责任，不检查蓝图一致性。
    """
    text = (content or "").lstrip("\ufeff")
    failures: list[dict[str, Any]] = []

    if require_frontmatter:
        if not text.startswith("---"):
            failures.append({
                "id": "markdown.frontmatter.missing",
                "source": "markdown_format",
                "target": file_path,
                "layer": "markdown_format",
                "message": f"{file_path} 缺少 YAML frontmatter。",
                "expected": "文件必须以 --- 开始，并在正文前用单独一行 --- 闭合。",
                "minimal_edit": "只在文件开头补齐 YAML frontmatter。",
            })
            return failures

        lines = text.splitlines(keepends=True)
        close_idx = None
        for idx in range(1, len(lines)):
            if lines[idx].strip() == "---":
                close_idx = idx
                break

        if close_idx is None:
            failures.append({
                "id": "markdown.frontmatter.unclosed",
                "source": "markdown_format",
                "target": file_path,
                "layer": "markdown_format",
                "message": f"{file_path} frontmatter 只有开头 ---，没有收尾 ---。",
                "expected": "frontmatter 必须在正文前用单独一行 --- 闭合。",
                "minimal_edit": "只在正文第一个标题或正文开始前补一行 ---，不要修改正文内容。",
            })
            return failures

        raw_yaml = "".join(lines[1:close_idx])
        try:
            parsed = yaml.safe_load(raw_yaml) or {}
            if not isinstance(parsed, dict):
                failures.append({
                    "id": "markdown.frontmatter.not_object",
                    "source": "markdown_format",
                    "target": file_path,
                    "layer": "markdown_format",
                    "message": f"{file_path} frontmatter 必须是 YAML object。",
                    "expected": "frontmatter 应是 key/value YAML object。",
                    "minimal_edit": "只修 YAML frontmatter 内容，不要修改正文。",
                })
        except Exception as exc:
            failures.append({
                "id": "markdown.frontmatter.invalid_yaml",
                "source": "markdown_format",
                "target": file_path,
                "layer": "markdown_format",
                "message": f"{file_path} frontmatter YAML 无法解析：{type(exc).__name__}: {exc}",
                "expected": "frontmatter 必须是合法 YAML。",
                "minimal_edit": "只修 YAML frontmatter 内容，不要修改正文。",
            })

    fence_count = len(re.findall(r"(?m)^\s*(```|~~~)", text))
    if fence_count % 2 != 0:
        failures.append({
            "id": "markdown.fences_unbalanced",
            "source": "markdown_format",
            "target": file_path,
            "layer": "markdown_format",
            "message": f"{file_path} 存在未闭合的 Markdown fenced block。",
            "expected": "所有 ``` 或 ~~~ fenced block 必须成对闭合。",
            "minimal_edit": "只补齐或删除多余的 fenced block 标记。",
            "details": {"fence_count": fence_count},
        })

    return failures

def _extract_script_command_templates(skill_md: str, script_path: str) -> list[str]:
    """Return shell command templates in SKILL.md that invoke script_path."""
    commands: list[str] = []
    normalized_script_path = script_path.replace("\\", "/")

    for info, body in _iter_markdown_fenced_blocks(skill_md):
        if not _is_shell_fence_info(info):
            continue

        command = body.strip()
        if not command:
            continue

        normalized_command = command.replace("\\", "/")
        if normalized_script_path in normalized_command:
            commands.append(command)

    return commands


def _command_uses_json_argv(command: str) -> bool:
    return "{" in command and "}" in command


def _script_reads_json_argv(content: str, runtime: str = "python") -> bool:
    if runtime == "node":
        return "JSON.parse" in content and "process.argv" in content
    if runtime in {"bash", "shell"}:
        return "$1" in content or "${1" in content or "jq" in content
    return "json.loads" in content and "sys.argv" in content


def _script_uses_input_keys(content: str, keys: list[str]) -> tuple[bool, list[str]]:
    missing = [key for key in keys if key not in content]
    return not missing, missing


def _script_has_main_entry(content: str, runtime: str) -> bool:
    if runtime == "python":
        return "def main" in content and "__main__" in content
    if runtime == "node":
        return "process.argv" in content and "console.log" in content
    if runtime in {"bash", "shell"}:
        return ("$1" in content or "${1" in content) and ("echo" in content or "printf" in content or "print(json.dumps" in content)
    return True


def _validate_script_contract_static(
    *,
    file_path: str,
    content: str,
    skill_md: str,
    skill_plan_entry: dict[str, Any] | SkillPlanEntry | None = None,
) -> None:
    """Validate script source against SKILL.md contract locally.

    Creator 单文件阶段只做“协议 + 运行 + 产物”中的静态协议部分：
    - 如果 SKILL.md 命令传入 JSON argv，脚本必须读取 JSON argv；
    - 不用 fake/mock/template 关键词、工具能力声明、helper 路线或 LLM
      validator 作为 hard gate；
    - 字段级 stdout/artifact 闭环交给单文件 trial run 和最终 E2E。
    """
    explicit_entry = (
        skill_plan_entry.__dict__
        if isinstance(skill_plan_entry, SkillPlanEntry)
        else skill_plan_entry
    )
    plan_entry = (
        _skill_plan_entry_for_file(file_path=file_path, skill_plan_entry=explicit_entry)
        if explicit_entry is not None
        else _skill_plan_entry_for_file(file_path=file_path, blueprint_text=skill_md)
    )
    commands = _extract_script_command_templates(skill_md, file_path)
    if not commands:
        return

    command_results = _check_command_block_contract(file_path, commands, plan_entry)
    failed_command_results = [r for r in command_results if not r.passed]
    if failed_command_results:
        raise ValueError(
            "SKILL.md 命令块不合法，属于 workflow 局部合同问题，不要改脚本字段强制对齐 SkillPlan:\n"
            + _format_contract_checks(failed_command_results, passed=False)
        )

    json_argv_commands = [c for c in commands if _command_uses_json_argv(c)]
    if json_argv_commands and not _script_reads_json_argv(content, plan_entry.runtime):
        raise ValueError(
            f"{file_path} SKILL.md 命令传入 JSON argv，但脚本未按 runtime 读取 JSON argv（例如 Python json.loads(sys.argv[1])）。"
        )



def _validate_script_against_existing_skill_contract(skill_name: str, file_path: str, content: str) -> None:
    """Refuse saving scripts that do not match the current SKILL.md contract."""
    if not file_path.startswith("scripts/"):
        return
    skill_md_path = settings.skills_path / skill_name / "SKILL.md"
    if not skill_md_path.is_file():
        return
    skill_md = skill_md_path.read_text(encoding="utf-8")
    _validate_script_contract_static(file_path=file_path, content=content, skill_md=skill_md)


def _format_script_functional_issues(issues: list[dict[str, Any]]) -> str:
    lines = ["script_functional 校验未通过："]
    for issue in issues:
        lines.append(
            "- {id}\n"
            "  failed_file: {failed_file}\n"
            "  failed_function: {failed_function}\n"
            "  code_region: {code_region}\n"
            "  reason: {reason}\n"
            "  minimal_edit: {minimal_edit}\n"
            "  allowed_scope: {allowed_scope}\n"
            "  forbidden_scope: {forbidden_scope}".format(
                id=issue.get("id", "script_functional.unknown"),
                failed_file=issue.get("failed_file", ""),
                failed_function=issue.get("failed_function", ""),
                code_region=issue.get("code_region", ""),
                reason=issue.get("reason", ""),
                minimal_edit=issue.get("minimal_edit", ""),
                allowed_scope=issue.get("allowed_scope", ""),
                forbidden_scope=issue.get("forbidden_scope", ""),
            )
        )
    return "\n".join(lines)


class ScriptFunctionalValidationError(ValueError):
    """First-round responsibility/evidence failure after script smoke passed."""

    def __init__(self, issues: list[dict[str, Any]], *, layer: str = "responsibility"):
        super().__init__(_format_script_functional_issues(issues))
        self.issues = issues
        self.layer = layer


def _stage_error_for_script_functional(exc: Exception):
    layer = getattr(exc, "layer", None) or _failure_layer_from_error_text(str(exc)) or "responsibility"
    return FileGenerationStageError(
        source="script_functional",
        layer=layer,
        detail=str(exc),
        original=exc,
    )


def _stdout_artifact_paths(payload: dict[str, Any], entry: SkillPlanEntry, canonical_contract: Any | None = None) -> list[str]:
    fields = _artifact_fields_for_entry(entry, canonical_contract)
    fields.extend([
        "image_path", "pdf_path", "docx_path", "pptx_path", "html_path",
        "image_paths", "file_paths", "file_outputs",
    ])
    paths: list[str] = []
    for field_name in dict.fromkeys(fields):
        value = payload.get(field_name)
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, str) and item.strip():
                paths.append(item.strip())
    return list(dict.fromkeys(paths))


def _resolve_trial_artifact_path(skill_dir: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate
    if raw_path.startswith("outputs/"):
        return (skill_dir / candidate).resolve()
    return (skill_dir / "scripts" / candidate).resolve()


def _validate_artifact_content_evidence(
    *,
    stdout_payload: dict[str, Any],
    argv_payload: dict[str, Any],
    entry: SkillPlanEntry,
    skill_dir: Path,
    canonical_contract: Any | None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    artifact_paths = _stdout_artifact_paths(stdout_payload, entry, canonical_contract)
    input_strings = [
        text.strip()
        for text in _flatten_json_strings(argv_payload)
        if len(text.strip()) >= 8
    ]
    for raw_path in artifact_paths:
        path = _resolve_trial_artifact_path(skill_dir, raw_path)
        if not path.is_file():
            issues.append({
                "id": "script_functional.artifact_evidence.missing",
                "failed_file": entry.path,
                "failed_function": "artifact/file output generation",
                "code_region": "file write path and stdout artifact field",
                "reason": f"stdout 声明产物 {raw_path!r}，但试运行目录中不存在该文件。",
                "minimal_edit": "只修当前脚本的产物写入路径和 stdout 路径映射，确保返回的文件真实存在。",
                "allowed_scope": entry.path,
                "forbidden_scope": "不得改 SKILL.md、其它脚本或 SkillPlan；不得返回不存在路径。",
            })
            continue
        size = path.stat().st_size
        if size <= 0:
            issues.append({
                "id": "script_functional.artifact_evidence.empty",
                "failed_file": entry.path,
                "failed_function": "artifact/file output generation",
                "code_region": "file content write",
                "reason": f"产物 {raw_path!r} 已生成但文件为空。",
                "minimal_edit": "只修当前脚本的文件内容生成逻辑，让产物写入真实内容。",
                "allowed_scope": entry.path,
                "forbidden_scope": "不得输出空文件或占位文件骗过路径校验。",
            })
        if path.suffix.lower() == ".pdf":
            try:
                header = path.read_bytes()[:5]
            except OSError:
                header = b""
            if header != b"%PDF-":
                issues.append({
                    "id": "script_functional.artifact_evidence.pdf_header",
                    "failed_file": entry.path,
                    "failed_function": "pdf artifact generation",
                    "code_region": "PDF writer/output file creation",
                    "reason": f"产物 {raw_path!r} 不是有效 PDF 文件头。",
                    "minimal_edit": "只修当前脚本 PDF 写入逻辑，确保生成真实 PDF，而不是普通文本或空占位文件。",
                    "allowed_scope": entry.path,
                    "forbidden_scope": "不得改产物字段协议或返回假 PDF 路径。",
                })
        elif path.suffix.lower() in {".txt", ".md", ".json", ".html", ".htm", ".csv"} and input_strings:
            try:
                artifact_text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                artifact_text = ""
            if artifact_text and not any(text in artifact_text for text in input_strings[:10]):
                issues.append({
                    "id": "script_functional.artifact_evidence.input_dependency",
                    "failed_file": entry.path,
                    "failed_function": "artifact content assembly",
                    "code_region": "input processing through artifact write",
                    "reason": f"文本类产物 {raw_path!r} 没有体现试运行输入内容，可能未消费 argv JSON。",
                    "minimal_edit": "只修当前脚本产物内容组织逻辑，让文本类产物真实依赖输入或工具/模型结果。",
                    "allowed_scope": entry.path,
                    "forbidden_scope": "不得返回固定模板、mock 内容或空壳产物。",
                })
    return issues


def _flatten_json_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for item in value.values():
            out.extend(_flatten_json_strings(item))
        return out
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_flatten_json_strings(item))
        return out
    return []



def _html_output_candidates(payload: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    html_path = payload.get("html_path")
    if isinstance(html_path, str) and html_path.strip():
        candidates.append(html_path.strip())
    for key in ("file_paths", "file_outputs"):
        paths = payload.get(key)
        if isinstance(paths, list):
            candidates.extend(path.strip() for path in paths if isinstance(path, str) and path.strip())
    return candidates


_LEGACY_OUTPUT_ALIASES: dict[str, tuple[str, ...]] = {}


def _payload_has_declared_output(payload: dict[str, Any], output_key: str) -> bool:
    """New Creator E2E requires exact declared output fields; no alias guessing."""
    return output_key in payload


def _payload_output_value(payload: dict[str, Any], output_key: str) -> Any:
    """Return exact declared output value only; aliases belong in migration/warnings."""
    return payload.get(output_key) if output_key in payload else None

def _json_value_non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return any(_json_value_non_empty(item) for item in value)
    if isinstance(value, dict):
        return any(_json_value_non_empty(item) for item in value.values())
    return True


def _validate_trial_stdout_json(*, stdout: str, content: str, args: list[str], role: str | None = None, skill_dir: Path | None = None, skill_plan_entry: dict[str, Any] | None = None, canonical_contract: Any | None = None) -> None:
    """Validate trial stdout with dynamic, field-name-agnostic rules.

    SkillPlan.outputs is a blueprint hint, not the sole runtime contract.  The
    hard requirements here are: stdout is a JSON object, it has at least one
    non-empty value, it does not report an error, and any file-looking values it
    declares point at real files.
    """
    stripped = (stdout or "").strip()
    if not stripped:
        raise ValueError(f"脚本试运行 stdout 为空：argv={args!r}")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"脚本试运行 stdout 不是合法 JSON object：argv={args!r} stdout={stripped[-4000:]}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"脚本试运行 stdout 必须是 JSON object：argv={args!r} stdout={stripped[-4000:]}")
    if "error" in payload:
        raise ValueError(f"脚本试运行 stdout JSON 不得包含 error 字段：argv={args!r} stdout={stripped[-4000:]}")
    if not any(_json_value_non_empty(value) for value in payload.values()):
        raise ValueError(f"脚本试运行 stdout JSON 至少需要一个非空字段：argv={args!r} stdout={stripped[-4000:]}")
    if canonical_contract is not None:
        stdout_schema = getattr(canonical_contract, "stdout_schema", {}) or {}
        required = stdout_schema.get("required") if isinstance(stdout_schema, dict) else []
        missing = [str(key) for key in required or [] if str(key) not in payload or not _json_value_non_empty(payload.get(str(key)))]
        if missing:
            raise ValueError(
                "stdout_required_outputs_missing: 当前脚本 stdout 缺少 required_outputs。"
                f" missing={missing!r} actual={list(payload.keys())!r} required={list(required or [])!r} "
                f"argv={args!r} stdout={stripped[-4000:]}"
            )
    elif skill_plan_entry is not None:
        entry = _skill_plan_entry_for_file(file_path=str((skill_plan_entry or {}).get("path") or "scripts/main.py"), skill_plan_entry=skill_plan_entry)
        stdout_schema = _script_stdout_schema_for_entry(entry)
        required = stdout_schema.get("required") if isinstance(stdout_schema, dict) else []
        missing = [str(key) for key in required or [] if str(key) not in payload or not _json_value_non_empty(payload.get(str(key)))]
        if missing:
            raise ValueError(
                "stdout_required_outputs_missing: 当前脚本 stdout 缺少 required_outputs。"
                f" missing={missing!r} actual={list(payload.keys())!r} required={list(required or [])!r} "
                f"argv={args!r} stdout={stripped[-4000:]}"
            )

    try:
        if skill_dir is not None:
            validate_stdout_file_outputs(stripped, skill_dir=skill_dir, cwd=skill_dir / "scripts")
    except FileOutputValidationError as exc:
        raise ValueError(str(exc)) from exc



def _install_capability_dependencies(venv_python: Path, required_capabilities: list[str]) -> None:
    """Install platform-owned runtime dependencies for required capabilities.

    Creator trial runs execute generated scripts in a per-skill venv.  Scripts
    commonly import only ``backend.services.skill_runtime`` while the helper
    itself lazy-imports optional runtime dependencies. Static script import
    scanning cannot see those helper internals, so install the
    dependencies declared by the Creator tool registry before execution.
    """
    dependencies: list[str] = []
    seen: set[str] = set()
    for capability_name in required_capabilities or []:
        capability = get_tool_capability(capability_name)
        if capability is None:
            continue
        for dependency in capability.dependencies or []:
            package = str(dependency.get("package") or dependency.get("name") or "") if isinstance(dependency, dict) else str(dependency or "")
            if package and package not in seen:
                seen.add(package)
                dependencies.append(package)

    _install_declared_dependency_packages(venv_python, dependencies, source_label="capability")


def _install_declared_dependency_packages(venv_python: Path, dependencies: list[str], *, source_label: str = "declared") -> None:
    dependencies = [str(item).strip() for item in dependencies or [] if str(item).strip()]
    if not dependencies:
        return
    missing: list[str] = []
    dependency_import_names = {"python-docx": "docx", "python-pptx": "pptx"}
    for dependency in dependencies:
        module_name = dependency_import_names.get(dependency, dependency).replace("-", "_")
        check = subprocess.run(
            [
                str(venv_python),
                "-c",
                "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec(sys.argv[1]) is not None else 1)",
                module_name,
            ],
            capture_output=True,
            timeout=10,
        )
        if check.returncode != 0:
            missing.append(dependency)

    if not missing:
        return

    logger.info("skill-env: pip installing %s deps into venv: %s", source_label, missing)
    result = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--quiet", *missing],
        timeout=180,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"安装 {source_label} 依赖失败 ({', '.join(missing)}): {result.stderr[:500]}")


def _contract_resolution_for_trial(file_path: str, skill_md: str, role: str | None, skill_plan_entry: dict[str, Any] | None) -> tuple[Any, Any]:
    entry = _skill_plan_entry_for_file(
        file_path=file_path,
        blueprint_text=skill_md,
        role=role,
        skill_plan_entry=skill_plan_entry,
    )
    stdout_schema = _script_stdout_schema_for_entry(entry)
    contract = compile_canonical_file_contract(entry, stdout_schema)
    resolution = resolve_implementation(entry, contract)
    refined_contract = refine_contract_with_resolution(contract, resolution)
    resolution = resolve_implementation(entry, refined_contract)
    return refined_contract, resolution




def _artifact_fields_from_contract_dict(contract: dict[str, Any] | None) -> list[str]:
    """Return only fields that are semantically artifact/file outputs.

    Creator 中需要严格区分两类字段：

    1. stdout data fields:
       普通 stdout JSON 业务字段，只表示脚本输出的数据结构。
       例如任何业务字段、结构化内容字段、统计字段、描述字段等。
       这些字段只应接受 JSON required/non-empty/type/provenance 校验，
       不能被当作文件路径检查。

    2. artifact/file fields:
       文件产物路径字段，字段值应指向真实生成的文件。
       这些字段才进入 artifact existence/content evidence 校验。

    因此：
    - artifact_fields / file_fields / file_outputs 是显式文件产物字段，可以直接采纳。
    - artifact_outputs 是显式 artifact manifest，也可以采纳。
    - stdout_fields / output_fields 只是普通 stdout 字段集合，不能整体提升为 artifact。
      只有字段名本身具备 runtime artifact 语义时，才可作为 artifact 字段。
    """
    fields: list[str] = []
    contract = contract or {}

    # 显式声明为文件产物的字段，直接采纳。
    for key in ("artifact_fields", "file_fields", "file_outputs"):
        raw = contract.get(key)
        if isinstance(raw, str) and raw.strip():
            fields.append(raw.strip())
        elif isinstance(raw, list):
            fields.extend(str(item).strip() for item in raw if str(item).strip())

    # stdout_fields / output_fields 是 stdout JSON 字段，不等于 artifact 字段。
    # 只有字段名本身具备平台 runtime artifact 语义时，才提升为 artifact。
    for key in ("stdout_fields", "output_fields"):
        raw = contract.get(key)
        candidates: list[str] = []

        if isinstance(raw, str) and raw.strip():
            candidates.append(raw.strip())
        elif isinstance(raw, list):
            candidates.extend(str(item).strip() for item in raw if str(item).strip())

        for field_name in candidates:
            try:
                if is_runtime_artifact_semantic(field_name):
                    fields.append(field_name)
            except Exception:
                # 语义判断 helper 异常时，宁可不把普通 stdout 字段误判为 artifact。
                continue

    # 显式 artifact_outputs manifest 继续采纳。
    raw_outputs = contract.get("artifact_outputs")
    if isinstance(raw_outputs, list):
        for item in raw_outputs:
            if isinstance(item, dict):
                field = str(item.get("field") or item.get("name") or "").strip()
                if field:
                    fields.append(field)

    return list(dict.fromkeys(fields))


def _artifact_fields_from_tool_manifests(entry: SkillPlanEntry) -> list[str]:
    fields: list[str] = []
    for capability_name in list(entry.required_capabilities or []) + list(entry.selected_tools if hasattr(entry, "selected_tools") else []):
        cap = get_tool_capability(str(capability_name))
        if not cap:
            continue
        for output in getattr(cap, "artifact_outputs", []) or []:
            if isinstance(output, dict):
                field = str(output.get("field") or output.get("name") or "").strip()
                if field:
                    fields.append(field)
        for fn in getattr(cap, "functions", []) or []:
            for output in getattr(fn, "artifact_outputs", []) or []:
                if isinstance(output, dict):
                    field = str(output.get("field") or output.get("name") or "").strip()
                    if field:
                        fields.append(field)
    return list(dict.fromkeys(fields))


def _artifact_fields_for_entry(entry: SkillPlanEntry, canonical_contract: Any | None = None) -> list[str]:
    fields: list[str] = []
    fields.extend(_artifact_fields_from_contract_dict(entry.artifact_contract))
    if canonical_contract is not None:
        fields.extend(_artifact_fields_from_contract_dict(getattr(canonical_contract, "artifact_contract", {}) or {}))
    fields.extend(_artifact_fields_from_tool_manifests(entry))
    return list(dict.fromkeys(field for field in fields if field))

def _e2e_layer_from_errors(errors: list[str]) -> str:
    for error in errors or []:
        match = re.search(r"^E2E_LAYER=([^\n]+)", str(error), re.M)
        if match:
            return match.group(1).strip()
    return ""


def _targeted_e2e_repair_hint(errors: list[str]) -> str:
    layer = _e2e_layer_from_errors(errors)

    if layer == "final_platform_output_contract":
        return (
            "当前失败只属于最终平台输出字段不对齐。"
            "不要修改 SKILL.md，不要新增模型调用，不要改变已有业务字段。"
            "请保留最后一步脚本 stdout JSON 的原有业务字段，并额外映射到合法平台最终输出字段。"
            "如果最后一步已有可展示的主要结果值，保留原字段并额外映射到 text 或 markdown。"
            "如果最后一步产出文件，输出对应平台文件字段。"
        )

    if layer == "final_platform_output_value_invalid":
        return (
            "当前失败属于最终平台字段值类型不合法。"
            "请保持字段名不变，但修正值类型："
            "text/markdown/pdf_path/docx_path/pptx_path/html_path 必须是非空字符串；"
            "image_paths/file_paths/file_outputs 必须是非空字符串列表。"
        )

    if layer in {"external_input_missing", "e2e_dataflow_missing"}:
        return (
            "当前失败属于命令占位符无法从 payload 或前序 stdout 解析。"
            "优先修 SKILL.md 当前失败步骤的 JSON argv placeholder，"
            "不要改已成功 trace 对应步骤。"
        )

    return ""

