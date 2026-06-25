"""Creator FastAPI endpoint handlers and response assembly."""

from .common import *  # noqa: F403
from . import common as _common

globals().update({k: v for k, v in _common.__dict__.items() if not k.startswith("__")})

@router.post("/analyze-blueprint", response_model=AnalyzeBlueprintResponse)
async def analyze_blueprint(request: AnalyzeBlueprintRequest):
    try:
        plan: BlueprintPlan = parse_blueprint(request.messages, strict=request.strict)
    except BlueprintShapeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    effective_messages = request.messages
    blueprint_contract_warnings: list[dict[str, Any]] = []
    entries_by_path = {
        entry.path: entry
        for entry in (plan.skill_plan.files if plan.skill_plan else [])
        if not _is_directory_like_skill_path(entry.path)
    }

    blueprint_text = "\n\n".join(
        str(message.get("content") or "")
        for message in effective_messages
        if isinstance(message, dict)
    )

    def is_directory_placeholder(path: str) -> bool:
        normalized = _normalize_skill_path(path)
        return not normalized or normalized in {"assets", "assets/"} or normalized.endswith("/") or (
            normalized.startswith(("assets/", "references/", "scripts/")) and not _has_file_extension(normalized)
        )

    base_paths = {f.path for f in plan.files if not is_directory_placeholder(f.path)}

    candidate_paths: set[str] = {path for path in _extract_declared_skill_paths(blueprint_text) if not is_directory_placeholder(path)}
    candidate_paths.update(entries_by_path.keys())

    extra_paths = []
    extra_path_warnings: list[str] = []
    for path in sorted(candidate_paths):
        if path in base_paths or not path.startswith("references/"):
            continue
        if is_runtime_artifact_semantic(path, _local_blueprint_text_for_path(path, blueprint_text)):
            extra_path_warnings.append(
                f"已忽略运行时产物文件计划项 {path}；运行时生成文件只能通过脚本 outputs/stdout metadata 表示。"
            )
            continue
        extra_paths.append(path)

    def fallback_role(path: str) -> str | None:
        if path == "SKILL.md":
            return "skill_overview"
        if path.startswith("scripts/"):
            return "generic_script"
        if path.startswith("references/"):
            return "reference"
        if path.startswith("assets/"):
            return "asset"
        return None

    def fallback_file_type(path: str) -> str | None:
        if path == "SKILL.md":
            return "skill"
        if path.startswith("scripts/"):
            return "script"
        if path.startswith("references/"):
            return "reference"
        if path.startswith("assets/"):
            return "asset"
        return None

    def serialize_plan_items(items: Any) -> list[dict[str, Any]]:
        return [
            dict(getattr(item, "__dict__", item))
            for item in (items or [])
            if isinstance(getattr(item, "__dict__", item), dict)
        ]

    def selected_tools_for_entry(entry: SkillPlanEntry | None) -> list[str]:
        if not entry:
            return []
        return list(resolve_tools_for_skill_plan_entry(entry).allowed_tools or [])

    files_out: list[FileSpecOut] = []

    directory_asset_requirements: list[AssetRequirementOut] = []
    for f in plan.files:
        if is_directory_placeholder(f.path):
            local_context = _local_blueprint_text_for_path(f.path, blueprint_text) or f.purpose or blueprint_text
            # TODO: move directory-level upload needs into the normalized plan so
            # asset_requirements are explicit and no longer inferred from text.
            if _normalize_skill_path(f.path).startswith("assets") and re.search(r"上传|user[_ -]?upload|素材|图片|image|asset", local_context, re.IGNORECASE) and not re.search(r"无需|不需要|不用|无需创建|不生成", local_context):
                directory_asset_requirements.append(AssetRequirementOut(
                    path="assets/",
                    generation_order=_generation_order_for_file("assets/", "user_upload"),
                    source="user_upload",
                    required=getattr(f, "required", True),
                    description=f.purpose or "需要用户上传素材",
                ))
            continue
        entry = entries_by_path.get(f.path)
        role = entry.role if entry else fallback_role(f.path)
        file_type = entry.file_type if entry else fallback_file_type(f.path)
        language = entry.language if entry else language_for_path(f.path)
        runtime = entry.runtime if entry else runtime_for_language(language, file_type or "")

        files_out.append(
            FileSpecOut(
                path=f.path,
                generation_order=_generation_order_for_file(f.path, f.asset_source if f.path.startswith("assets/") else ""),
                purpose=f.purpose,
                required=f.required,
                can_skip=f.can_skip,
                file_type=file_type,
                file_kind=entry.file_kind if entry else file_kind_for_path(f.path),
                role=role,
                component_hint=entry.component_hint if entry else (role or ""),
                inputs=entry.inputs if entry else [],
                outputs=entry.outputs if entry else [],
                dependencies=entry.dependencies if entry else [],
                side_effects=entry.side_effects if entry else [],
                required_tool_slots=serialize_plan_items(entry.required_tool_slots) if entry else [],
                implementation_strategy=serialize_plan_items(entry.implementation_strategy) if entry else [],
                selected_tools=selected_tools_for_entry(entry),
                runtime_contract=entry.runtime_contract if entry else {},
                artifact_contract=entry.artifact_contract if entry else {},
                required_capabilities=entry.required_capabilities if entry else [],
                raw_capability_hints=entry.raw_capability_hints if entry else [],
                forbidden_capabilities=entry.forbidden_capabilities if entry else [],
                reference_files=entry.reference_files if entry else [],
                skill_local_references=entry.skill_local_references if entry else [],
                creator_internal_references=entry.creator_internal_references if entry else [],
                language=language,
                runtime=runtime,
                entrypoint=entry.entrypoint if entry else "",
                command_template=entry.command_template if entry else "",
                references=entry.reference_files if entry else [],
                low_confidence=(entry.confidence < 0.7) if entry else False,
                confidence=entry.confidence if entry else 1.0,
                reason=entry.reason if entry else "fallback path classification",
                heuristic_signals=entry.heuristic_signals if entry else [],
                asset_source=f.asset_source if f.path.startswith("assets/") else "",
            )
        )

    for path in extra_paths:
        role = fallback_role(path)
        file_type = fallback_file_type(path)
        language = language_for_path(path)
        runtime = runtime_for_language(language, file_type or "")
        is_asset = path.startswith("assets/")

        required_capabilities, forbidden_capabilities = [], []
        inputs, outputs = default_io_for_file_kind(file_kind_for_path(path))

        files_out.append(
            FileSpecOut(
                path=path,
                generation_order=_generation_order_for_file(path, ""),
                purpose=(
                    f"用户上传的静态素材：{path}"
                    if is_asset
                    else f"参考说明文件：{path}"
                ),
                required=True,
                can_skip=False,
                file_type=file_type,
                file_kind=file_kind_for_path(path),
                role=role,
                component_hint=role or "",
                inputs=list(inputs or []),
                outputs=list(outputs or []),
                dependencies=[],
                side_effects=[],
                required_tool_slots=[],
                implementation_strategy=[],
                selected_tools=[],
                runtime_contract={},
                artifact_contract={},
                required_capabilities=list(required_capabilities or []),
                raw_capability_hints=[],
                forbidden_capabilities=[
                    cap for cap in list(forbidden_capabilities or [])
                    if cap not in set(required_capabilities or [])
                ],
                reference_files=[],
                skill_local_references=[],
                creator_internal_references=[],
                language=language,
                runtime=runtime,
                entrypoint=path if path.startswith("scripts/") else "",
                command_template="",
                references=[],
                low_confidence=False,
                confidence=1.0,
                reason="fallback path classification from declared blueprint path",
                heuristic_signals=["declared_skill_path"],
                asset_source="",
            )
        )

    asset_requirements = [
        AssetRequirementOut(
            path=file_spec.path,
            generation_order=file_spec.generation_order,
            source=file_spec.asset_source,
            required=file_spec.required,
            description=file_spec.purpose,
        )
        for file_spec in files_out
        if file_spec.path.startswith("assets/") and file_spec.asset_source == "user_upload"
    ] + directory_asset_requirements

    available_tools = [tool_status(cap) for cap in list_tool_capabilities()]
    required_tool_names = {
        capability
        for file_spec in files_out
        for capability in file_spec.required_capabilities
    }
    missing_tool_configs = []
    def normalize_warning(item: Any) -> dict[str, Any] | None:
        if isinstance(item, dict):
            return {
                "severity": str(item.get("severity") or "normalization_note"),
                "code": str(item.get("code") or "normalization_note"),
                "source": str(item.get("source") or "skill_plan"),
                "path": str(item.get("path") or ""),
                "field": str(item.get("field") or ""),
                "message": str(item.get("message") or ""),
            }
        text = str(item or "").strip()
        if not text:
            return None
        return {
            "severity": "normalization_note",
            "code": "normalization_note",
            "source": "skill_plan",
            "path": "",
            "field": "",
            "message": text,
        }

    warnings = []
    seen_warning_keys: set[str] = set()
    for raw_warning in [*list(plan.warnings), *extra_path_warnings, *blueprint_contract_warnings]:
        warning = normalize_warning(raw_warning)
        if not warning:
            continue
        key = ":".join(str(warning.get(part) or "") for part in ("source", "path", "field", "code"))
        if key in seen_warning_keys:
            continue
        seen_warning_keys.add(key)
        warnings.append(warning)
    for capability_name in sorted(required_tool_names):
        cap = get_tool_capability(capability_name)
        if not cap or cap.category == "resource":
            continue
        status = tool_status(cap)
        missing_runtime_helpers = status.get("missing_runtime_helpers") or []
        missing_dependencies = status.get("missing_dependencies") or []
        if not status["creator_available"]:
            warnings.append({"severity": "user_warning", "code": "tool_unavailable", "source": "generator", "path": "", "field": "required_capabilities", "message": f"工具能力 {capability_name} 已被禁用或不允许 Creator 使用，相关脚本不会默认获得该能力。"})
        if missing_runtime_helpers:
            warnings.append({"severity": "user_warning", "code": "tool_runtime_helper_missing", "source": "generator", "path": "", "field": "required_capabilities", "message": f"工具能力 {capability_name} 缺少 runtime helper: {', '.join(missing_runtime_helpers)}。"})
        if missing_dependencies:
            warnings.append({"severity": "user_warning", "code": "tool_runtime_dependency_missing", "source": "generator", "path": "", "field": "required_capabilities", "message": f"工具能力 {capability_name} 缺少 runtime dependency: {', '.join(missing_dependencies)}。"})
        if not status["configured"] or missing_runtime_helpers or missing_dependencies or not status["creator_available"]:
            missing_tool_configs.append(status)

    return AnalyzeBlueprintResponse(
        skill_name=plan.skill_name,
        files=files_out,
        warnings=warnings,
        asset_requirements=asset_requirements,
        final_outputs=_final_outputs_from_plan_entries(list(entries_by_path.values())),
        available_tools=available_tools,
        missing_tool_configs=missing_tool_configs,
        blueprint_text=blueprint_text,
        blueprint_refined=False,
    )


@router.post("/init-skill", response_model=InitSkillResponse)
async def init_skill(request: InitSkillRequest):
    """Initialise a new Skill directory structure."""
    skill_name = _validate_skill_name(request.skill_name)
    result = run_action({"action": "init", "name": skill_name})
    return InitSkillResponse(
        success=result["success"],
        path=result.get("path"),
        message=result["message"],
    )

@router.post("/upload-asset", response_model=UploadAssetResponse)
async def upload_asset(
    skill_name: str = Form(...),
    file_path: str = Form(...),
    file: UploadFile = File(...),
):
    skill_name = _validate_skill_name(skill_name)
    target_rel_path = _validate_asset_upload_path(file_path)

    skill_dir = settings.skills_path / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)

    target_path = skill_dir / target_rel_path
    target_path.parent.mkdir(parents=True, exist_ok=True)

    total = 0
    try:
        with target_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break

                total += len(chunk)
                if total > _MAX_ASSET_UPLOAD_BYTES:
                    try:
                        target_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    raise HTTPException(
                        status_code=413,
                        detail=f"素材文件超过大小限制：{_MAX_ASSET_UPLOAD_BYTES // 1024 // 1024}MB",
                    )

                out.write(chunk)
    finally:
        await file.close()

    return UploadAssetResponse(
        success=True,
        path=target_rel_path,
        size=total,
        message=f"素材已上传：{target_rel_path}",
    )


@dataclass
class FileGenerationStageError(Exception):
    """Structured failure source for first-round generation validation."""

    source: str
    layer: str
    detail: str
    original: Exception | None = None

    def __str__(self) -> str:
        return self.detail


def _contract_failure_layer(results: list[ContractCheckResult]) -> str:
    failed = [result for result in results if not result.passed]
    if not failed:
        return "content_review"

    first = failed[0]
    if getattr(first, "layer", None):
        return str(first.layer)

    if getattr(first, "id", None):
        return str(first.id)

    return "content_review"


def _stage_error_from_exception(source: str, exc: Exception, *, default_layer: str) -> FileGenerationStageError:
    if isinstance(exc, FileGenerationStageError):
        return exc

    if isinstance(exc, ContractValidationError):
        layer = _contract_failure_layer(exc.results) or default_layer

        # 让 SKILL.md 蓝图一致性失败进入专门的返修 source。
        # 不是新增责任审查，只是把已有审查结果正确分类。
        if layer == "skill_md_blueprint_alignment" or any(
            getattr(result, "layer", None) == "skill_md_blueprint_alignment"
            for result in exc.results
            if not result.passed
        ):
            return FileGenerationStageError(
                source="skill_md_blueprint_alignment",
                layer="skill_md_blueprint_alignment",
                detail=str(exc),
                original=exc,
            )

        return FileGenerationStageError(
            source=source,
            layer=layer,
            detail=str(exc),
            original=exc,
        )

    return FileGenerationStageError(
        source=source,
        layer=default_layer,
        detail=str(exc),
        original=exc,
    )


def _first_round_repair_limit(source: str) -> int:
    return {
        "markdown_format": 3,
        "content_review": 4,
        "skill_md_blueprint_alignment": 6,
        "script_responsibility": 5,
        "script_functional": 5,
        "model_empty_content": len(_EMPTY_GENERATION_PROMPT_VARIANTS),
    }.get(source, 4)


def _prompt_chars(messages: list[dict]) -> int:
    return sum(len(str(message.get("content") or "")) for message in messages if isinstance(message, dict))


async def _complete_creator_file_generation(
    *,
    messages: list[dict],
    model: str,
    skill_name: str,
    file_path: str,
    prompt_variant: str,
    retry_index: int,
) -> str:
    """Call the file-generation model with minimum diagnostic logging."""
    messages = _ensure_user_visible_task_message(messages)
    prompt_text = "\n".join(str(message.get("content") or "") for message in messages if isinstance(message, dict))
    logger.info(
        "[Creator][generate_file][llm_request] skill=%s file_path=%s model=%s prompt_variant=%s retry_index=%d prompt_chars=%d message_roles=%s system_chars=%d user_chars=%d uses_full_blueprint=%s uses_platform_protocol_text=%s",
        skill_name,
        file_path,
        model,
        prompt_variant,
        retry_index,
        _prompt_chars(messages),
        json.dumps(_message_role_counts(messages), ensure_ascii=False, sort_keys=True),
        _message_role_chars(messages, "system"),
        _message_role_chars(messages, "user"),
        "已确认的蓝图" in prompt_text or "Skill 架构蓝图" in prompt_text,
        any(
            marker in prompt_text
            for marker in ("宿主 Markdown 执行说明", "SKILL.md workflow", "第二轮 E2E", "平台执行协议")
        ),
    )
    try:
        content = await complete_chat_once(messages, model)
    except Exception as exc:
        logger.exception(
            "[Creator][generate_file][llm_response] skill=%s file_path=%s model=%s prompt_variant=%s retry_index=%d message_roles=%s system_chars=%d user_chars=%d error_type=%s",
            skill_name,
            file_path,
            model,
            prompt_variant,
            retry_index,
            json.dumps(_message_role_counts(messages), ensure_ascii=False, sort_keys=True),
            _message_role_chars(messages, "system"),
            _message_role_chars(messages, "user"),
            type(exc).__name__,
        )
        raise
    logger.info(
        "[Creator][generate_file][llm_response] skill=%s file_path=%s model=%s prompt_variant=%s retry_index=%d message_roles=%s system_chars=%d user_chars=%d finish_reason=%s content_len=%d error_type=%s",
        skill_name,
        file_path,
        model,
        prompt_variant,
        retry_index,
        json.dumps(_message_role_counts(messages), ensure_ascii=False, sort_keys=True),
        _message_role_chars(messages, "system"),
        _message_role_chars(messages, "user"),
        "unknown",
        len(content or ""),
        "",
    )
    return content

def _build_skill_md_model_finalizer_prompt(
    *,
    skill_name: str,
    description: str,
    blueprint_text: str,
    references: list[str] | None,
    assets: list[str] | None,
    final_outputs: list[str] | None,
) -> list[dict[str, str]]:
    resource_reference = {
        "references": references or [],
        "assets": assets or [],
        "final_outputs": final_outputs or [],
    }

    return [
        {
            "role": "system",
            "content": (
                "你是 SKILL.md 最终说明文档编辑器。"
                "请基于蓝图创作完整、自然、用户可读的 SKILL.md。"
                "不要泄露 runtime_contract、artifact_contract、ToolSlot、implementation_strategy 等内部字段。"
                "不要把本文档退化成合同字段清单。"
                "只输出 SKILL.md 文件内容，不要 Markdown 外层代码块。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"Skill 名称：{skill_name}\n"
                f"描述：{description}\n\n"
                "蓝图：\n"
                f"{clean_blueprint_body_text(blueprint_text or '')}\n\n"
                "资源和最终输出参考：\n"
                f"{json.dumps(resource_reference, ensure_ascii=False, indent=2)}\n\n"
                "请输出完整 SKILL.md 文件正文，必须满足：\n"
                "1. 文件必须以 YAML frontmatter 开头，包含 name 和 description。\n"
                "2. 正文应包含适用场景、用户需提供内容、执行流程、脚本调用说明、references/assets 使用说明、最终产物和注意事项。\n"
                "3. 如果蓝图包含 scripts/*.py，必须为每个真实脚本写一个标准、独立、无缩进的 ```bash fenced code block。\n"
                "4. 每个 ```bash block 内只能有一条真实 shell 命令。\n"
                "5. 标准命令格式必须是：python scripts/<真实脚本名>.py '<JSON object argv>'。\n"
                "6. 脚本路径后的第一个参数必须是 json.loads 可解析的 JSON object 字符串。\n"
                "7. JSON argv 必须是 object，但 object 内字段名必须由当前脚本真实接口、蓝图需求和上下游数据流决定。\n"
                "8. 不得固定套用 payload/user_request/fields/options/input_files 等模板字段。\n"
                "9. 如果脚本需要主题参数，就传主题参数；如果脚本需要图片路径，就传图片路径；如果脚本需要前序输出，就使用对应前序输出 placeholder。\n"
                "10. 动态 placeholder 必须作为 JSON 字符串值出现；不要把未加引号的动态 placeholder 放进 JSON。\n"
                "11. 禁止在 ```bash block 中直接放 JSON 配置对象。\n"
                "12. 禁止在 ```bash block 中放 runner/script/argv 伪命令对象。\n"
                "13. 禁止在 ```bash block 中放说明文字、列表、多条命令或 `<真实参数>` 占位说明。\n"
                "14. 默认不要使用 --argv CLI flag，除非脚本源码明确实现了 --argv；Creator 默认脚本协议是 sys.argv[1] JSON object。\n"
                "15. references/*.md 只作为参考资料说明，不是执行源。不要把 reference 正文全文复制进 SKILL.md。\n"
                "16. assets/** 只能作为上传素材或静态资源引用，不能描述为 Creator 生成素材。\n"
                "17. 不要包含 Creator 创建流程、确认清单、点击开始创建、系统将自动创建文件等平台创建流程文案。\n"
                "18. 不要声称“已通过 E2E 校验”“可直接投入运行”，SKILL.md 是使用说明，不是校验报告。\n\n"
                "标准命令示例只说明形态，不代表固定字段：\n"
                "```bash\n"
                "python scripts/generate_story.py '{\"topic\":\"{{topic}}\",\"chapter_count\":5}'\n"
                "```\n\n"
                "```bash\n"
                "python scripts/build_pdf.py '{\"story_text\":\"{{story_text}}\",\"image_paths\":\"{{image_paths}}\"}'\n"
                "```\n\n"
                "注意：实际字段必须根据当前蓝图和脚本接口调整，禁止照抄示例字段。"
            ),
        },
    ]

def _strict_contract_rewrite_allowed(source: str) -> bool:
    # First-round repairs must stay localized: the validator identifies the
    # failed function/region, and the repair model edits only that region while
    # preserving entrypoints, JSON argv parsing, stdout fields, and artifact
    # protocol.  Do not escalate script failures into full rewrites.
    return source in {"content_review"}


def _repair_mode_for_first_round(*, source: str, file_path: str, attempt: int) -> str:
    """Choose repair mode for first-round generation failures.

    第一轮 scripts/** 只剩：
    - content_review：裸源码、语法、安全；
    - script_functional / script_responsibility：职责未完成。

    不再存在 script_smoke。
    """
    if source in {"script_functional", "script_responsibility"} and file_path.startswith("scripts/"):
        return "localized_patch"

    if attempt >= 2 and file_path.startswith("scripts/") and _strict_contract_rewrite_allowed(source):
        return "strict_contract_rewrite"

    return "minimal_edit"


def _contract_result_to_failure(result: ContractCheckResult) -> dict[str, Any]:
    return {
        "id": result.id,
        "target": result.target,
        "message": result.message,
        "expected": result.expected,
        "minimal_edit": result.minimal_edit,
        "details": result.details,
        "layer": result.layer or _contract_layer_for_check_id(result.id),
    }


def _exception_to_skill_md_failures(exc: Exception, *, source: str = "skill_md") -> list[dict[str, Any]]:
    if isinstance(exc, ContractValidationError):
        return [_contract_result_to_failure(result) for result in exc.results if not result.passed]
    return [{
        "id": f"{source}.validation_error",
        "target": "SKILL.md",
        "message": str(exc),
        "expected": "SKILL.md 第一轮只修格式、蓝图一致性、脚本说明、资源说明、最终产物说明和平台/内部字段泄露。",
        "minimal_edit": "只修改 SKILL.md；不要修改 scripts、assets、SkillPlan 或 runtime specs。",
        "details": {},
        "layer": source,
    }]


def _non_code_text_near_script(content: str, script_path: str, window: int = 360) -> str:
    normalized = content.replace("\r\n", "\n")
    idx = normalized.find(script_path)
    if idx < 0:
        return ""
    start = max(0, idx - window)
    end = min(len(normalized), idx + len(script_path) + window)
    nearby = normalized[start:end]
    nearby = re.sub(r"```[\s\S]*?```", " ", nearby)
    nearby = re.sub(r"`[^`]*`", " ", nearby)
    nearby = re.sub(r"[#>*_\-\[\]()`]", " ", nearby)
    return re.sub(r"\s+", " ", nearby).strip()


def _is_generic_script_description(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "").lower()
    if len(compact) < 24:
        return True
    generic_patterns = [
        "执行脚本", "运行脚本", "处理任务", "运行该步骤", "执行该步骤",
        "调用脚本", "script", "runthisscript", "processtask",
    ]
    return any(pattern in compact for pattern in generic_patterns) and len(compact) < 60


def _check_skill_md_script_narrative_quality(content: str, script_paths: list[str]) -> list[ContractCheckResult]:
    results: list[ContractCheckResult] = []
    for script_path in dict.fromkeys(path for path in script_paths if path.startswith("scripts/")):
        mentioned = script_path in content
        results.append(ContractCheckResult(
            id="skill_md.script.mentioned",
            passed=mentioned,
            target=script_path,
            message=f"{script_path} 已在 SKILL.md 中出现。" if mentioned else f"{script_path} 未在 SKILL.md 中出现。",
            expected="每个真实 scripts/** 都必须在 SKILL.md 中被提及。",
            minimal_edit=f"添加 {script_path} 的自然语言功能说明和对应 bash fenced block。",
            layer="skill_md_first_round",
        ))
        commands = _extract_script_command_templates(content, script_path) if mentioned else []
        results.append(ContractCheckResult(
            id="skill_md.script.bash_block_nearby",
            passed=bool(commands),
            target=script_path,
            message=f"{script_path} 有对应 bash fenced block。" if commands else f"{script_path} 缺少对应 bash fenced block。",
            expected="每个脚本必须有对应 ```bash fenced block，且 block 调用真实脚本路径。",
            minimal_edit=f"为 {script_path} 添加调用真实脚本路径且 argv 为 JSON object 的 bash fenced block。",
            layer="skill_md_first_round",
        ))
        nearby_text = _non_code_text_near_script(content, script_path)
        has_specific_description = bool(nearby_text) and not _is_generic_script_description(nearby_text)
        results.append(ContractCheckResult(
            id="skill_md.script.narrative_quality",
            passed=has_specific_description,
            target=script_path,
            message=f"{script_path} 附近有非代码块的具体功能说明。" if has_specific_description else f"{script_path} 附近缺少具体自然语言功能说明，或说明过于空泛。",
            expected="每个脚本附近必须说明它在整体流程中的作用，不能只有“执行脚本/处理任务/运行该步骤”。",
            minimal_edit=f"在 {script_path} 的 bash block 前后添加一句具体说明：它读取什么、完成什么流程步骤、产出什么用户可理解结果。",
            details={"nearby_text": nearby_text[:240]},
            layer="skill_md_first_round",
        ))
    return results

def _skill_md_first_round_failures(
    *,
    skill_name: str,
    content: str,
    blueprint_text: str,
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []

    # 1. 最早先做 Markdown 基础格式检查。
    # 格式不对，不进入后面的模型内容审查。
    raw_failures = _basic_markdown_format_failures(
        "SKILL.md",
        content,
        require_frontmatter=True,
    )
    if raw_failures:
        return raw_failures

    # 2. 再跑你现有的平台合同规则。
    try:
        _raise_file_contract_failures(validate_file_contract(
            file_path="SKILL.md",
            content=content,
            blueprint_text=blueprint_text or "",
            skill_plan_entry=None,
        ))
    except Exception as exc:
        failures.extend(_exception_to_skill_md_failures(exc, source="skill_md_contract"))

    # 3. 再检查文件引用。
    try:
        _validate_skill_md_against_existing_files(
            skill_name,
            content,
            blueprint_text=blueprint_text or "",
            require_existing=False,
        )
    except Exception as exc:
        failures.extend(_exception_to_skill_md_failures(exc, source="skill_md_files"))

    return failures


async def _repair_skill_md_model_finalizer(
    *,
    previous_content: str,
    failures: list[dict[str, Any]],
    prompt_messages: list[dict[str, str]],
    model: str,
    skill_name: str,
    attempt: int,
) -> str:
    """Repair SKILL.md by exact_replace patch, not full regeneration."""

    failures_text = json.dumps(failures, ensure_ascii=False, indent=2, default=str)

    validation_error = (
        "SKILL.md 合同/责任/蓝图对齐校验未通过。"
        "本轮只能对上一版 SKILL.md 做局部 patch 修复，不能重新生成完整文件。\n\n"
        "注意：Markdown 基础格式错误不会进入本函数，已经由整文件重写阶段处理。\n\n"
        "失败项 JSON：\n"
        f"{failures_text}"
    )

    targeted_repair = (
        "只修复 failures 指向的 target/layer/minimal_edit 对应区域。"
        "未被 failures 指向的 frontmatter、章节、脚本说明、bash fenced block、资源说明、最终产物说明必须保持。"
        "不得重排整篇文档，不得新增蓝图外脚本、reference、asset 或能力。"
        "如果失败是资源提及问题，只在已有资源说明附近补充缺失路径。"
        "\n\n"
        "如果失败涉及 command_block、fenced block、single_command、signature_parseable、json_argv_object、"
        "命令块、bash block 或脚本调用格式："
        "只修改对应的 ```bash fenced block。"
        "bash block 内必须是一条真实可执行 shell 命令，且能解析出 runner、真实 scripts/*.py 路径、一个 JSON object argv 参数。"
        "JSON argv 的 key/value 由当前脚本接口和 workflow 自洽决定；不要固定套用某组字段名。"
        "禁止在 bash block 中保留 JSON 配置对象、伪命令对象、说明文字、列表或多条命令。"
        "如果当前 block 是 JSON 伪命令，只把该 block 改成等价的真实脚本调用命令，不要重写其它章节。"
        "\n\n"
        "不要输出完整 SKILL.md，只输出 exact_replace patch。"
    )

    return await _repair_generated_file_with_feedback(
        prompt_messages=prompt_messages,
        model=model,
        file_path="SKILL.md",
        previous_content=previous_content,
        validation_error=validation_error,
        targeted_repair=targeted_repair,
        contract_text=(
            "SKILL.md 是主 Skill 说明文档，必须保持蓝图意图、真实脚本顺序、资源说明和最终输出说明一致。"
            "所有 scripts/*.py 必须通过真实可执行 bash 命令调用，不能使用 JSON 伪命令块。"
        ),
        passed_checks_text="",
        failed_checks_text=failures_text,
        repair_mode="localized_patch",
        skill_plan_entry=None,
    )


@router.post("/finalize-skill-md")
async def finalize_skill_md(request: FinalizeSkillMdRequest):
    """Finalize SKILL.md with staged repair.

    阶段：
    1. Markdown 格式错误：整文件重写，最多 3 轮；
    2. 合同/责任/蓝图错误：局部 diff 修复；
    3. 最终失败也返回可编辑草稿，不抛 400，避免前端文件变灰。
    """
    skill_name = _validate_skill_name(request.skill_name)
    route = route_creator_file_model(
        file_path="SKILL.md",
        purpose=request.description or "final SKILL.md",
        requested_model=request.model,
    )

    prompt_messages = _build_skill_md_model_finalizer_prompt(
        skill_name=skill_name,
        description=request.description or "",
        blueprint_text=request.blueprint_text or "",
        references=request.references,
        assets=request.assets,
        final_outputs=request.final_outputs,
    )

    failures: list[dict[str, Any]] = []
    candidate = ""
    content = ""

    for attempt in range(1, _MAX_FILE_REPAIR_ATTEMPTS + 1):
        try:
            if attempt == 1:
                candidate = await _complete_creator_file_generation(
                    messages=prompt_messages,
                    model=route.model,
                    skill_name=skill_name,
                    file_path="SKILL.md",
                    prompt_variant="model_finalizer",
                    retry_index=0,
                )

            content = _sanitize_generated_file_content("SKILL.md", candidate)

            # 阶段 1：Markdown 基础格式错误，直接整文件重写，不走 diff。
            format_failures = _basic_markdown_format_failures(
                "SKILL.md",
                content,
                require_frontmatter=True,
            )
            if format_failures:
                failures = format_failures

                if attempt >= 3:
                    break

                rewrite_messages = [
                    {
                        "role": "system",
                        "content": (
                            "你是 SKILL.md Markdown 格式修复器。"
                            "你必须输出完整 SKILL.md 文件内容，不要输出 patch，不要输出 JSON，不要解释。"
                            "本轮只修 Markdown 结构格式，不修业务语义。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Skill 名称：{skill_name}\n\n"
                            "后台 Markdown 格式校验失败项：\n"
                            f"{json.dumps(format_failures, ensure_ascii=False, indent=2, default=str)}\n\n"
                            "要求：\n"
                            "1. 输出完整 SKILL.md 文件内容。\n"
                            "2. 不要用 ``` 包裹整个文件。\n"
                            "3. 只修 YAML frontmatter、frontmatter 收尾 ---、fenced block 成对闭合等 Markdown 格式问题。\n"
                            "4. 保留原有业务语义、脚本说明、资源说明、最终产物说明。\n"
                            "5. 不要新增蓝图外能力、脚本、reference 或 asset。\n"
                            "6. 不要声称已经执行或已经通过 E2E。\n\n"
                            "蓝图上下文：\n"
                            f"{(request.blueprint_text or '')[:8000]}\n\n"
                            "当前 SKILL.md 内容：\n"
                            "<<<CURRENT_FILE\n"
                            f"{content}\n"
                            "CURRENT_FILE\n"
                        ),
                    },
                ]

                candidate = await _complete_creator_file_generation(
                    messages=rewrite_messages,
                    model=route.model,
                    skill_name=skill_name,
                    file_path="SKILL.md",
                    prompt_variant="markdown_format_rewrite",
                    retry_index=attempt - 1,
                )
                continue

            # 阶段 2：平台合同、文件引用等非基础 Markdown 格式问题。
            failures = _skill_md_first_round_failures(
                skill_name=skill_name,
                content=content,
                blueprint_text=request.blueprint_text or "",
            )

            # 阶段 3：格式/合同通过后，再做蓝图责任审查。
            if not failures:
                try:
                    await _validate_skill_md_blueprint_alignment(
                        skill_name=skill_name,
                        content=content,
                        blueprint_text=request.blueprint_text or "",
                        skill_plan_entry=None,
                        model=request.model or route.model,
                    )
                except Exception as exc:
                    failures = _exception_to_skill_md_failures(exc, source="blueprint_alignment")

            if not failures:
                return {
                    "success": True,
                    "content": content,
                    "repair_attempts": attempt - 1,
                    "validation_status": "passed",
                    "editable": True,
                    "disabled": False,
                }

            if attempt >= _MAX_FILE_REPAIR_ATTEMPTS:
                break

            # 非格式错误继续走原来的局部 diff。
            candidate = await _repair_skill_md_model_finalizer(
                previous_content=content,
                failures=failures,
                prompt_messages=prompt_messages,
                model=route.model,
                skill_name=skill_name,
                attempt=attempt,
            )

        except Exception as exc:
            logger.exception(
                "[Creator][skill_md][finalize_attempt_failed] skill=%s attempt=%s",
                skill_name,
                attempt,
            )
            failures = _exception_to_skill_md_failures(exc, source="skill_md_model_finalize")
            break

    return {
        "success": False,
        "content": content or candidate,
        "repair_attempts": max(0, attempt if "attempt" in locals() else 0),
        "validation_status": "needs_repair",
        "needs_repair": True,
        "editable": True,
        "disabled": False,
        "failures": failures,
        "error": "SKILL.md finalize did not pass after repair attempts.",
    }

@router.post("/generate-file")
async def generate_file(request: GenerateFileRequest):
    """Generate one Creator file and stream it back as SSE.

    Important:
    - This endpoint must not write files to disk.
    - The frontend expects streamed content and then calls /write-file.
    - assets/** are upload-only and must never be generated by model.
    - SKILL.md must pass blueprint-intent alignment before returned.
    - references/*.md may omit YAML frontmatter; if present it must use ordinary document metadata only.
    """
    skill_name = _validate_skill_name(request.skill_name)
    _validate_file_path(request.file_path)

    if request.file_path.startswith("assets/") and (request.skill_plan_entry or {}).get("asset_source") != "bundled":
        raise HTTPException(
            status_code=400,
            detail=f"{request.file_path} 属于 assets 静态素材目录；只有 source=bundled 的预置静态资源可由 Creator 生成，source=user_upload 必须上传。",
        )

    # request.skill_plan_entry 来自前端请求态，只能作为 hint，不能作为唯一可信合同。
    # 这里不再因为 outputs/artifact_contract/file_kind 不完整而在生成循环外 422；
    # 真正的单文件合同会在 event_stream 内由后端基于 file_path、role、purpose、SKILL.md/blueprint 重新归一化。
    # 只有明确的 path 冲突才属于不可恢复请求错误。
    raw_skill_plan_entry = request.skill_plan_entry if isinstance(request.skill_plan_entry, dict) else {}
    raw_entry_path = str(raw_skill_plan_entry.get("path") or "").strip()
    if request.file_path.startswith("scripts/") and raw_entry_path and raw_entry_path != request.file_path:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "contract_path_mismatch",
                "severity": "user_warning",
                "source": "generator",
                "path": request.file_path,
                "field": "skill_plan_entry.path",
                "message": f"skill_plan_entry.path={raw_entry_path} 与当前 file_path={request.file_path} 不一致。",
            },
        )

    async def event_stream():
        try:
            route = route_creator_file_model(
                file_path=request.file_path,
                purpose=request.purpose,
                requested_model=request.model,
            )
            _log_creator_model_usage(
                phase="generate.route",
                skill_name=skill_name,
                file_path=request.file_path,
                route=route,
            )

            effective_skill_plan_entry = request.skill_plan_entry if isinstance(request.skill_plan_entry, dict) else None
            if request.file_path.startswith("scripts/"):
                # 后端生成阶段重新构建 canonical entry，避免依赖前端传来的不完整 entry。
                # 这仍然是单文件合同，不做跨文件 E2E 判断。
                skill_md_for_entry = (
                    (settings.skills_path / skill_name / "SKILL.md").read_text(encoding="utf-8")
                    if (settings.skills_path / skill_name / "SKILL.md").is_file()
                    else request.blueprint_text
                )
                entry_obj = _skill_plan_entry_for_file(
                    file_path=request.file_path,
                    purpose=request.purpose,
                    blueprint_text=skill_md_for_entry or request.blueprint_text,
                    role=request.role,
                    skill_plan_entry=effective_skill_plan_entry,
                )
                effective_skill_plan_entry = dict(getattr(entry_obj, "__dict__", {}) or {})
                effective_skill_plan_entry.setdefault("path", request.file_path)
                effective_skill_plan_entry.setdefault("purpose", request.purpose)

            prompt_messages = _build_generate_file_prompt(
                request.file_path,
                skill_name,
                request.purpose,
                request.blueprint_text,
                request.conversation_history,
                role=request.role,
                skill_plan_entry=effective_skill_plan_entry,
            )
            prompt_variant = "standard"
        except Exception as exc:
            logger.exception("Creator generate_file prepare failed: %s", exc)
            yield _file_done_error_sse(
                file_path=request.file_path,
                role=request.role,
                error=f"生成前准备失败：{exc}",
                error_type="prepare_failed",
            )
            return

        candidate = ""
        repair_counts_by_layer: dict[str, int] = {}

        try:
            candidate = await _complete_creator_file_generation(
                messages=prompt_messages,
                model=route.model,
                skill_name=skill_name,
                file_path=request.file_path,
                prompt_variant=prompt_variant,
                retry_index=0,
            )
        except Exception as exc:
            logger.exception("Creator generate_file initial model call failed: %s", exc)
            yield _file_done_error_sse(
                file_path=request.file_path,
                role=request.role,
                error=f"模型调用失败：{exc}",
                error_type="model_call_failed",
            )
            return

        for attempt in range(1, _MAX_FILE_REPAIR_ATTEMPTS + 1):
            try:
                if len(candidate or "") == 0:
                    raise FileGenerationStageError(
                        source="model_empty_content",
                        layer="file_generation",
                        detail="model_empty_content: 模型生成结果 content_chars=0，跳过 validator/repair；进入 prompt 降级重试。",
                    )

                try:
                    content = _sanitize_generated_file_content(
                        request.file_path,
                        candidate,
                        role=request.role,
                        skill_plan_entry=effective_skill_plan_entry,
                    )

                    # 阶段 1：Markdown 基础格式错误最早判断。
                    # 这里必须在 canonicalize / ensure_reference_metadata 之前，
                    # 避免 reference 里 frontmatter 未闭合等错误被后续逻辑吞掉。
                    if (
                            request.file_path == "SKILL.md"
                            or request.file_path.startswith("references/")
                            or Path(request.file_path).suffix.lower() in {".md", ".markdown"}
                    ):
                        format_failures = _basic_markdown_format_failures(
                            request.file_path,
                            content,
                            require_frontmatter=(
                                    request.file_path == "SKILL.md"
                                    or request.file_path.startswith("references/")
                            ),
                        )
                        if format_failures:
                            raise FileGenerationStageError(
                                source="markdown_format",
                                layer="format",
                                detail=json.dumps(format_failures, ensure_ascii=False, indent=2, default=str),
                            )

                    content, _metadata_patched = _canonicalize_markdown_frontmatter_for_file(
                        file_path=request.file_path,
                        content=content,
                        skill_name=skill_name,
                        purpose=request.purpose,
                    )

                    if request.file_path.startswith("references/") and Path(request.file_path).suffix.lower() == ".md":
                        content = _ensure_reference_metadata_frontmatter(
                            file_path=request.file_path,
                            content=content,
                            purpose=request.purpose,
                            skill_plan_entry=effective_skill_plan_entry,
                        )

                    if not content.strip():
                        raise FileGenerationStageError(
                            source="content_review",
                            layer="content_empty",
                            detail=f"{request.file_path} 生成内容为空。",
                        )

                    if request.file_path == "SKILL.md":
                        _raise_file_contract_failures(validate_file_contract(
                            file_path=request.file_path,
                            content=content,
                            blueprint_text=request.blueprint_text,
                            skill_plan_entry=effective_skill_plan_entry,
                        ))

                        _validate_skill_md_against_existing_files(
                            skill_name,
                            content,
                            blueprint_text=request.blueprint_text,
                            require_existing=False,
                        )

                        await _validate_skill_md_blueprint_alignment(
                            skill_name=skill_name,
                            content=content,
                            blueprint_text=request.blueprint_text,
                            skill_plan_entry=effective_skill_plan_entry,
                            model=request.model or route.model,
                        )

                    elif request.file_path.startswith("references/"):
                        _raise_file_contract_failures(validate_file_contract(
                            file_path=request.file_path,
                            content=content,
                            blueprint_text=request.blueprint_text,
                            skill_plan_entry={
                                **(effective_skill_plan_entry or {}),
                                "purpose": request.purpose or request.blueprint_text,
                            },
                        ))

                    elif request.file_path.startswith("scripts/"):
                        _raise_file_contract_failures(_check_script_content_review_contract(
                            request.file_path,
                            content,
                            role=request.role,
                            skill_plan_entry=effective_skill_plan_entry,
                        ))

                except Exception as exc:
                    raise _stage_error_from_exception("content_review", exc, default_layer="content_review") from exc

                if request.file_path.startswith("scripts/"):
                    try:
                        skill_md = (
                            (settings.skills_path / skill_name / "SKILL.md").read_text(encoding="utf-8")
                            if (settings.skills_path / skill_name / "SKILL.md").is_file()
                            else ""
                        )

                        entry = _skill_plan_entry_for_file(
                            file_path=request.file_path,
                            blueprint_text=skill_md,
                            role=request.role,
                            skill_plan_entry=effective_skill_plan_entry,
                        )

                        responsibility_review = await _run_script_responsibility_review(
                            file_path=request.file_path,
                            script_content=content,
                            skill_plan_entry=entry,
                            deterministic_issues=[],
                            requested_model=request.model or route.model,
                            review_context={
                                "phase": "first_round_no_smoke",
                                "policy": (
                                    "第一轮只判断脚本是否完成自身职责；"
                                    "不检查运行、argv、stdout、artifact、字段名或上下游映射；"
                                    "这些由第二轮 E2E 负责。"
                                ),
                            },
                        )

                        if not responsibility_review.get("passed"):
                            issues = (
                                responsibility_review.get("issues")
                                if isinstance(responsibility_review.get("issues"), list)
                                else []
                            )
                            raise ScriptFunctionalValidationError(
                                issues or [{
                                    "id": "script_responsibility.failed",
                                    "failed_file": request.file_path,
                                    "failed_function": "current script",
                                    "code_region": "current file responsibility logic",
                                    "reason": "职责审查模型判定当前脚本没有完成自身职责。",
                                    "minimal_edit": str(
                                        responsibility_review.get("repair_instructions")
                                        or "只修改当前脚本中未完成职责的业务逻辑。"
                                    ),
                                    "allowed_scope": "只允许修改当前脚本职责实现区域。",
                                    "forbidden_scope": "不得修改 SKILL.md、workflow、字段映射、stdout schema、artifact 或其它脚本。",
                                    "details": {"review": responsibility_review},
                                }],
                                layer="responsibility",
                            )

                    except ScriptFunctionalValidationError as exc:
                        raise _stage_error_for_script_functional(exc) from exc
                    except Exception as exc:
                        raise _stage_error_from_exception(
                            "script_responsibility",
                            exc,
                            default_layer="responsibility",
                        ) from exc

                logger.info(
                    "[Creator][generate_file] validation passed file=%s role=%s content_chars=%d",
                    request.file_path,
                    request.role or "",
                    len(content),
                )

                yield _sse({
                    "type": "file_content",
                    "status": "success",
                    "success": True,
                    "file_path": request.file_path,
                    "role": request.role,
                    "content": content,
                    "editable": True,
                    "disabled": False,
                })

                yield _sse({
                    "type": "file_done",
                    "status": "success",
                    "success": True,
                    "file_path": request.file_path,
                    "role": request.role,
                    "done": True,
                    "editable": True,
                    "disabled": False,
                })

                return

            except Exception as exc:
                stage_error = (
                    exc
                    if isinstance(exc, FileGenerationStageError)
                    else _stage_error_from_exception("content_review", exc, default_layer="content_review")
                )
                deterministic_error = str(stage_error)
                error_source = stage_error.source
                error_layer = f"{stage_error.source}:{stage_error.layer}"
                repair_counts_by_layer[error_layer] = repair_counts_by_layer.get(error_layer, 0) + 1

                if error_source == "model_empty_content":
                    empty_retry_index = repair_counts_by_layer[error_layer]
                    if empty_retry_index >= len(_EMPTY_GENERATION_PROMPT_VARIANTS):
                        prompt_messages = _ensure_user_visible_task_message(prompt_messages)
                        logger.warning(
                            "[Creator][generate_file][model_empty_content] skill=%s file_path=%s model=%s prompt_variant=%s retry_index=%d prompt_chars=%d message_roles=%s system_chars=%d user_chars=%d content_len=%d finish_reason=%s variants=%s error_type=%s",
                            skill_name,
                            request.file_path,
                            route.model,
                            prompt_variant,
                            empty_retry_index,
                            _prompt_chars(prompt_messages),
                            json.dumps(_message_role_counts(prompt_messages), ensure_ascii=False, sort_keys=True),
                            _message_role_chars(prompt_messages, "system"),
                            _message_role_chars(prompt_messages, "user"),
                            len(candidate or ""),
                            "unknown",
                            "->".join(_EMPTY_GENERATION_PROMPT_VARIANTS),
                            "model_empty_content",
                        )
                        yield _file_done_error_sse(
                            file_path=request.file_path,
                            role=request.role,
                            error="文件内容生成失败：same-model prompt degradation 已尝试 standard -> simplified -> minimal 后仍为空。",
                            error_type="model_empty_content",
                            content=candidate or "",
                            recoverable=True,
                        )
                        return

                    next_variant = _EMPTY_GENERATION_PROMPT_VARIANTS[empty_retry_index]
                    next_messages = (
                        _build_script_generate_file_prompt_variant(
                            file_path=request.file_path,
                            skill_name=skill_name,
                            purpose=request.purpose,
                            blueprint_text=request.blueprint_text,
                            role=request.role,
                            skill_plan_entry=effective_skill_plan_entry,
                            variant=next_variant,
                        )
                        if request.file_path.startswith("scripts/")
                        else prompt_messages
                    )

                    yield _sse({
                        "type": "validation",
                        "status": "regenerating",
                        "success": False,
                        "file_path": request.file_path,
                        "role": request.role,
                        "validation": {
                            "status": "regenerating",
                            "attempt": attempt,
                            "source": error_source,
                            "layer": stage_error.layer,
                            "error": deterministic_error,
                        },
                    })

                    candidate = await _complete_creator_file_generation(
                        messages=next_messages,
                        model=route.model,
                        skill_name=skill_name,
                        file_path=request.file_path,
                        prompt_variant=next_variant,
                        retry_index=empty_retry_index,
                    )
                    prompt_messages = next_messages
                    prompt_variant = next_variant
                    continue
                if error_source == "markdown_format":
                    layer_limit = _first_round_repair_limit(error_source)

                    if repair_counts_by_layer[error_layer] > layer_limit:
                        yield _file_done_error_sse(
                            file_path=request.file_path,
                            role=request.role,
                            error=(
                                f"Markdown 格式修复失败：已整文件重写 {layer_limit} 轮仍未通过。"
                                f"最后错误：{deterministic_error}"
                            ),
                            error_type="markdown_format_rewrite_failed",
                            content=candidate or "",
                            recoverable=True,
                        )
                        return

                    yield _sse({
                        "type": "validation",
                        "status": "rewriting_format",
                        "success": False,
                        "file_path": request.file_path,
                        "role": request.role,
                        "editable": True,
                        "disabled": False,
                        "validation": {
                            "status": "rewriting_format",
                            "attempt": repair_counts_by_layer[error_layer],
                            "source": error_source,
                            "layer": stage_error.layer,
                            "error": deterministic_error,
                        },
                    })

                    rewrite_messages = [
                        {
                            "role": "system",
                            "content": (
                                "你是 Markdown 文件格式修复器。"
                                "你必须输出完整文件内容，不要输出 patch，不要输出 JSON，不要解释。"
                                "本轮只修 Markdown 结构格式，不修业务语义。"
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"文件路径：{request.file_path}\n"
                                f"Skill 名称：{skill_name}\n\n"
                                "后台 Markdown 格式校验失败项：\n"
                                f"{deterministic_error}\n\n"
                                "要求：\n"
                                "1. 输出完整 Markdown 文件内容。\n"
                                "2. 不要用 ``` 包裹整个文件。\n"
                                "3. 只修 YAML frontmatter、frontmatter 收尾 ---、fenced block 成对闭合等 Markdown 格式问题。\n"
                                "4. 保留原业务语义、脚本说明、资源说明、最终产物说明。\n"
                                "5. 不要新增蓝图外能力、脚本、reference、asset。\n"
                                "6. 如果是 reference 文件，必须保留 reference 的正文内容和用途。\n"
                                "7. 不要声称已经执行或已经通过 E2E。\n\n"
                                "蓝图上下文：\n"
                                f"{(request.blueprint_text or '')[:8000]}\n\n"
                                "当前文件内容：\n"
                                "<<<CURRENT_FILE\n"
                                f"{candidate or ''}\n"
                                "CURRENT_FILE\n"
                            ),
                        },
                    ]

                    candidate = await _complete_creator_file_generation(
                        messages=rewrite_messages,
                        model=route.model,
                        skill_name=skill_name,
                        file_path=request.file_path,
                        prompt_variant="markdown_format_rewrite",
                        retry_index=repair_counts_by_layer[error_layer] - 1,
                    )
                    continue
                layer_limit = _first_round_repair_limit(error_source)
                if repair_counts_by_layer[error_layer] > layer_limit:
                    yield _file_done_error_sse(
                        file_path=request.file_path,
                        role=request.role,
                        error=(
                            f"文件内容生成失败：同一阶段/层 {error_layer} "
                            f"已修复 {layer_limit} 次仍未通过。最后错误：{deterministic_error}"
                        ),
                        error_type="repair_layer_limit_exceeded",
                        content=candidate or "",
                        recoverable=True,
                    )
                    return

                targeted_repair = _targeted_generated_file_repair_instructions(
                    file_path=request.file_path,
                    deterministic_error=deterministic_error,
                )

                if request.file_path == "SKILL.md":
                    targeted_repair += (
                        "\n\n额外修复目标：SKILL.md 必须与蓝图意图一致。"
                        "不得新增蓝图外能力、脚本、reference 或 asset；"
                        "必须覆盖 required_capabilities；不得包含 forbidden_capabilities；"
                        "assets/** 只能描述为上传/静态素材。"
                    )

                if request.file_path.startswith("references/"):
                    targeted_repair += (
                        "\n\n额外修复目标：references/*.md 必须是一份正式 Markdown 参考资料文档。"
                        "必须包含 YAML frontmatter，且 title/description 非空；"
                        "frontmatter 顶层只允许 title、description、source、license、metadata。"
                        "frontmatter 后必须有 Markdown 正文，正文必须包含标题，并提供可复用参考内容。"
                        "不要输出聊天式澄清问题、确认选项、状态说明或计划询问。"
                    )

                contract_text = _build_generated_file_contract_text(
                    request.file_path,
                    request.blueprint_text,
                    request.purpose,
                    role=request.role,
                    skill_plan_entry=effective_skill_plan_entry,
                )

                passed_checks_text = ""
                failed_checks_text = ""
                original_exc = stage_error.original
                if isinstance(original_exc, ContractValidationError):
                    passed_checks_text = _format_contract_checks(original_exc.results, passed=True)
                    failed_checks_text = _format_contract_checks(original_exc.results, passed=False)

                if attempt >= _MAX_FILE_REPAIR_ATTEMPTS:
                    error_message = (
                        f"文件内容生成失败：已自动修复 {attempt - 1} 次仍未通过。"
                        f"最后错误：{deterministic_error}"
                    )

                    logger.info(
                        "[Creator][generate_file] validation failed finally file=%s role=%s attempts=%d error=%s",
                        request.file_path,
                        request.role or "",
                        attempt,
                        deterministic_error,
                    )

                    yield _file_done_error_sse(
                        file_path=request.file_path,
                        role=request.role,
                        error=error_message,
                        error_type="repair_limit_exceeded",
                        content=candidate or "",
                        recoverable=True,
                    )
                    return

                yield _sse({
                    "type": "validation",
                    "status": "repairing",
                    "success": False,
                    "file_path": request.file_path,
                    "role": request.role,
                    "validation": {
                        "status": "repairing",
                        "attempt": attempt,
                        "source": error_source,
                        "layer": stage_error.layer,
                        "error": deterministic_error,
                    },
                })

                try:
                    validator_report = await _run_generated_file_validator_round(
                        file_path=request.file_path,
                        content=candidate,
                        deterministic_error=deterministic_error,
                        requested_model=route.model,
                        targeted_repair=targeted_repair,
                        contract_text=contract_text,
                        passed_checks_text=passed_checks_text,
                        failed_checks_text=failed_checks_text,
                        repair_mode=_repair_mode_for_first_round(
                            source=error_source,
                            file_path=request.file_path,
                            attempt=attempt,
                        ),
                    )

                    feedback = _format_file_validator_feedback(
                        deterministic_error,
                        validator_report,
                        targeted_repair=targeted_repair,
                        file_path=request.file_path,
                    )

                    repair_mode = _repair_mode_for_first_round(
                        source=error_source,
                        file_path=request.file_path,
                        attempt=attempt,
                    )

                    repaired_candidate = await _repair_generated_file_with_feedback(
                        prompt_messages=prompt_messages,
                        model=route.model,
                        file_path=request.file_path,
                        previous_content=candidate,
                        validation_error=feedback,
                        targeted_repair=targeted_repair,
                        contract_text=contract_text,
                        passed_checks_text=passed_checks_text,
                        failed_checks_text=failed_checks_text,
                        repair_mode=repair_mode,
                        skill_plan_entry=effective_skill_plan_entry,
                    )

                except Exception as repair_exc:
                    logger.exception(
                        "[Creator][generate_file][repair_failed] file=%s source=%s layer=%s attempt=%d",
                        request.file_path,
                        error_source,
                        stage_error.layer,
                        attempt,
                    )
                    yield _file_done_error_sse(
                        file_path=request.file_path,
                        role=request.role,
                        error=f"文件内容修复阶段异常：{type(repair_exc).__name__}: {repair_exc}",
                        error_type="repair_failed",
                        content=candidate or "",
                        recoverable=True,
                    )
                    return
                if request.file_path.startswith("scripts/") and repaired_candidate.strip() == (candidate or "").strip():
                    logger.warning(
                        "[Creator][generate_file][repair_noop] file=%s source=%s layer=%s attempt=%d repair_mode=%s",
                        request.file_path,
                        error_source,
                        stage_error.layer,
                        attempt,
                        repair_mode,
                    )

                    repaired_candidate = await _repair_generated_file_with_feedback(
                        prompt_messages=prompt_messages,
                        model=route.model,
                        file_path=request.file_path,
                        previous_content=candidate,
                        validation_error=(
                            feedback
                            + "\n\n上一轮 repair 没有改变文件内容，这是无效修复。"
                            + "现在必须执行 strict_patch：只修改 deterministic_error / localization 指出的失败行附近代码，"
                            + "必须落实 localization.minimal_edit，不得保留 traceback 指出的错误表达式。"
                        ),
                        targeted_repair=targeted_repair,
                        contract_text=contract_text,
                        passed_checks_text=passed_checks_text,
                        failed_checks_text=failed_checks_text,
                        repair_mode="strict_patch",
                        skill_plan_entry=effective_skill_plan_entry,
                    )

                candidate = repaired_candidate

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )

@router.post("/write-file", response_model=WriteFileResponse)
async def write_file(request: WriteFileRequest):
    """Write already-validated generated content to disk.

    /write-file 只落盘：
    - 不做 content contract 校验；
    - 不做 script responsibility review；
    - 不做 script smoke trial run；
    - 不做 SKILL.md 蓝图一致性或跨文件检查；
    - 不重新 canonicalize，避免前端展示内容与落盘内容不一致。

    所有生成内容是否合格，必须在 /generate-file 的生成循环中解决。
    """
    skill_name = _validate_skill_name(request.skill_name)
    _validate_file_path(request.file_path)

    if request.file_path.startswith("assets/") and (request.skill_plan_entry or {}).get("asset_source") != "bundled":
        raise HTTPException(
            status_code=400,
            detail=f"{request.file_path} 属于 assets 静态素材目录；只有 source=bundled 的预置静态资源可写入，source=user_upload 必须通过 /api/creator/upload-asset 上传。",
        )

    skill_dir = settings.skills_path / skill_name
    if not skill_dir.exists():
        raise HTTPException(status_code=404, detail=f"Skill 不存在：{skill_name}")

    content = request.content or ""

    target_path = skill_dir / request.file_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(content, encoding="utf-8")

    return WriteFileResponse(
        success=True,
        path=str(target_path),
        bytes=len(content.encode("utf-8")),
        message=f"已写入：{request.file_path}",
    )

@dataclass(frozen=True)
class E2EWorkflowCommand:
    ordinal: int
    source_path: str
    script_path: str
    raw_command: str
    runner: str
    argv_template: dict[str, Any]

@dataclass(frozen=True)
class E2EStepTrace:
    """Creator E2E workflow boundary trace.

    中间步骤只记录边界，不要求平台协议字段：
    - command JSON argv
    - command placeholders
    - real stdout JSON
    - payload.update(stdout_json) 后新增字段

    最后一步才要求 stdout JSON 能被 sandbox 平台消费。
    """

    ordinal: int
    script_path: str
    raw_command: str
    placeholders: list[str] = field(default_factory=list)
    argv_keys: list[str] = field(default_factory=list)
    stdout_keys: list[str] = field(default_factory=list)
    new_keys: list[str] = field(default_factory=list)
    artifact_paths: list[str] = field(default_factory=list)
    argv_shape: dict[str, str] = field(default_factory=dict)
    stdout_shape: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class E2EFailure:
    failed_step_index: int
    target_file: str
    target_region: str
    failed_command: str
    input_payload: dict[str, Any] = field(default_factory=dict)
    rendered_payload: dict[str, Any] = field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""
    return_code: int | None = None
    expected: str = ""
    actual: str = ""
    repair_instruction: str = ""
    layer: str = ""
    artifact_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "failed_step_index": self.failed_step_index,
            "target_file": self.target_file,
            "target_region": self.target_region,
            "failed_command": self.failed_command,
            "input_payload": self.input_payload,
            "rendered_payload": self.rendered_payload,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "return_code": self.return_code,
            "expected": self.expected,
            "actual": self.actual,
            "repair_instruction": self.repair_instruction,
            "layer": self.layer,
            "artifact_paths": self.artifact_paths,
        }


def _format_e2e_failure(failure: E2EFailure) -> str:
    return (
        f"E2E_REPAIR_TARGET={failure.target_file}\n"
        f"E2E_LAYER={failure.layer}\n"
        "E2E_STRUCTURED_FAILURE="
        + json.dumps(failure.to_dict(), ensure_ascii=False, sort_keys=True)
    )


def _iter_markdown_shell_blocks_with_source(content: str, *, source_path: str) -> list[tuple[str, str]]:
    """Return shell/bash fenced blocks in document order.

    Keep this parser aligned with _extract_script_command_templates(), otherwise
    file-level validation and final E2E validation can disagree.
    """
    blocks: list[tuple[str, str]] = []

    for info, body in _iter_markdown_fenced_blocks(content):
        if not _is_shell_fence_info(info):
            continue

        command = body.strip()
        if not command:
            continue

        if "scripts/" in command.replace("\\", "/"):
            blocks.append((source_path, command))

    return blocks

def _validate_skill_md_final_resource_existence(skill_name: str) -> None:
    """Final package-time check: SKILL.md references must exist on disk.

    This is not used during file generation. It should run only after all files
    have been generated/uploaded and before packaging.
    """
    skill_name = _validate_skill_name(skill_name)
    skill_dir = settings.skills_path / skill_name
    skill_md_path = skill_dir / "SKILL.md"

    if not skill_md_path.exists():
        raise ValueError("缺少 SKILL.md，无法进行最终资源存在性校验。")

    content = skill_md_path.read_text(encoding="utf-8")

    _validate_skill_md_against_existing_files(
        skill_name,
        content,
        blueprint_text="",
        require_existing=True,
    )

def _ordered_reference_paths_in_skill_md(skill_md: str) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for match in _SKILL_FILE_PATH_RE.finditer(skill_md or ""):
        path = match.group(1).strip()
        if path.startswith("references/") and path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered

_E2E_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def _json_shape(value: Any) -> str:
    """Compact runtime shape for E2E trace."""
    if isinstance(value, dict):
        keys = ", ".join(sorted(str(k) for k in value.keys())[:12])
        return f"object({keys})"
    if isinstance(value, list):
        if not value:
            return "list[0]"
        return f"list[{len(value)}]<{_json_shape(value[0])}>"
    if isinstance(value, str):
        return "string(non_empty)" if value else "string(empty)"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if value is None:
        return "null"
    return type(value).__name__


def _json_object_shape(obj: dict[str, Any]) -> dict[str, str]:
    return {str(k): _json_shape(v) for k, v in obj.items()}


def _format_json_shape(obj: dict[str, Any]) -> str:
    if not obj:
        return "{}"
    shape = _json_object_shape(obj)
    return json.dumps(shape, ensure_ascii=False, sort_keys=True)

_SANDBOX_TERMINAL_OUTPUT_KEYS = {
    "text",
    "markdown",
    "image_path",
    "image_paths",
    "pdf_path",
    "docx_path",
    "pptx_path",
    "html_path",
    "file_paths",
    "file_outputs",
}


def _e2e_trace_line(trace: E2EStepTrace) -> str:
    return (
        f"step={trace.ordinal} "
        f"script={trace.script_path} "
        f"placeholders={trace.placeholders} "
        f"argv_keys={trace.argv_keys} "
        f"stdout_keys={trace.stdout_keys} "
        f"new_keys={trace.new_keys} "
        f"artifact_paths={trace.artifact_paths} "
        f"argv_shape={json.dumps(trace.argv_shape, ensure_ascii=False, sort_keys=True)} "
        f"stdout_shape={json.dumps(trace.stdout_shape, ensure_ascii=False, sort_keys=True)}"
    )


def _format_e2e_trace(traces: list[E2EStepTrace]) -> str:
    if not traces:
        return "（暂无成功步骤）"
    return "\n".join(_e2e_trace_line(trace) for trace in traces)


def _terminal_output_expected_type(key: str) -> str:
    if key in {"text", "markdown", "image_path", "pdf_path", "docx_path", "pptx_path", "html_path"}:
        return "non-empty string"
    if key in {"image_paths", "file_paths", "file_outputs"}:
        return "non-empty list[string]"
    return "platform terminal field"


def _valid_terminal_output_value(key: str, value: Any) -> bool:
    if key in {"text", "markdown", "image_path", "pdf_path", "docx_path", "pptx_path", "html_path"}:
        return isinstance(value, str) and bool(value.strip())
    if key in {"image_paths", "file_paths", "file_outputs"}:
        return (
            isinstance(value, list)
            and bool(value)
            and all(isinstance(item, str) and item.strip() for item in value)
        )
    return False


def _invalid_terminal_output_values(payload: dict[str, Any]) -> list[dict[str, str]]:
    invalid: list[dict[str, str]] = []
    for key in sorted(_SANDBOX_TERMINAL_OUTPUT_KEYS):
        if key not in payload:
            continue
        value = payload.get(key)
        if _valid_terminal_output_value(key, value):
            continue
        invalid.append({
            "key": key,
            "expected_type": _terminal_output_expected_type(key),
            "actual_type": _json_shape(value),
        })
    return invalid


def _has_sandbox_terminal_output(payload: dict[str, Any]) -> bool:
    """Return whether final stdout JSON is consumable by sandbox runtime.

    这里校验的是平台与 Skill 交互的最终输出协议，不校验中间步骤。
    """
    return any(
        _valid_terminal_output_value(key, payload.get(key))
        for key in _SANDBOX_TERMINAL_OUTPUT_KEYS
        if key in payload
    )

def _validate_final_platform_output_contract(
    *,
    command: E2EWorkflowCommand,
    stdout_json: dict[str, Any],
    traces: list[E2EStepTrace],
) -> None:
    """Validate only the final workflow output against sandbox platform protocol.

    中间步骤 stdout 可以是任意 JSON object；
    最后一步必须输出 sandbox 能展示/下载的标准字段。
    """
    if _has_sandbox_terminal_output(stdout_json):
        return

    invalid_terminal_values = _invalid_terminal_output_values(stdout_json)
    if invalid_terminal_values:
        details = {
            "stdout_shape": _json_object_shape(stdout_json),
            "invalid_terminal_keys": [item["key"] for item in invalid_terminal_values],
            "invalid_terminal_values": invalid_terminal_values,
        }
        raise ValueError(
            _e2e_error(
                target=command.script_path,
                layer="final_platform_output_value_invalid",
                message=(
                    f"第 {command.ordinal} 步 {command.script_path} 是 workflow 最后一步，"
                    "stdout JSON 包含 sandbox 平台字段，但字段值类型/内容不合法。\n"
                    f"stdout_shape={json.dumps(details['stdout_shape'], ensure_ascii=False, sort_keys=True)}\n"
                    f"invalid_terminal_keys={json.dumps(details['invalid_terminal_keys'], ensure_ascii=False)}\n"
                    f"invalid_terminal_values={json.dumps(invalid_terminal_values, ensure_ascii=False, sort_keys=True)}\n"
                    "每个 invalid_terminal_values 项均包含 expected_type 和 actual_type。\n\n"
                    "已成功执行的前序边界 trace：\n"
                    f"{_format_e2e_trace(traces)}"
                ),
            )
        )

    raise ValueError(
        _e2e_error(
            target=command.script_path,
            layer="final_platform_output_contract",
            message=(
                f"第 {command.ordinal} 步 {command.script_path} 是 workflow 最后一步，"
                "但 stdout JSON 没有包含 sandbox 可消费的最终输出字段。\n"
                f"当前 stdout 字段：{sorted(stdout_json.keys())}\n"
                f"stdout_shape={json.dumps(_json_object_shape(stdout_json), ensure_ascii=False, sort_keys=True)}\n"
                f"平台允许的最终输出字段：{sorted(_SANDBOX_TERMINAL_OUTPUT_KEYS)}\n\n"
                "注意：中间步骤可以使用任意内部字段名，不需要对齐平台协议；"
                "但最后一步必须输出平台字段，例如 text、markdown、image_paths、"
                "pdf_path、docx_path、pptx_path、html_path、file_paths 或 file_outputs。\n\n"
                "已成功执行的前序边界 trace：\n"
                f"{_format_e2e_trace(traces)}"
            ),
        )
    )

def _placeholder_exprs_from_value(value: Any) -> list[str]:
    exprs: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for item in v.values():
                walk(item)
        elif isinstance(v, list):
            for item in v:
                walk(item)
        elif isinstance(v, str):
            exprs.extend(match.group(1).strip() for match in _E2E_PLACEHOLDER_RE.finditer(v))

    walk(value)
    return exprs


def _placeholder_root(expr: str) -> str:
    expr = str(expr or "").strip()
    if not expr:
        return ""
    return re.split(r"[.\[]", expr, maxsplit=1)[0].strip()


def _collect_placeholders_from_payload_template(template: dict[str, Any]) -> set[str]:
    placeholders: set[str] = set()
    for value in template.values():
        placeholders.update(_collect_placeholders_from_value(value))
    return placeholders


def _resolve_e2e_payload_expr(
    expr: str,
    *,
    payload: dict[str, Any],
    missing: list[str],
) -> Any:
    """Resolve placeholder expression against current runtime payload.

    Supports:
    - {{text_content}}
    - {{image_paths.0}}
    - {{foo.bar.0}}
    """
    expr = str(expr or "").strip()
    if not expr:
        missing.append(expr)
        return ""

    parts = expr.split(".")
    root = parts[0].strip()

    if root not in payload:
        missing.append(expr)
        return ""

    value: Any = payload[root]

    for part in parts[1:]:
        part = part.strip()
        if isinstance(value, list):
            try:
                index = int(part)
            except ValueError:
                missing.append(expr)
                return ""
            if index < 0 or index >= len(value):
                missing.append(expr)
                return ""
            value = value[index]
            continue

        if isinstance(value, dict):
            if part not in value:
                missing.append(expr)
                return ""
            value = value[part]
            continue

        missing.append(expr)
        return ""

    return value


def _e2e_command_placeholders(command: E2EWorkflowCommand) -> list[str]:
    return _placeholder_exprs_from_value(command.argv_template)

def _looks_like_directory_tree_block(text: str) -> bool:
    """Heuristically detect directory-tree/documentation blocks.

    These blocks are often rendered as plain Markdown fences and may contain
    scripts/ paths, but they are not executable workflow commands.
    """
    text = text or ""
    if any(marker in text for marker in ("├──", "└──", "│", "─")):
        return True

    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False

    # Typical tree blocks contain multiple directory/file-looking lines and no
    # shell runner at the beginning.
    first = lines[0].strip()
    first_word = first.split(maxsplit=1)[0] if first.split() else ""
    if first_word in {"python", "python3", "node", "bash", "sh"}:
        return False

    treeish_count = 0
    for line in lines:
        stripped = line.strip()
        if stripped.endswith("/") or stripped.startswith(("scripts/", "references/", "assets/")):
            treeish_count += 1
        elif re.match(r"^[A-Za-z0-9_.-]+\.(py|md|json|yaml|yml|txt|pdf|docx|pptx)$", stripped):
            treeish_count += 1

    return len(lines) >= 2 and treeish_count >= 2


def _is_valid_e2e_script_path(script_path: str) -> bool:
    """Return whether a token is a concrete executable script path.

    E2E must not accept directories such as scripts/ as workflow steps.
    """
    normalized = (script_path or "").replace("\\", "/").strip()
    if not normalized.startswith("scripts/"):
        return False
    if normalized.endswith("/"):
        return False

    path = Path(normalized)
    if not path.name or path.name in {".", ".."}:
        return False
    if not path.suffix:
        return False

    return path.suffix.lower() in {
        ".py",
    }

def _parse_e2e_workflow_command(
    *,
    command: str,
    ordinal: int,
    source_path: str,
) -> E2EWorkflowCommand | None:
    """Parse one SKILL.md shell command into executable E2E workflow step.

    Strict rule:
    - The fenced block must contain exactly one effective shell command.
    - The command must invoke a concrete scripts/<file> path, not scripts/.
    - The script path must be followed by exactly one JSON object argv.
    - Directory-tree/documentation blocks are ignored.
    """
    raw_command = (command or "").strip()
    if not raw_command:
        return None

    if _looks_like_directory_tree_block(raw_command):
        return None

    effective_lines: list[str] = []
    for line in raw_command.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        effective_lines.append(stripped)

    if not effective_lines:
        return None

    if len(effective_lines) != 1:
        raise ValueError(
            _e2e_error(
                target=source_path,
                layer="command_block_multiple",
                message=(
                    f"{source_path} 第 {ordinal} 个可执行 fenced block 中包含多条有效命令。\n"
                    "二次 E2E 要求每个 bash/sh/shell block 只包含一条脚本调用命令。\n"
                    f"原始块：{raw_command}"
                ),
            )
        )

    command = effective_lines[0]

    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise ValueError(
            _e2e_error(
                target=source_path,
                layer="command_parse",
                message=(
                    f"{source_path} 第 {ordinal} 个命令无法被 shell 解析：{exc}\n"
                    f"原始命令：{command}"
                ),
            )
        ) from exc

    if not parts:
        return None

    runner = Path(parts[0]).name
    if runner not in {"python", "python3"}:
        # Creator workflow E2E accepts one command protocol only.
        return None

    script_idx: int | None = None
    script_path = ""

    for idx, part in enumerate(parts[1:], start=1):
        normalized = part.replace("\\", "/")

        candidate = ""
        if normalized.startswith("scripts/"):
            candidate = normalized

        if not candidate:
            continue

        if not _is_valid_e2e_script_path(candidate):
            # Example: scripts/ directory in a tree/list. Not a workflow step.
            return None

        script_idx = idx
        script_path = candidate
        break

    if script_idx is None:
        return None

    if script_idx + 1 >= len(parts):
        raise ValueError(
            _e2e_error(
                target=source_path,
                layer="command_argv_missing",
                message=(
                    f"{source_path} 第 {ordinal} 步 {script_path} 缺少 JSON argv。\n"
                    f"命令必须形如：python {script_path} '{{\"payload\":{{\"user_request\":\"{{{{user_request}}}}\"}}}}'\n"
                    f"原始命令：{command}"
                ),
            )
        )

    if script_idx != 1:
        raise ValueError(
            _e2e_error(
                target=source_path,
                layer="command_protocol",
                message=(
                    f"{source_path} 第 {ordinal} 步必须直接调用 scripts/*.py：python {script_path} '<JSON object>'。\n"
                    f"原始命令：{command}"
                ),
            )
        )

    if script_idx + 2 < len(parts):
        raise ValueError(
            _e2e_error(
                target=source_path,
                layer="command_argv_extra",
                message=(
                    f"{source_path} 第 {ordinal} 步 {script_path} 的 JSON argv 后存在额外参数：{parts[script_idx + 2:]!r}。\n"
                    "二次 E2E 校验要求脚本路径后只跟一个 JSON object argv。\n"
                    f"原始命令：{command}"
                ),
            )
        )

    try:
        argv_template = json.loads(parts[script_idx + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(
            _e2e_error(
                target=source_path,
                layer="command_json_parse",
                message=(
                    f"{source_path} 第 {ordinal} 步 {script_path} 的 JSON argv 不可解析：{exc.msg}\n"
                    f"argv={parts[script_idx + 1]!r}\n"
                    f"原始命令：{command}"
                ),
            )
        ) from exc

    if not isinstance(argv_template, dict):
        raise ValueError(
            _e2e_error(
                target=source_path,
                layer="command_json_type",
                message=(
                    f"{source_path} 第 {ordinal} 步 {script_path} 的 argv 必须是 JSON object。\n"
                    f"原始命令：{command}"
                ),
            )
        )

    return E2EWorkflowCommand(
        ordinal=ordinal,
        source_path=source_path,
        script_path=script_path,
        raw_command=command,
        runner=runner,
        argv_template=argv_template,
    )

def _extract_e2e_workflow_commands(skill_dir: Path, skill_md: str) -> list[E2EWorkflowCommand]:
    """Extract executable E2E workflow commands from SKILL.md only.

    references/*.md are reference resources only and must never become E2E steps.
    """
    raw_blocks = _iter_markdown_shell_blocks_with_source(skill_md, source_path="SKILL.md")

    commands: list[E2EWorkflowCommand] = []
    seen: set[tuple[str, str]] = set()
    ordinal = 0

    for source_path, raw_command in raw_blocks:
        parsed = _parse_e2e_workflow_command(
            command=raw_command,
            ordinal=ordinal + 1,
            source_path="SKILL.md",
        )
        if parsed is None:
            continue

        key = (parsed.script_path, parsed.raw_command)
        if key in seen:
            continue

        seen.add(key)
        ordinal += 1
        commands.append(replace(parsed, ordinal=ordinal, source_path="SKILL.md"))

    return commands


def _collect_placeholders_from_value(value: Any) -> set[str]:
    """Collect root placeholder names from command argv template.

    支持：
    - {{topic}}
    - {{image_paths.0}}
    - {{result.pdf_path}}

    seed 初始输入时只取 root key。
    """
    placeholders: set[str] = set()

    if isinstance(value, str):
        for match in _E2E_PLACEHOLDER_RE.finditer(value):
            root = _placeholder_root(match.group(1))
            if root:
                placeholders.add(root)

    elif isinstance(value, dict):
        for item in value.values():
            placeholders.update(_collect_placeholders_from_value(item))

    elif isinstance(value, list):
        for item in value:
            placeholders.update(_collect_placeholders_from_value(item))

    return placeholders


def _collect_placeholders_from_payload_template(template: dict[str, Any]) -> set[str]:
    placeholders: set[str] = set()
    for value in template.values():
        placeholders.update(_collect_placeholders_from_value(value))
    return placeholders


def _render_e2e_template_value(
    value: Any,
    *,
    payload: dict[str, Any],
    missing: list[str],
) -> Any:
    if isinstance(value, str):
        whole = re.fullmatch(r"\{\{\s*([^{}]+?)\s*\}\}", value.strip())
        if whole:
            return _resolve_e2e_payload_expr(
                whole.group(1),
                payload=payload,
                missing=missing,
            )

        def replace_match(match: re.Match[str]) -> str:
            rendered = _resolve_e2e_payload_expr(
                match.group(1),
                payload=payload,
                missing=missing,
            )
            if isinstance(rendered, (dict, list)):
                return json.dumps(rendered, ensure_ascii=False)
            return str(rendered)

        return _E2E_PLACEHOLDER_RE.sub(replace_match, value)

    if isinstance(value, dict):
        return {
            str(key): _render_e2e_template_value(item, payload=payload, missing=missing)
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [
            _render_e2e_template_value(item, payload=payload, missing=missing)
            for item in value
        ]

    return value


def _render_e2e_command_payload(
    command: E2EWorkflowCommand,
    *,
    payload: dict[str, Any],
    traces: list[E2EStepTrace] | None = None,
) -> dict[str, Any]:
    missing: list[str] = []

    rendered = {
        str(key): _render_e2e_template_value(value, payload=payload, missing=missing)
        for key, value in command.argv_template.items()
    }

    if missing:
        unique_missing = sorted(set(missing))
        available = sorted(payload.keys())

        raise ValueError(
            _e2e_error(
                target=command.source_path,
                layer="external_input_missing" if command.ordinal == 1 else "e2e_dataflow_missing",
                message=(
                    ("平台外部输入缺失，第一条命令不能引用无确定来源字段。" if command.ordinal == 1 else "Skill 内部 dataflow 缺失，后续命令只能引用已有 context 或前序 stdout 字段。")
                    + "\n"
                    + f"第 {command.ordinal} 步 {command.script_path} 的命令模板引用了当前 payload 中不存在的字段："
                    f"{', '.join(unique_missing)}。\n"
                    f"当前可用字段：{', '.join(available) or '(无)'}。\n"
                    f"命令来源：{command.source_path}\n"
                    f"原始命令：{command.raw_command}\n\n"
                    "已成功执行的前序边界 trace：\n"
                    f"{_format_e2e_trace(traces or [])}\n\n"
                    "这只表示 SKILL.md 当前失败步骤的命令占位符，"
                    "无法从用户初始输入或前序 stdout JSON 中解析。"
                    "优先局部修复当前失败步骤的 SKILL.md 命令块；"
                    "不要修改已成功 trace 对应的前序步骤。"
                ),
            )
        )

    return rendered


def _seed_initial_e2e_payload(
    commands: list[E2EWorkflowCommand],
    *,
    external_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Seed Creator E2E with a non-empty generic external input envelope.

    第二轮 E2E 需要真实跑 workflow。若 validate-skill 没传用户消息，
    也必须给 {{user_request}} / {{input}} / {{text}} / {{payload}}
    一个非空通用测试值，否则第一步会收到空字符串，导致参数接入审查误判。

    这里仍然不发明业务字段，只补平台通用外部输入 envelope。
    """
    base_context = external_context
    if not isinstance(base_context, dict):
        base_context = build_creator_external_input_context(messages=[])

    payload: dict[str, Any] = dict(base_context or {})

    seed_value = ""
    for key in ("user_request", "input", "text", "payload"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            seed_value = value.strip()
            break

    if not seed_value:
        seed_value = (
            "Creator E2E 验证输入：请根据这个请求完成当前 Skill 的主要任务，"
            "内容包含中文、English words 和标点，用于验证参数传递、脚本消费和输出闭环。"
        )

    for key in ("user_request", "input", "text", "payload"):
        if not _json_value_non_empty(payload.get(key)):
            payload[key] = seed_value

    if not isinstance(payload.get("fields"), dict):
        payload["fields"] = {}

    if not isinstance(payload.get("options"), dict):
        payload["options"] = {}

    if not isinstance(payload.get("input_files"), list):
        payload["input_files"] = []

    if not isinstance(payload.get("files"), list):
        payload["files"] = list(payload.get("input_files") or [])

    return payload


def _e2e_error(*, target: str, layer: str, message: str) -> str:
    failure = {
        "failed_step_index": 0,
        "target_file": target,
        "target_region": "frontmatter" if "frontmatter" in layer else ("workflow block" if target == "SKILL.md" else "run()"),
        "failed_command": "",
        "input_payload": {},
        "stdout": "",
        "stderr": message,
        "return_code": None,
        "expected": "Creator E2E step must be executable and produce valid JSON/artifacts.",
        "actual": message,
        "repair_instruction": f"只修改 {target} 中与 {layer} 失败相关的最小区域，不修改其它文件。",
        "layer": layer,
    }
    return f"E2E_REPAIR_TARGET={target}\nE2E_LAYER={layer}\nE2E_STRUCTURED_FAILURE={json.dumps(failure, ensure_ascii=False, sort_keys=True)}\n{message}"


def _e2e_repair_target_from_errors(errors: list[str]) -> str:
    for error in errors:
        match = re.search(r"^E2E_REPAIR_TARGET=([^\n]+)", error)
        if match:
            target = match.group(1).strip()
            if target:
                return target
    return "SKILL.md"


def _copy_skill_dir_for_e2e(
    skill_name: str,
    *,
    source_skill_dir: Path | None = None,
) -> tuple[tempfile.TemporaryDirectory, Path]:
    source_dir = (source_skill_dir or (settings.skills_path / skill_name)).resolve()

    tmp = tempfile.TemporaryDirectory(prefix="creator-e2e-skill-")
    tmp_root = Path(tmp.name)
    trial_skill_dir = tmp_root / skill_name

    shutil.copytree(
        source_dir,
        trial_skill_dir,
        ignore=shutil.ignore_patterns(
            ".venv",
            "__pycache__",
            "*.pyc",
            ".pytest_cache",
        ),
    )

    return tmp, trial_skill_dir


def _runner_matches_command_runtime(command: E2EWorkflowCommand, entry: SkillPlanEntry) -> bool:
    runner = Path(command.runner or "").name
    if entry.runtime == "python":
        return runner.startswith("python")
    if entry.runtime == "node":
        return runner == "node"
    if entry.runtime in {"bash", "shell"}:
        return runner in {"bash", "sh"}
    return True

def _run_e2e_step_argument_effect_review(
    *,
    command: E2EWorkflowCommand,
    script_content: str,
    skill_plan_entry: SkillPlanEntry,
    rendered_payload: dict[str, Any],
    stdout_json: dict[str, Any],
    artifact_paths: list[str],
    trace: E2EStepTrace,
    previous_traces: list[E2EStepTrace],
    requested_model: str | None = None,
) -> dict[str, Any]:
    """Second-round E2E step interface + argument-effect review.

    第二轮只做接口闭环，不做第一轮责任审查：

    1. SKILL.md 当前 command 是否把当前 step 所需输入/控制参数传进 rendered_payload；
    2. 当前脚本是否真实读取 rendered_payload；
    3. rendered_payload 是否影响 stdout_json 或 artifact_paths；
    4. 如果失败，归因到 SKILL.md 参数映射，或当前脚本参数消费。

    不写业务字段词表，不要求固定字段名。
    """

    rendered_payload = rendered_payload if isinstance(rendered_payload, dict) else {}
    stdout_json = stdout_json if isinstance(stdout_json, dict) else {}
    artifact_paths = artifact_paths if isinstance(artifact_paths, list) else []

    declared_inputs = [
        str(item).strip()
        for item in (getattr(skill_plan_entry, "inputs", []) or [])
        if str(item).strip()
    ]
    declared_outputs = [
        str(item).strip()
        for item in (getattr(skill_plan_entry, "outputs", []) or [])
        if str(item).strip()
    ]

    placeholders = sorted(_e2e_command_placeholders(command))
    argv_template = command.argv_template if isinstance(command.argv_template, dict) else {}

    # 只有完全没有 argv、没有 placeholder、没有声明输入时，才跳过。
    # 之前 rendered_payload 空就直接跳过，会漏掉“SKILL.md 没传参数”的接口问题。
    if not rendered_payload and not argv_template and not placeholders and not declared_inputs:
        return {
            "passed": True,
            "model": None,
            "advisory_notes": [
                "当前 step 没有声明输入、没有 argv_template、没有 placeholder，视为 no-input deterministic step，跳过接口审查。"
            ],
        }

    route = route_model(
        VALIDATOR_TASK,
        requested_model=requested_model,
        reason=f"creator e2e step interface/argument review: {command.script_path}",
    )

    local_block_payload = {
        "raw_command": command.raw_command,
        "argv_template": command.argv_template,
        "placeholders": placeholders,
    }

    messages = [
        {
            "role": "system",
            "content": (
                "你是 Creator 第二轮 E2E 当前 step 接口对齐审查模型，只输出严格 JSON object。\n\n"

                "你只判断当前 step 的接口闭环：\n"
                "1. SKILL.md 当前 bash command 是否把当前脚本需要的输入/控制参数传入 rendered_payload；\n"
                "2. 当前脚本是否真实读取 rendered_payload；\n"
                "3. rendered_payload 中的有效信息是否影响 stdout_json 或 artifact_paths。\n\n"

                "重要边界：\n"
                "- 不做第一轮脚本职责审查；脚本功能是否完整由 _run_script_responsibility_review 负责。\n"
                "- 不判断完整 SKILL.md 写得好不好。\n"
                "- 不判断最终产物审美质量。\n"
                "- 不要求固定字段名。\n"
                "- 不允许套用业务字段词表。\n"
                "- 允许脚本通过 payload、input、fields、options、统一对象、别名字段或等价结构接收参数。\n"
                "- 如果字段名不同但语义已传入并被脚本消费，应 passed=true。\n"
                "- 如果 rendered_payload 缺少当前 step 必需信息，failure_kind=missing_payload，target_file=SKILL.md。\n"
                "- 如果 rendered_payload 已传入合理信息，但脚本没有读取或被默认值覆盖，failure_kind=script_not_consuming_payload，target_file=当前脚本。\n"
                "- 如果当前 step 输出了内容，但后续映射接不上，failure_kind=output_mapping_mismatch，target_file=SKILL.md。\n"
                "- 如果没有接口问题，返回 passed=true。\n\n"

                "返回 JSON：\n"
                "{\n"
                "  \"passed\": true|false,\n"
                "  \"target_file\": \"SKILL.md 或 当前脚本路径\",\n"
                "  \"failure_kind\": \"missing_payload|script_not_consuming_payload|output_mapping_mismatch|none\",\n"
                "  \"problem\": \"具体问题\",\n"
                "  \"evidence\": \"从 command/rendered_payload/script/stdout/trace 中引用证据\",\n"
                "  \"repair_instruction\": \"只修改目标文件的最小区域\",\n"
                "  \"advisory_notes\": []\n"
                "}\n"
            ),
        },
        {
            "role": "user",
            "content": (
                f"当前 step：{command.ordinal}\n"
                f"当前脚本：{command.script_path}\n\n"

                "当前 SKILL.md workflow block 局部信息：\n"
                f"{json.dumps(local_block_payload, ensure_ascii=False, default=str)[:8000]}\n\n"

                "当前脚本 SkillPlanEntry：\n"
                f"{json.dumps(skill_plan_entry.__dict__, ensure_ascii=False, default=str)[:8000]}\n\n"

                "declared_inputs（语义参考，不是字段名硬约束）：\n"
                f"{json.dumps(declared_inputs, ensure_ascii=False, default=str)}\n\n"

                "declared_outputs（语义参考，不是字段名硬约束）：\n"
                f"{json.dumps(declared_outputs, ensure_ascii=False, default=str)}\n\n"

                "E2E rendered_payload：\n"
                f"{json.dumps(rendered_payload, ensure_ascii=False, default=str)[:10000]}\n\n"

                "当前 step stdout_json：\n"
                f"{json.dumps(stdout_json, ensure_ascii=False, default=str)[:10000]}\n\n"

                "当前 step artifact_paths：\n"
                f"{json.dumps(artifact_paths, ensure_ascii=False, default=str)}\n\n"

                "当前 step trace：\n"
                f"{_e2e_trace_line(trace)}\n\n"

                "前序成功 trace 摘要：\n"
                f"{_format_e2e_trace(previous_traces)[-5000:]}\n\n"

                "当前脚本源码（带行号）：\n"
                f"{_numbered_source(script_content)[-18000:]}\n\n"

                "审查要求：\n"
                "1. 先判断 SKILL.md 当前 command 是否把当前 step 必要输入/控制参数传进 rendered_payload。\n"
                "2. 再判断脚本是否真实读取并使用 rendered_payload。\n"
                "3. 不要根据固定字段名判断；只看语义是否传入、是否消费、是否影响输出。\n"
                "4. 如果是 SKILL.md 没传对，target_file=SKILL.md。\n"
                "5. 如果是脚本没接住，target_file=当前脚本。\n"
                "6. 如果没有接口闭环问题，passed=true。\n"
            ),
        },
    ]

    try:
        text = _complete_chat_once_sync_for_e2e(messages, route.model)
    except Exception as exc:
        logger.warning(
            "[Creator][E2E][argument_effect_review_unavailable] step=%s script=%s error=%s",
            command.ordinal,
            command.script_path,
            exc,
        )
        return {
            "passed": True,
            "model": route.model,
            "advisory_notes": [
                f"E2E 接口审查模型不可用，已跳过本轮 LLM 接口审查：{type(exc).__name__}: {exc}"
            ],
        }

    data = _parse_validator_json_object(text)
    if not isinstance(data, dict) or not data:
        logger.warning(
            "[Creator][E2E][argument_effect_review_invalid_json] step=%s script=%s raw=%s",
            command.ordinal,
            command.script_path,
            str(text or "")[:1000],
        )
        return {
            "passed": True,
            "model": route.model,
            "advisory_notes": [
                "E2E 接口审查模型没有返回合法 JSON object，已跳过本轮 LLM 接口审查。"
            ],
        }

    if data.get("passed") is True:
        return {
            "passed": True,
            "model": route.model,
            "advisory_notes": data.get("advisory_notes") if isinstance(data.get("advisory_notes"), list) else [],
        }

    failure_kind = str(data.get("failure_kind") or "").strip()
    if failure_kind not in {
        "missing_payload",
        "script_not_consuming_payload",
        "output_mapping_mismatch",
        "none",
    }:
        failure_kind = "script_not_consuming_payload"

    target_file = str(data.get("target_file") or "").strip()

    if failure_kind in {"missing_payload", "output_mapping_mismatch"}:
        target_file = "SKILL.md"
    elif failure_kind == "script_not_consuming_payload":
        target_file = command.script_path
    elif target_file not in {"SKILL.md", command.script_path}:
        target_file = command.script_path

    layer = (
        "e2e_step_argument_mapping"
        if target_file == "SKILL.md"
        else "e2e_step_argument_effect"
    )

    problem = str(
        data.get("problem")
        or data.get("reason")
        or data.get("message")
        or "当前 step 接口没有闭环，或 rendered_payload 没有真实影响当前脚本输出。"
    )

    evidence = str(
        data.get("evidence")
        or data.get("details")
        or "模型未提供 evidence。"
    )

    repair_instruction = str(
        data.get("repair_instruction")
        or data.get("minimal_edit")
        or data.get("fix")
        or (
            f"只修改 {target_file} 中当前 E2E step 接口映射/参数消费相关的最小区域，"
            "不要修改其它文件或已通过步骤。"
        )
    )

    return {
        "passed": False,
        "target_file": target_file,
        "layer": layer,
        "failure_kind": failure_kind,
        "problem": problem,
        "evidence": evidence,
        "repair_instruction": repair_instruction,
        "model": route.model,
        "raw_review": data,
    }

def _e2e_argument_effect_failure(
    *,
    command: E2EWorkflowCommand,
    review: dict[str, Any],
    rendered_payload: dict[str, Any],
    stdout_json: dict[str, Any],
    artifact_paths: list[str],
    traces: list[E2EStepTrace],
) -> str:
    target_file = str(review.get("target_file") or command.script_path)
    layer = str(review.get("layer") or "e2e_step_argument_effect")
    problem = str(review.get("problem") or "当前 E2E step 参数有效性审查失败。")
    evidence = str(review.get("evidence") or "")
    repair_instruction = str(
        review.get("repair_instruction")
        or f"只修改 {target_file} 中与当前 step 参数映射/消费相关的最小区域。"
    )

    return _format_e2e_failure(E2EFailure(
        failed_step_index=command.ordinal,
        target_file=target_file,
        target_region="workflow block" if target_file == "SKILL.md" else "run()",
        failed_command=command.raw_command,
        input_payload=rendered_payload,
        rendered_payload=rendered_payload,
        stdout=json.dumps(stdout_json, ensure_ascii=False, default=str)[-4000:],
        stderr=(
            f"{problem}\n\n"
            f"evidence:\n{evidence}\n\n"
            "已成功执行的前序边界 trace：\n"
            f"{_format_e2e_trace(traces)}"
        ),
        return_code=0,
        expected=(
            "最终 SKILL.md 当前 workflow block 传入的核心业务参数，"
            "必须被当前脚本真实消费，并影响当前 step 的 stdout_json 或 artifact。"
        ),
        actual=problem,
        repair_instruction=repair_instruction,
        layer=layer,
        artifact_paths=artifact_paths,
    ))

def _execute_e2e_python_command(
    *,
    command: E2EWorkflowCommand,
    trial_skill_dir: Path,
    rendered_payload: dict[str, Any],
    venv_python: Path,
) -> subprocess.CompletedProcess[str]:
    script_abs = trial_skill_dir / command.script_path
    return subprocess.run(
        [
            str(venv_python),
            str(script_abs),
            json.dumps(rendered_payload, ensure_ascii=False),
        ],
        cwd=str(trial_skill_dir / "scripts"),
        capture_output=True,
        text=True,
        timeout=_SCRIPT_TRIAL_TIMEOUT_SECONDS,
        env={**_build_script_runtime_env(trial_skill_dir), "SKILL_TRIAL_RUN": "1"},
    )


def _execute_e2e_node_command(
    *,
    command: E2EWorkflowCommand,
    trial_skill_dir: Path,
    rendered_payload: dict[str, Any],
) -> subprocess.CompletedProcess[str]:
    script_abs = trial_skill_dir / command.script_path
    return subprocess.run(
        [
            "node",
            str(script_abs),
            json.dumps(rendered_payload, ensure_ascii=False),
        ],
        cwd=str(trial_skill_dir / "scripts"),
        capture_output=True,
        text=True,
        timeout=_SCRIPT_TRIAL_TIMEOUT_SECONDS,
        env={**_build_script_runtime_env(trial_skill_dir), "SKILL_TRIAL_RUN": "1"},
    )


def _execute_e2e_shell_command(
    *,
    command: E2EWorkflowCommand,
    trial_skill_dir: Path,
    rendered_payload: dict[str, Any],
) -> subprocess.CompletedProcess[str]:
    script_abs = trial_skill_dir / command.script_path
    runner = "sh" if Path(command.runner or "").name == "sh" else "bash"
    return subprocess.run(
        [
            runner,
            str(script_abs),
            json.dumps(rendered_payload, ensure_ascii=False),
        ],
        cwd=str(trial_skill_dir / "scripts"),
        capture_output=True,
        text=True,
        timeout=_SCRIPT_TRIAL_TIMEOUT_SECONDS,
        env={**_build_script_runtime_env(trial_skill_dir), "SKILL_TRIAL_RUN": "1"},
    )


def _parse_e2e_stdout_json(
    *,
    command: E2EWorkflowCommand,
    proc: subprocess.CompletedProcess[str],
    trial_skill_dir: Path,
    content: str,
    entry: SkillPlanEntry,
    rendered_payload: dict[str, Any],
    trial_skill_md: str | None = None,
) -> dict[str, Any]:
    """Parse and validate one E2E step stdout.

    This function must not depend on an outer-scope ``trial_skill_md`` variable.
    Older callers may not pass trial_skill_md, so we recover it from the copied
    trial Skill directory when missing.
    """
    if trial_skill_md is None:
        skill_md_path = trial_skill_dir / "SKILL.md"
        trial_skill_md = skill_md_path.read_text(encoding="utf-8") if skill_md_path.is_file() else ""

    if proc.returncode != 0:
        raise ValueError(_format_e2e_failure(E2EFailure(
            failed_step_index=command.ordinal,
            target_file=command.script_path,
            target_region="run()",
            failed_command=command.raw_command,
            input_payload=rendered_payload,
            rendered_payload=rendered_payload,
            stdout=(proc.stdout or "")[-4000:],
            stderr=(proc.stderr or "")[-4000:],
            return_code=proc.returncode,
            expected="脚本必须成功退出、stdout 输出合法 JSON object，并真实完成该步骤职责。",
            actual=f"return_code={proc.returncode}",
            repair_instruction=f"只修改 {command.script_path} 中 run()/main 执行失败相关区域，不修改其它文件或已通过步骤。",
            layer="script_exit",
        )))

    try:
        refined_contract, _resolution = _contract_resolution_for_trial(
            command.script_path,
            trial_skill_md,
            entry.role,
            entry.__dict__,
        )
        _validate_trial_stdout_json(
            stdout=proc.stdout,
            content=content,
            args=[json.dumps(rendered_payload, ensure_ascii=False)],
            role=entry.role,
            skill_dir=trial_skill_dir,
            skill_plan_entry=entry.__dict__,
            canonical_contract=refined_contract,
        )
    except ValueError as exc:
        raise ValueError(_format_e2e_failure(E2EFailure(
            failed_step_index=command.ordinal,
            target_file=command.script_path,
            target_region="stdout output logic",
            failed_command=command.raw_command,
            input_payload=rendered_payload,
            rendered_payload=rendered_payload,
            stdout=(proc.stdout or "")[-4000:],
            stderr=(proc.stderr or "")[-4000:],
            return_code=proc.returncode,
            expected="stdout 必须是合法 JSON object，required outputs 存在；只有真正 artifact/path/file 语义字段才检查文件产物真实存在。",
            actual=f"stdout_contract_error={exc}",
            repair_instruction=(
                f"只修改 {command.script_path} 的 stdout/artifact 输出逻辑，不修改其它文件。"
                "如果失败字段是普通业务 stdout 字段，不要把它改成文件路径；"
                "如果失败字段是 pdf_path/image_path/file_outputs 等产物字段，则确保真实写入文件并返回正确路径。"
            ),
            layer="stdout_contract",
        ))) from exc

    try:
        parsed = json.loads((proc.stdout or "").strip())
    except json.JSONDecodeError as exc:
        raise ValueError(
            _e2e_error(
                target=command.script_path,
                layer="stdout_json_parse",
                message=(
                    f"第 {command.ordinal} 步 {command.script_path} stdout 不是合法 JSON。\n"
                    f"stdout={(proc.stdout or '')[-4000:]}"
                ),
            )
        ) from exc

    if not isinstance(parsed, dict):
        raise ValueError(
            _e2e_error(
                target=command.script_path,
                layer="stdout_json_type",
                message=f"第 {command.ordinal} 步 {command.script_path} stdout 必须是 JSON object。",
            )
        )

    return parsed


def _validate_e2e_script_static_preflight(*, file_path: str, content: str, skill_md: str) -> None:
    """E2E preflight for local safety/entry/JSON argv only.

    This intentionally does not enforce helper_preferred implementation choices
    or SkillPlan input key exactness. The real workflow run validates rendered
    argv, stdout context propagation, and final artifacts.
    """
    entry = _skill_plan_entry_for_file(file_path=file_path, blueprint_text=skill_md)

    if entry.language == "python":
        try:
            ast.parse(content)
        except SyntaxError as exc:
            raise ValueError(f"{file_path} 不是合法 Python 源码: {exc.msg}") from exc

    if not _script_has_main_entry(content, entry.runtime):
        raise ValueError(f"{file_path} 缺少 runtime={entry.runtime} 的入口或 stdout 输出。")

    commands = _extract_script_command_templates(skill_md, file_path)
    json_argv_commands = [command for command in commands if _command_uses_json_argv(command)]
    if json_argv_commands and not _script_reads_json_argv(content, entry.runtime):
        raise ValueError(
            f"{file_path} SKILL.md 命令传入 JSON argv，但脚本未按 runtime 读取 JSON argv（例如 Python json.loads(sys.argv[1])）。"
        )

    helper_required_capabilities = [
        capability
        for capability in _effective_required_capabilities_for_script(entry)
        if (get_tool_capability(capability) and get_tool_capability(capability).usage_policy == "helper_required")
    ]
    missing_required_helpers = _script_required_capability_failures(content, helper_required_capabilities)
    if missing_required_helpers:
        raise ValueError(
            "脚本没有调用这些 helper_required 能力对应接口："
            + ", ".join(missing_required_helpers)
        )


def _validate_e2e_command_static(
    *,
    command: E2EWorkflowCommand,
    trial_skill_dir: Path,
    skill_md: str,
    available_payload_keys: set[str] | None = None,
) -> SkillPlanEntry:
    source_path = trial_skill_dir / command.script_path
    if not source_path.is_file():
        raise ValueError(
            _e2e_error(
                target=command.source_path,
                layer="script_missing",
                message=f"第 {command.ordinal} 步引用的脚本不存在：{command.script_path}",
            )
        )

    entry = _skill_plan_entry_for_file(file_path=command.script_path, blueprint_text=skill_md)
    if not _runner_matches_command_runtime(command, entry):
        raise ValueError(
            _e2e_error(
                target=command.source_path,
                layer="runtime_mismatch",
                message=(
                    f"第 {command.ordinal} 步 {command.script_path} 的命令 runner={command.runner!r} "
                    f"与 SkillPlan.runtime={entry.runtime!r} 不一致。\n"
                    f"原始命令：{command.raw_command}"
                ),
            )
        )


    content = source_path.read_text(encoding="utf-8")
    try:
        _validate_e2e_script_static_preflight(
            file_path=command.script_path,
            content=content,
            skill_md=skill_md,
        )
    except ValueError as exc:
        raise ValueError(
            _e2e_error(
                target=command.script_path,
                layer="script_static_contract",
                message=f"第 {command.ordinal} 步 {command.script_path} 静态合同失败：{exc}",
            )
        ) from exc

    return entry


def _run_skill_workflow_e2e_once(
    skill_name: str,
    *,
    external_context: dict[str, Any] | None = None,
    source_skill_dir: Path | None = None,
    requested_model: str | None = None,
) -> list[str]:
    """Run SKILL.md workflow once.

    第二轮 E2E 职责：
    - 执行最终 SKILL.md 中的 workflow block；
    - 检查上下游 JSON 字段是否串起来；
    - 检查 stdout/artifact/final platform output；
    - 局部检查当前 SKILL.md block 传入参数是否真实影响当前 step 输出。

    不负责：
    - 判断脚本整体业务质量；
    - 判断完整 SKILL.md 写得好不好；
    - 判断 PDF/图片/文本审美质量；
    - 判断其它脚本职责。
    """

    source_skill_dir = (source_skill_dir or (settings.skills_path / skill_name)).resolve()
    skill_md_path = source_skill_dir / "SKILL.md"

    if not skill_md_path.is_file():
        return [
            _e2e_error(
                target="SKILL.md",
                layer="missing_skill_md",
                message="无法加载 SKILL.md。",
            )
        ]

    skill_md = skill_md_path.read_text(encoding="utf-8")
    errors: list[str] = []

    try:
        _validate_skill_md_contract(skill_md, skill_md)
    except ValueError as exc:
        errors.append(
            _e2e_error(
                target="SKILL.md",
                layer="skill_md_contract",
                message=f"SKILL.md 合同错误：{exc}",
            )
        )
        return errors

    try:
        commands = _extract_e2e_workflow_commands(source_skill_dir, skill_md)
    except ValueError as exc:
        return [str(exc)]

    script_files = (
        sorted((source_skill_dir / "scripts").glob("*.py"))
        if (source_skill_dir / "scripts").is_dir()
        else []
    )

    if script_files and not commands:
        shell_like_blocks = [
            body
            for info, body in _iter_markdown_fenced_blocks(skill_md)
            if _is_shell_fence_info(info) and "scripts/" in body.replace("\\", "/")
        ]

        hint = ""
        if shell_like_blocks:
            hint = (
                "\n检测到 SKILL.md 中存在疑似 scripts/ 命令块，但未能解析为 E2E workflow。"
                "请检查 fenced code block 是否是标准 Markdown 形态，"
                "以及命令是否形如：python scripts/name.py '{\"key\":\"{{key}}\"}'。"
            )

        return [
            _e2e_error(
                target="SKILL.md",
                layer="workflow_missing",
                message=(
                    "Skill 包含 scripts/*.py，但 SKILL.md 中没有可执行 bash/sh/shell 命令块。\n"
                    "必须在 SKILL.md 中按真实工作流顺序写出脚本调用命令。\n"
                    "references/*.md 只能作为参考资料，不会被 E2E 解析为执行步骤。"
                    f"{hint}"
                ),
            )
        ]

    tmp_handle: tempfile.TemporaryDirectory | None = None

    try:
        tmp_handle, trial_skill_dir = _copy_skill_dir_for_e2e(
            skill_name,
            source_skill_dir=source_skill_dir,
        )
        trial_skill_md = (trial_skill_dir / "SKILL.md").read_text(encoding="utf-8")

        payload: dict[str, Any] = _seed_initial_e2e_payload(
            commands,
            external_context=external_context,
        )
        traces: list[E2EStepTrace] = []

        venv_python: Path | None = None

        if any(command.script_path.endswith(".py") for command in commands):
            try:
                venv_python = _get_skill_venv_python(trial_skill_dir)

                for command in commands:
                    if not command.script_path.endswith(".py"):
                        continue

                    entry = _skill_plan_entry_for_file(
                        file_path=command.script_path,
                        blueprint_text=trial_skill_md,
                    )

                    _install_capability_dependencies(
                        venv_python,
                        entry.required_capabilities,
                    )

                    refined_contract, resolution = _contract_resolution_for_trial(
                        command.script_path,
                        trial_skill_md,
                        None,
                        None,
                    )

                    _install_declared_dependency_packages(
                        venv_python,
                        list(refined_contract.declared_dependencies or [])
                        + list(resolution.declared_dependencies or []),
                        source_label="implementation_resolution",
                    )

            except RuntimeError as exc:
                return [
                    _e2e_error(
                        target="scripts",
                        layer="venv_prepare",
                        message=f"端到端试运行环境准备失败：{exc}",
                    )
                ]

        for index, command in enumerate(commands):
            try:
                entry = _validate_e2e_command_static(
                    command=command,
                    trial_skill_dir=trial_skill_dir,
                    skill_md=trial_skill_md,
                    available_payload_keys=set(payload.keys()),
                )

                content = (trial_skill_dir / command.script_path).read_text(encoding="utf-8")

                rendered_payload = _render_e2e_command_payload(
                    command,
                    payload=payload,
                    traces=traces,
                )

                if entry.runtime == "python":
                    if venv_python is None:
                        raise ValueError("python venv 未初始化。")

                    proc = _execute_e2e_python_command(
                        command=command,
                        trial_skill_dir=trial_skill_dir,
                        rendered_payload=rendered_payload,
                        venv_python=venv_python,
                    )

                elif entry.runtime == "node":
                    proc = _execute_e2e_node_command(
                        command=command,
                        trial_skill_dir=trial_skill_dir,
                        rendered_payload=rendered_payload,
                    )

                elif entry.runtime in {"bash", "shell"}:
                    proc = _execute_e2e_shell_command(
                        command=command,
                        trial_skill_dir=trial_skill_dir,
                        rendered_payload=rendered_payload,
                    )

                else:
                    raise ValueError(
                        _e2e_error(
                            target=command.script_path,
                            layer="unsupported_runtime",
                            message=(
                                f"第 {command.ordinal} 步 {command.script_path} "
                                f"runtime={entry.runtime} 暂不支持端到端执行。"
                            ),
                        )
                    )

                stdout_json = _parse_e2e_stdout_json(
                    command=command,
                    proc=proc,
                    trial_skill_dir=trial_skill_dir,
                    trial_skill_md=trial_skill_md,
                    content=content,
                    entry=entry,
                    rendered_payload=rendered_payload,
                )

                artifact_paths = _stdout_artifact_paths(stdout_json, entry, None)

                before_keys = set(payload.keys())
                new_keys = sorted(set(stdout_json.keys()) - before_keys)

                trace = E2EStepTrace(
                    ordinal=command.ordinal,
                    script_path=command.script_path,
                    raw_command=command.raw_command,
                    placeholders=sorted(_e2e_command_placeholders(command)),
                    argv_keys=sorted(str(key) for key in rendered_payload.keys()),
                    stdout_keys=sorted(str(key) for key in stdout_json.keys()),
                    new_keys=new_keys,
                    artifact_paths=artifact_paths,
                    argv_shape=_json_object_shape(rendered_payload),
                    stdout_shape=_json_object_shape(stdout_json),
                )

                argument_effect_review = _run_e2e_step_argument_effect_review(
                    command=command,
                    script_content=content,
                    skill_plan_entry=entry,
                    rendered_payload=rendered_payload,
                    stdout_json=stdout_json,
                    artifact_paths=artifact_paths,
                    trace=trace,
                    previous_traces=traces,
                    requested_model=requested_model,
                )

                if not argument_effect_review.get("passed"):
                    raise ValueError(_e2e_argument_effect_failure(
                        command=command,
                        review=argument_effect_review,
                        rendered_payload=rendered_payload,
                        stdout_json=stdout_json,
                        artifact_paths=artifact_paths,
                        traces=traces,
                    ))

                is_final_step = index == len(commands) - 1
                if is_final_step:
                    _validate_final_platform_output_contract(
                        command=command,
                        stdout_json=stdout_json,
                        traces=traces,
                    )

                payload.update(stdout_json)

                if artifact_paths:
                    payload.setdefault("_artifacts", [])
                    if isinstance(payload["_artifacts"], list):
                        payload["_artifacts"].extend(artifact_paths)
                    payload["_last_artifacts"] = artifact_paths

                traces.append(trace)

                logger.info("[Creator][E2E] %s", _e2e_trace_line(trace))

            except subprocess.TimeoutExpired as exc:
                errors.append(
                    _e2e_error(
                        target=command.script_path,
                        layer="timeout",
                        message=(
                            f"第 {command.ordinal} 步 {command.script_path} 端到端执行超时：{exc}\n\n"
                            "已成功执行的前序边界 trace：\n"
                            f"{_format_e2e_trace(traces)}"
                        ),
                    )
                )
                break

            except ValueError as exc:
                message = str(exc)
                if "已成功执行的前序边界 trace" not in message and "已成功执行的前序步骤" not in message:
                    message += "\n\n已成功执行的前序边界 trace：\n" + _format_e2e_trace(traces)
                errors.append(message)
                break

    finally:
        if tmp_handle is not None:
            tmp_handle.cleanup()

    return errors


def _placeholder_root_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\{\{\s*([A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*)\s*\}\}", value.strip())
    if not match:
        return None
    return match.group(1).split(".", 1)[0]


def _values_for_skill_plan_command(
    *,
    entry: SkillPlanEntry,
    existing_payload: dict[str, Any] | None,
    available_values: set[str],
) -> dict[str, str]:
    values: dict[str, str] = {}
    existing_payload = existing_payload or {}
    reusable_by_root: dict[str, str] = {}
    for value in existing_payload.values():
        root = _placeholder_root_name(value)
        if root:
            reusable_by_root[root] = value

    for input_name in entry.inputs or ["payload"]:
        current = existing_payload.get(input_name)
        if isinstance(current, str) and _placeholder_root_name(current) in available_values:
            values[input_name] = current
        elif input_name in reusable_by_root and input_name in available_values:
            values[input_name] = reusable_by_root[input_name]
        else:
            values[input_name] = f"{{{{{input_name}}}}}"
    return values


def _patch_skill_md_command_payloads_from_skill_plan(content: str, blueprint_text: str) -> tuple[str, list[dict[str, Any]]]:
    """Deprecated no-op.

    Command payload repair must be based on real E2E traces (argv_shape,
    stdout_shape, missing placeholders, and script parser behavior), not by
    rewriting JSON argv to match SkillPlan.inputs. Keep this function as a
    compatibility shim for older callers/tests, but never mutate content.
    """
    return content, []


def _structured_failure_from_errors(errors: list[str]) -> dict[str, Any]:
    for error in errors or []:
        match = re.search(r"E2E_STRUCTURED_FAILURE=(\{.*?\})(?:\n|$)", error, re.S)
        if not match:
            continue
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return {}

async def _repair_existing_file_for_e2e_failure(
    *,
    skill_name: str,
    target_path: str,
    e2e_errors: list[str],
    requested_model: str | None = None,
    external_context: dict[str, Any] | None = None,
) -> str:
    """Repair existing SKILL.md/script file using local patch + sandbox E2E.

    第二轮原则：

    1. 只修跨模块接口串接：
       - SKILL.md workflow command；
       - 上下游 JSON 字段；
       - 当前脚本 argv/stdout 对齐；
       - 最终平台输出是否能被 sandbox 消费。

    2. 不在这里修单模块功能细节：
       - PDF 字体、字号、行距；
       - 图片分辨率、风格；
       - 表格样式；
       - 内容质量。
       这些属于第一轮 module functional smoke。

    3. 不写平台 IO 词表。
       平台 IO 直接复用现有 sandbox / E2E 试运行协议。

    4. 模型只输出局部 patch。
       E2E 是否通过由临时 skill 沙盒真实试运行决定。

    5. 如果 patch apply / static preflight / sandbox E2E 失败，
       在本函数内部继续把失败反馈给写代码模型重试，直到通过或达到最大轮次。
    """

    _validate_file_path(target_path)

    if target_path.startswith("references/"):
        logger.info(
            "[Creator][E2E] remap reference repair target to SKILL.md target=%s",
            target_path,
        )
        target_path = "SKILL.md"

    skill_dir = settings.skills_path / skill_name
    target_file = skill_dir / target_path

    if not target_file.is_file():
        raise ValueError(f"端到端修复目标不存在：{target_path}")

    skill_md_path = skill_dir / "SKILL.md"
    skill_md = skill_md_path.read_text(encoding="utf-8") if skill_md_path.is_file() else ""

    all_file_summaries: list[str] = []
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue

        rel = path.relative_to(skill_dir).as_posix()
        if rel.startswith(".venv/") or "__pycache__" in rel:
            continue
        if rel == target_path:
            continue

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        all_file_summaries.append(f"\n--- FILE {rel} ---\n{text[-6000:]}")

    route = route_creator_file_model(
        file_path=target_path,
        purpose=(
            "第二轮 workflow E2E 局部修复："
            "只修 SKILL.md workflow、跨模块 JSON 字段串接、最终平台输出字段映射；"
            "不修单文件业务功能细节；"
            "平台 IO 由 sandbox/E2E 试运行判断；"
            "输出 single-file local patch proposal。"
        ),
        requested_model=requested_model,
    )
    model = route.model

    _log_creator_model_usage(
        phase="e2e_repair.route",
        skill_name=skill_name,
        file_path=target_path,
        route=route,
        extra=f"errors={len(e2e_errors)} mode=exact_replace_patch_sandbox_e2e",
    )

    deterministic_error = "\n\n".join(e2e_errors)[-12000:]
    structured_failure = _structured_failure_from_errors(e2e_errors)
    targeted_e2e_hint = _targeted_e2e_repair_hint(e2e_errors)

    scope = CreatorRepairScope(
        phase="workflow_e2e",
        repair_type="cross_step_io_alignment",
        target_file=target_path,
        max_changed_lines=220,
        notes=(
            "第二轮只修 workflow / cross-step IO / final sandbox output。",
            "平台 IO 不在 repair 层用词表判断，直接由 sandbox/E2E 试运行判断。",
            "优先输出 edits old/new exact_replace patch，不要输出完整文件。",
        ),
    )

    e2e_tool_cards = ""
    if target_path.startswith("scripts/"):
        e2e_entry = _skill_plan_entry_for_file(
            file_path=target_path,
            blueprint_text=skill_md,
        )
        e2e_tool_cards = _creator_tool_context_for_script(
            file_path=target_path,
            skill_plan_entry=e2e_entry,
            blueprint_text=skill_md,
            failure_layer=_failure_layer_from_error_text(deterministic_error),
            error_text=deterministic_error,
            include_snippets=True,
        )

    if target_path == "SKILL.md":
        target_rule = (
            "你正在修复 SKILL.md 的 workflow 执行块。\n"
            "第二轮 E2E 的目标是让 workflow 在简单沙盒中真实跑通。\n"
            "E2E 只执行 SKILL.md 中的 bash/sh/shell fenced command block，references/*.md 不是执行步骤。\n"
            "只修命令块、参数传递、步骤串接相关问题。\n"
            "不要重写 SKILL.md 正文。\n"
            "不要在 repair 层重新定义平台 IO；平台 IO 由 sandbox/E2E 试运行判断。\n"
            "优先输出 edits old/new exact_replace patch。不要输出完整 SKILL.md。"
        )

    elif target_path.startswith("scripts/"):
        target_rule = (
            "你正在修复脚本源码的 E2E 接口串接问题。\n"
            "第二轮 E2E 的目标是让 workflow 在简单沙盒中真实跑通。\n"
            "只修当前脚本与 SKILL.md 命令块、上游 stdout、下游输入之间的接口对齐问题。\n"
            "不要重新设计业务功能；PDF 样式、图片风格、表格样式、内容质量属于第一轮功能 smoke。\n"
            "不要在 repair 层重新定义平台 IO；平台 IO 由 sandbox/E2E 试运行判断。\n"
            "优先输出 edits old/new exact_replace patch。不要输出完整源码。"
        )

    else:
        target_rule = (
            "只修复 E2E_REPAIR_TARGET 指向的文件。\n"
            "只修当前 E2E 失败对应的最小接口串接问题。\n"
            "优先输出 edits old/new exact_replace patch。不要输出完整文件。"
        )

    base_task_context = "\n".join([
        f"Skill 名称：{skill_name}",
        "",
        "结构化失败对象：",
        json.dumps(structured_failure, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        "",
        "定向 E2E 修复提示：",
        targeted_e2e_hint or "无",
        "",
        "sandbox IO 前置协议：",
        _sandbox_io_contract_text_for_creator(),
        "",
        "当前 SKILL.md：",
        skill_md[-12000:],
        "",
        "其它相关文件摘要：",
        "".join(all_file_summaries)[-20000:],
        "",
        "Tool Registry / Snippet 上下文：",
        e2e_tool_cards,
    ])

    repair_feedback = deterministic_error
    last_failure = ""
    max_candidate_attempts = 5

    for candidate_attempt in range(1, max_candidate_attempts + 1):
        current_content = target_file.read_text(encoding="utf-8")

        try:
            _proposal, candidate_content, diff_stats = await _request_and_apply_repair_patch(
                model=model,
                file_path=target_path,
                current_content=current_content,
                failure_text=repair_feedback,
                scope=scope,
                task_context=base_task_context + ("\n\n上一轮候选失败反馈：\n" + last_failure if last_failure else ""),
                target_rule=target_rule,
                patch_retry_limit=3,
            )

            sanitized = _sanitize_generated_file_content(target_path, candidate_content)

            try:
                if target_path == "SKILL.md":
                    _validate_skill_md_against_existing_files(skill_name, sanitized)

                elif target_path.startswith("references/"):
                    _validate_reference_file_contract(target_path, sanitized, skill_md)

                elif target_path.startswith("assets/"):
                    _validate_asset_file_contract(target_path, sanitized)

                elif target_path.startswith("scripts/"):
                    _validate_e2e_script_static_preflight(
                        file_path=target_path,
                        content=sanitized,
                        skill_md=skill_md,
                    )

            except Exception as preflight_exc:
                last_failure = (
                    "STATIC_PREFLIGHT_FAILED：候选 patch 已应用，但静态预检失败。\n"
                    f"attempt={candidate_attempt}/{max_candidate_attempts}\n"
                    f"error_type={type(preflight_exc).__name__}\n"
                    f"error={preflight_exc}\n"
                    "请基于这个静态错误继续输出新的 exact_replace patch。"
                )
                repair_feedback = deterministic_error + "\n\n" + last_failure
                logger.warning(
                    "[Creator][E2E][repair_candidate_static_failed] skill=%s file=%s attempt=%d/%d error=%s",
                    skill_name,
                    target_path,
                    candidate_attempt,
                    max_candidate_attempts,
                    preflight_exc,
                )
                continue

            with tempfile.TemporaryDirectory(prefix="creator-e2e-patch-candidate-") as tmp:
                tmp_root = Path(tmp)
                patched_skill_dir = tmp_root / skill_name

                shutil.copytree(
                    skill_dir,
                    patched_skill_dir,
                    ignore=shutil.ignore_patterns(
                        ".venv",
                        "__pycache__",
                        "*.pyc",
                        ".pytest_cache",
                    ),
                )

                patched_target = patched_skill_dir / target_path
                patched_target.parent.mkdir(parents=True, exist_ok=True)
                patched_target.write_text(sanitized, encoding="utf-8")

                sandbox_gate = _run_e2e_sandbox_acceptance_gate(
                    skill_name=skill_name,
                    candidate_skill_dir=patched_skill_dir,
                    patched_file=target_path,
                    original_errors=e2e_errors,
                    external_context=external_context,
                    requested_model=requested_model,
                )

                if not sandbox_gate.get("accepted"):
                    last_failure = (
                        "SANDBOX_E2E_FAILED：候选 patch 已应用，但简单沙盒 E2E 仍失败。\n"
                        f"attempt={candidate_attempt}/{max_candidate_attempts}\n"
                        f"diff_stats={json.dumps(diff_stats, ensure_ascii=False, default=str)[:3000]}\n"
                        f"sandbox_gate={json.dumps(sandbox_gate, ensure_ascii=False, default=str)[:12000]}\n"
                        "请基于 sandbox_gate.errors 继续输出新的 exact_replace patch。"
                    )
                    repair_feedback = deterministic_error + "\n\n" + last_failure
                    logger.warning(
                        "[Creator][E2E][repair_candidate_e2e_failed] skill=%s file=%s attempt=%d/%d",
                        skill_name,
                        target_path,
                        candidate_attempt,
                        max_candidate_attempts,
                    )
                    continue

            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_text(sanitized, encoding="utf-8")

            logger.info(
                "[Creator][E2E][repair_accept] skill=%s file=%s attempt=%d diff_stats=%s sandbox=passed",
                skill_name,
                target_path,
                candidate_attempt,
                json.dumps(diff_stats, ensure_ascii=False, default=str)[:3000],
            )

            return target_path

        except Exception as candidate_exc:
            last_failure = (
                "REPAIR_CANDIDATE_FAILED：候选 patch 生成、解析或应用失败。\n"
                f"attempt={candidate_attempt}/{max_candidate_attempts}\n"
                f"error_type={type(candidate_exc).__name__}\n"
                f"error={candidate_exc}\n"
                "请继续输出新的 exact_replace patch。"
            )
            repair_feedback = deterministic_error + "\n\n" + last_failure

            logger.warning(
                "[Creator][E2E][repair_candidate_failed] skill=%s file=%s attempt=%d/%d error=%s",
                skill_name,
                target_path,
                candidate_attempt,
                max_candidate_attempts,
                candidate_exc,
            )

            continue

    raise ValueError(
        "端到端自动修复未完成：写代码模型连续提出的 patch 未能通过 apply/static/E2E。\n"
        f"skill={skill_name}\n"
        f"target={target_path}\n"
        f"last_failure={last_failure[:12000]}"
    )

def _iter_markdown_fenced_blocks(content: str) -> list[tuple[str, str]]:
    """Return fenced code blocks as (info_string, body).

    This parser is intentionally line-based instead of one regex so it accepts
    normal Markdown shapes generated by LLMs, including fences indented under
    list items:

        1. step
           ```bash
           python scripts/foo.py '{"topic":"{{topic}}"}'
           ```

    It also accepts CRLF, trailing spaces after fences, and ~~~ fences.
    """
    lines = (content or "").splitlines()
    blocks: list[tuple[str, str]] = []

    in_block = False
    fence_char = ""
    fence_len = 0
    info = ""
    body_lines: list[str] = []

    open_re = re.compile(r"^\s*(`{3,}|~{3,})([^\n`]*)\s*$")

    for line in lines:
        if not in_block:
            match = open_re.match(line)
            if not match:
                continue

            fence = match.group(1)
            fence_char = fence[0]
            fence_len = len(fence)
            info = (match.group(2) or "").strip().lower()
            body_lines = []
            in_block = True
            continue

        close_re = re.compile(rf"^\s*{re.escape(fence_char)}{{{fence_len},}}\s*$")
        if close_re.match(line):
            blocks.append((info, "\n".join(body_lines).strip()))
            in_block = False
            fence_char = ""
            fence_len = 0
            info = ""
            body_lines = []
            continue

        body_lines.append(line)

    return blocks


def _is_shell_fence_info(info: str) -> bool:
    """Return whether a fenced block should be treated as shell commands.

    Strict Creator/E2E rule:
    - Only explicitly marked shell fences are executable candidates.
    - Plain ``` fenced blocks are documentation blocks, not executable blocks.
    """
    normalized = (info or "").strip().lower()
    if not normalized:
        return False

    first = normalized.split()[0]
    return first in {"bash", "sh", "shell", "zsh"}

def _validate_skill_package_smoke(
    skill_name: str,
    *,
    mode: str = "trial",
    external_context: dict[str, Any] | None = None,
    requested_model: str | None = None,
) -> list[str]:
    """Strict end-to-end workflow validation."""
    return validate_workflow_e2e(
        skill_name,
        external_context=external_context,
        requested_model=requested_model,
    )

def _external_context_from_skill_action_request(request: SkillActionRequest) -> dict[str, Any]:
    """Build external context for Creator E2E.

    validate-skill / package-skill 经常不是从真实用户运行入口触发，
    request.messages 可能为空。如果不补一个非空通用输入，
    SKILL.md 中 {{user_request}} 会被渲染成空字符串，进而让
    argument-effect review 误判脚本没有消费参数。

    这里补的是平台外部输入 envelope，不是业务字段名：
    - user_request
    - input
    - text
    - payload

    不补 theme/topic/story_text 这类业务字段。
    """
    context = build_creator_external_input_context(
        messages=request.messages,
        input_files=request.input_files,
        fields=request.fields,
        options=request.options,
    )

    if not isinstance(context, dict):
        context = {}

    # 优先从真实 request.messages 取最后一条用户文本。
    user_text = ""
    for message in reversed(request.messages or []):
        if not isinstance(message, dict):
            continue
        if message.get("role") != "user":
            continue
        value = str(message.get("content") or "").strip()
        if value:
            user_text = value
            break

    # 如果没有真实用户文本，使用通用 E2E 测试输入。
    # 注意：这是平台外部 envelope 的测试值，不是业务字段名硬编码。
    fallback_text = (
        user_text
        or "Creator E2E 验证输入：请根据这个请求完成当前 Skill 的主要任务，"
           "内容包含中文、English words 和标点，用于验证参数传递、脚本消费和输出闭环。"
    )

    # 如果 build_creator_external_input_context 已经给了非空值，则保留。
    for key in ("user_request", "input", "text", "payload"):
        if not _json_value_non_empty(context.get(key)):
            context[key] = fallback_text

    if not isinstance(context.get("fields"), dict):
        context["fields"] = dict(request.fields or {})

    if not isinstance(context.get("options"), dict):
        context["options"] = dict(request.options or {})

    if not isinstance(context.get("input_files"), list):
        context["input_files"] = list(request.input_files or [])

    if not _json_value_non_empty(context.get("files")):
        context["files"] = list(context.get("input_files") or [])

    return context


@router.post("/validate-skill", response_model=SkillActionResponse)
async def validate_skill(request: SkillActionRequest):
    """Validate and strictly E2E-run a Skill package.

    Flow:
    1. basic SKILL.md validation
    2. strict SKILL.md workflow E2E execution
    3. if failed, route feedback to MD/code model and rewrite the failing file
    4. retry until success or max attempts exhausted
    """
    skill_name = _validate_skill_name(request.skill_name)

    result = run_action({"action": "validate", "name": skill_name})
    if not result["success"]:
        return SkillActionResponse(
            success=False,
            path=result.get("path"),
            message=result["message"],
        )

    max_attempts = max(0, min(int(request.max_e2e_repair_attempts or 0), 10))
    attempt = 0
    repair_logs: list[str] = []

    while True:
        external_context = _external_context_from_skill_action_request(request)
        try:
            e2e_errors = validate_workflow_e2e(
                skill_name,
                external_context=external_context,
                requested_model=request.model,
            )
        except Exception as exc:
            logger.exception("validate-skill e2e validator crashed skill=%s", skill_name)
            e2e_errors = [
                _e2e_error(
                    target="SKILL.md",
                    layer="e2e_internal_exception",
                    message=(
                        "严格端到端工作流校验内部异常，已按校验失败返回而不是 HTTP 500。\n"
                        f"exception_type={type(exc).__name__}\n"
                        f"exception={exc}"
                    ),
                )
            ]
        if not e2e_errors:
            suffix = ""
            if repair_logs:
                suffix = "\n\n端到端自动修复记录：\n" + "\n".join(repair_logs)
            return SkillActionResponse(
                success=True,
                path=result.get("path"),
                message=result["message"] + "\n严格端到端工作流校验通过：SKILL.md 命令已按顺序真实执行，中间 JSON 边界已流转，最终 stdout 已对齐 sandbox 平台输出协议。" + suffix,
            )

        if not request.auto_repair or attempt >= max_attempts:
            return SkillActionResponse(
                success=False,
                path=None,
                message=(
                    "严格端到端工作流校验失败：\n"
                    + "\n\n".join(e2e_errors)
                    + (
                        "\n\n端到端自动修复记录：\n" + "\n".join(repair_logs)
                        if repair_logs else ""
                    )
                ),
            )

        target_path = _e2e_repair_target_from_errors(e2e_errors)
        try:
            repaired_target = await _repair_existing_file_for_e2e_failure(
                skill_name=skill_name,
                target_path=target_path,
                e2e_errors=e2e_errors,
                requested_model=request.model,
                external_context=external_context,
            )
            attempt += 1
            repair_logs.append(
                f"第 {attempt} 轮：根据端到端失败反馈修复 {repaired_target}"
            )
        except Exception as exc:
            logger.exception(
                "validate-skill e2e auto repair failed skill=%s target=%s",
                skill_name,
                target_path,
            )
            return SkillActionResponse(
                success=False,
                path=None,
                message=(
                    "严格端到端工作流校验失败，且自动修复未完成：\n"
                    + "\n\n".join(e2e_errors)
                    + f"\n\n自动修复目标：{target_path}"
                    + f"\n自动修复异常：{exc}"
                    + (
                        "\n\n端到端自动修复记录：\n" + "\n".join(repair_logs)
                        if repair_logs else ""
                    )
                ),
            )


@router.post("/package-skill", response_model=SkillActionResponse)
async def package_skill(request: PackageSkillRequest):
    """Package a Skill directory into a distributable .skill archive.

    Packaging is intentionally gated by strict E2E validation so the frontend
    or any direct API caller cannot download a package that failed the real
    workflow trial run.

    Final local-resource existence check is performed only at package time:
    - During SKILL.md generation, scripts/references may not exist yet.
    - During packaging, all SKILL.md referenced scripts/references/assets
      must already exist on disk or the package is invalid.
    """
    skill_name = _validate_skill_name(request.skill_name)

    if request.validate_before_package:
        external_context = _external_context_from_skill_action_request(request)
        e2e_errors = _validate_skill_package_smoke(
            skill_name,
            mode="trial",
            external_context=external_context,
            requested_model=request.model,
        )
        if e2e_errors:
            return SkillActionResponse(
                success=False,
                path=None,
                message=(
                    "打包已中止：严格端到端工作流校验未通过。\n"
                    "请先调用 /api/creator/validate-skill 完成自动修复，"
                    "或根据以下错误手动修改后重试：\n"
                    + "\n\n".join(e2e_errors)
                ),
            )

    try:
        _validate_skill_md_final_resource_existence(skill_name)
    except Exception as exc:
        return SkillActionResponse(
            success=False,
            path=None,
            message=(
                "打包已中止：最终资源存在性校验失败。\n"
                "原因：SKILL.md 引用了尚未生成、尚未上传或不存在的本地资源。\n"
                "请确认 scripts/**、references/** 已生成，assets/** 已上传。\n\n"
                f"{exc}"
            ),
        )

    result = run_action({"action": "package", "name": skill_name})
    if not result["success"]:
        return SkillActionResponse(
            success=False,
            path=result.get("path"),
            message=result["message"],
        )

    return SkillActionResponse(
        success=True,
        path=result.get("path"),
        message=result["message"],
    )

@router.post("/init-from-blueprint", response_model=InitFromBlueprintResponse)
async def init_from_blueprint(request: InitFromBlueprintRequest):
    """Initialize Skill directory structure from blueprint file list.

    只创建 Skill 根目录和必要子目录，不再 touch 空文件。

    原因：
    - 文件内容必须由 /generate-file 成功生成后，再由 /write-file 写入；
    - 如果这里预先 touch 文件，前端会看到 0 B 文件，并可能误显示为“已写入”；
    - 这会掩盖模型生成失败或空内容问题。
    """
    skill_name = _validate_skill_name(request.skill_name)
    skill_root = settings.skill_public_dir / skill_name

    try:
        skill_root.mkdir(parents=True, exist_ok=True)

        dirs_created = 0
        seen_dirs: set[Path] = set()

        for file_spec in request.files:
            rel_path = _normalize_skill_path(file_spec.path)
            if not rel_path:
                continue

            target_path = skill_root / rel_path

            if _is_directory_like_skill_path(rel_path):
                dir_path = target_path
            else:
                dir_path = target_path.parent

            if dir_path in seen_dirs:
                continue

            existed = dir_path.exists()
            dir_path.mkdir(parents=True, exist_ok=True)
            seen_dirs.add(dir_path)

            if not existed:
                dirs_created += 1

        return InitFromBlueprintResponse(
            success=True,
            path=str(skill_root),
            files_created=0,
            message=(
                f"已初始化 Skill 目录结构，创建目录 {dirs_created} 个。"
                "文件将在 generate-file 成功返回非空内容后写入，不再预创建 0 B 空文件。"
            ),
        )

    except Exception as exc:
        logger.exception("init-from-blueprint error")
        return InitFromBlueprintResponse(
            success=False,
            path=None,
            files_created=0,
            message=f"初始化失败：{exc}",
        )

@router.post("/list-files", response_model=ListFilesResponse)
async def list_files(request: ListFilesRequest):
    """List all files in a Skill directory.
    
    Returns the actual file structure on disk, useful for displaying
    to the user after initializing the Skill directory structure.
    """
    skill_name = _validate_skill_name(request.skill_name)
    
    skill_root = settings.skill_public_dir / skill_name
    if not skill_root.exists():
        return ListFilesResponse(
            success=False,
            files=[],
            message=f"Skill '{skill_name}' 不存在",
        )
    
    files: list[FileInfo] = []
    
    def scan_dir(base: Path, rel_path: Path = Path("")):
        for entry in sorted(base.iterdir()):
            entry_rel = rel_path / entry.name
            if entry.is_dir():
                files.append(FileInfo(
                    path=str(entry_rel),
                    is_directory=True,
                ))
                scan_dir(entry, entry_rel)
            else:
                files.append(FileInfo(
                    path=str(entry_rel),
                    is_directory=False,
                    size=entry.stat().st_size,
                ))
    
    scan_dir(skill_root)
    
    return ListFilesResponse(
        success=True,
        files=files,
        message=f"已列出 {len(files)} 个文件",
    )
