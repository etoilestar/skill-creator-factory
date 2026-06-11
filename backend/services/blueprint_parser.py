"""Blueprint parser — pure-Python extraction of file specs from a confirmed Skill blueprint.

No LLM calls are made here.  All regex failures degrade gracefully to
sensible defaults, and any uncertainty is captured in BlueprintPlan.warnings
so the frontend can surface it to the user.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from .skill_plan import SkillPlan, SkillPlanEntry, build_skill_plan_entry, is_runtime_artifact_semantic, dependency_is_output_semantic, normalize_skill_plan, validate_file_plan_semantics, validate_skill_plan_dataflow

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FileSpec:
    """A single file to be generated for a Skill package."""

    path: str          # relative to skill root: "SKILL.md" / "scripts/main.py"
    purpose: str       # human-readable description used as LLM prompt context
    required: bool = True
    can_skip: bool = False


@dataclass
class BlueprintPlan:
    """Parsed creation plan extracted from a confirmed Skill blueprint."""

    skill_name: str
    files: list[FileSpec] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skill_plan: SkillPlan | None = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BLUEPRINT_MARKER = "📋 Skill 架构蓝图"

# "- **Skill 名称**: foo-bar" or "- **Skill 名称**: foo-bar"
_SKILL_NAME_RE = re.compile(
    r"-\s+\*\*Skill\s+名称\*\*[：:]\s*([^\n]+)",
    re.IGNORECASE,
)

# Inline backtick references: `scripts/main.py` or `references/guide.md`
_SKILL_PATH_INLINE_RE = re.compile(
    r"`((?:scripts|references|assets)/[^`\s]+)`"
)

# Tree structure patterns: 
# - ├── scripts/main.py 或 └── scripts/main.py (第一层级)
# - │   ├── scripts/main.py 或 │   └── scripts/main.py (第二层级)
# - /path/to/scripts/main.py (完整路径)
_TREE_FILE_RE = re.compile(
    r"(?:[│ ]{2,})?[├└]──\s*((?:/?[\w./-]+/)?(?:scripts|references|assets)/[^\s#]+)"
)

# "主入口脚本：scripts/xxx.py" or "主入口脚本: `scripts/xxx.py`"
_ENTRY_SCRIPT_RE = re.compile(
    r"主入口脚本[：:]\s*(`?)([^\n`]+)\1",
    re.IGNORECASE,
)

# "完整运行命令：python scripts/xxx.py ..."
_RUN_COMMAND_RE = re.compile(
    r"完整运行命令[：:]\s*([^\n]+)",
    re.IGNORECASE,
)

# Section lines in the blueprint for scripts / references / assets.
# These match lines like "- scripts/：..." or "- scripts/: 是否创建；..."
_SECTION_SCRIPTS_RE = re.compile(
    r"-\s+scripts/[：:]\s*([^\n]+(?:\n(?!\s*-).*)*)",
    re.IGNORECASE,
)
_SECTION_REFERENCES_RE = re.compile(
    r"-\s+references/[：:]\s*([^\n]+(?:\n(?!\s*-).*)*)",
    re.IGNORECASE,
)
_SECTION_ASSETS_RE = re.compile(
    r"-\s+assets/[：:]\s*([^\n]+(?:\n(?!\s*-).*)*)",
    re.IGNORECASE,
)

# Phrases that indicate a section is not needed
_SKIP_PHRASES: tuple[str, ...] = (
    "无需创建",
    "无需",
    "不需要",
    "暂无",
    "none",
    "n/a",
)

# Maximum allowed length (chars) for a normalised Skill name.
_MAX_SKILL_NAME_LENGTH = 64

# Valid extensions per directory
_SCRIPT_EXTENSIONS: frozenset[str] = frozenset(
    {".py", ".js", ".ts", ".sh", ".bash", ".rb", ".mjs", ".cjs"}
)

_PLACEHOLDER_SCRIPT_STEMS: frozenset[str] = frozenset({
    "foo",
    "bar",
    "baz",
    "demo",
    "example",
    "sample",
    "test",
    "tmp",
    "temp",
    "placeholder",
})


def _script_path_has_concrete_contract(path: str, blueprint_text: str, purpose: str = "") -> bool:
    """Return True if a suspicious script path has enough local evidence.

    We only allow placeholder-like names when the blueprint explicitly gives a
    concrete contract near that path, for example role/inputs/outputs/capabilities
    or a real workflow command using it.  This avoids prompt-leaked examples such
    as `scripts/foo.py` entering the generation queue.
    """
    text = blueprint_text or ""
    purpose_text = purpose or ""

    # Strong evidence: the path appears near explicit contract fields.
    for occurrence in re.finditer(re.escape(path), text):
        start = max(0, occurrence.start() - 500)
        end = min(len(text), occurrence.end() + 900)
        nearby = text[start:end]
        if re.search(
            r"\b(role|inputs|outputs|dependencies|required_capabilities|optional_capabilities|allowed_capabilities|forbidden_capabilities)\b\s*[：:=]",
            nearby,
            re.IGNORECASE,
        ):
            return True
        if re.search(
            r"(职责|输入|输出|依赖|能力|调用|生成|构建|读取|写入)\s*[：:=]",
            nearby,
            re.IGNORECASE,
        ):
            return True

    # Purpose from parsed section is concrete enough.
    if re.search(
        r"(生成|构建|读取|写入|合并|导出|调用|模型|图片|图像|PDF|文档|故事|文本|报告|role|inputs|outputs|capabilities)",
        purpose_text,
        re.IGNORECASE,
    ):
        return True

    # A real command line with JSON argv is concrete evidence.
    if re.search(
        rf"(?:python|python3|node|bash|sh)\s+{re.escape(path)}\s+['\"]?\{{",
        text,
        re.IGNORECASE,
    ):
        return True

    return False


def _is_probable_prompt_leaked_script(path: str, *, purpose: str = "", blueprint_text: str = "") -> bool:
    """Detect placeholder scripts likely leaked from prompt examples.

    This is intentionally conservative:
    - Only applies to scripts/*
    - Only applies to generic placeholder-like file names
    - Does not block the file if the blueprint gives a concrete local contract
    """
    normalized = path.strip().replace("\\", "/")
    if not normalized.startswith("scripts/"):
        return False

    file_name = Path(normalized).name
    stem = Path(file_name).stem.lower()
    suffix = Path(file_name).suffix.lower()

    if suffix not in _SCRIPT_EXTENSIONS:
        return False

    if stem not in _PLACEHOLDER_SCRIPT_STEMS:
        return False

    return not _script_path_has_concrete_contract(
        normalized,
        blueprint_text=blueprint_text,
        purpose=purpose,
    )

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _should_skip(text: str) -> bool:
    """Return True when the section description says no files are needed."""
    stripped = text.strip().lower()
    # Exact "无" or starts with any skip phrase
    if stripped == "无":
        return True
    return any(stripped.startswith(phrase.lower()) for phrase in _SKIP_PHRASES)


def _contains_path_wildcard(path: str) -> bool:
    return any(ch in path for ch in "*?[]{}")


def _extract_inline_paths(text: str, prefix: str) -> list[str]:
    """Return all backtick-wrapped paths matching `prefix/...` found in text."""
    found: list[str] = []
    for m in _SKILL_PATH_INLINE_RE.finditer(text):
        p = m.group(1).strip()
        if p.startswith(prefix + "/") and p not in found:
            found.append(p)
    return found


# ---------------------------------------------------------------------------
# Public parsing functions
# ---------------------------------------------------------------------------


def extract_blueprint_text(messages: list[dict]) -> str | None:
    """Return the last assistant message containing the blueprint marker, or None."""
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            content = msg.get("content") or ""
            if _BLUEPRINT_MARKER in content:
                return content
    return None


def parse_skill_name(blueprint_text: str) -> str | None:
    """Extract and normalise the skill name from the blueprint.

    Returns a kebab-case identifier (lowercase letters, digits, hyphens),
    or None if the name cannot be reliably determined.
    """
    m = _SKILL_NAME_RE.search(blueprint_text)
    if not m:
        return None

    raw = m.group(1).strip()
    # Take only the first token (guard against trailing comments/parens)
    raw = raw.split()[0] if raw else ""

    # Normalise: lowercase, replace spaces/underscores with hyphens, keep alnum+hyphen
    normalised = re.sub(
        r"[^a-z0-9-]",
        "",
        raw.lower().replace(" ", "-").replace("_", "-"),
    )
    normalised = re.sub(r"-{2,}", "-", normalised).strip("-")

    if not normalised or len(normalised) > _MAX_SKILL_NAME_LENGTH:
        return None

    return normalised


def parse_files_from_blueprint(blueprint_text: str) -> tuple[list[FileSpec], list[str]]:
    """Extract the list of files to generate from the blueprint body.

    Returns (files, warnings).  All paths are relative to the skill root.

    Important guard:
    placeholder-like scripts such as scripts/foo.py are ignored unless the
    confirmed blueprint gives them a concrete local contract.  This prevents
    prompt-leaked example paths from entering the Creator generation queue.
    """
    files: list[FileSpec] = []
    warnings: list[str] = []
    seen: set[str] = set()

    def _add(
        path: str,
        purpose: str,
        *,
        required: bool = True,
        can_skip: bool = False,
    ) -> None:
        path = path.strip().replace("\\", "/").strip("`'\"，,。.;；")
        if not path:
            return

        if _contains_path_wildcard(path):
            warning = f"忽略通配符文件路径 {path}；Creator 只能逐个生成具体文件，请在蓝图中展开为具体文件名。"
            if warning not in warnings:
                warnings.append(warning)
            return

        if _is_probable_prompt_leaked_script(path, purpose=purpose, blueprint_text=blueprint_text):
            warning = (
                f"已忽略疑似提示词示例/泄露脚本 {path}；"
                "该文件名像占位示例，且蓝图附近没有明确 role/inputs/outputs/capabilities。"
                "如果确实需要该脚本，请在蓝图中明确它的职责、输入、输出和能力后重新添加。"
            )
            if warning not in warnings:
                warnings.append(warning)
            return

        if is_runtime_artifact_semantic(path, purpose):
            warning = f"已忽略运行时产物文件计划项 {path}；脚本生成的结果只能声明在 outputs/stdout metadata 中，不能作为 Creator 待创建文件。"
            if warning not in warnings:
                warnings.append(warning)
            return

        if path in seen:
            return

        seen.add(path)
        files.append(
            FileSpec(path=path, purpose=purpose, required=required, can_skip=can_skip)
        )

    # 1. SKILL.md is always required
    _add("SKILL.md", "Skill 核心说明文件，包含 YAML frontmatter 和执行规范")

    # ------------------------------------------------------------------
    # 2. scripts/ section
    # ------------------------------------------------------------------
    scripts_desc = ""
    m_scripts = _SECTION_SCRIPTS_RE.search(blueprint_text)
    if m_scripts:
        scripts_desc = m_scripts.group(1).strip()

    # 3. Entry-point line, higher priority than section scan
    m_entry = _ENTRY_SCRIPT_RE.search(blueprint_text)
    if m_entry:
        raw_entry = m_entry.group(2).strip().split()[0]
        raw_entry = raw_entry.strip("`'\"，,。.;；")
        if raw_entry and not _should_skip(raw_entry):
            if not raw_entry.startswith("scripts/"):
                raw_entry = "scripts/" + Path(raw_entry).name
            _add(raw_entry, scripts_desc or "Skill 主执行脚本")

    # 4. Inline backtick paths anywhere in the blueprint
    for path in _extract_inline_paths(blueprint_text, "scripts"):
        _add(path, scripts_desc or "Skill 执行脚本")

    # 4b. Tree structure paths
    for m_tree in _TREE_FILE_RE.finditer(blueprint_text):
        tree_path = m_tree.group(1).strip().strip("`'\"，,。.;；")
        for prefix in ("scripts/", "references/", "assets/"):
            idx = tree_path.find(prefix)
            if idx >= 0:
                tree_path = tree_path[idx:]
                break
        if tree_path.startswith("scripts/"):
            _add(tree_path, scripts_desc or "Skill 执行脚本（从目录结构提取）")

    # 5. Bare paths inside scripts section description
    if scripts_desc and not _should_skip(scripts_desc):
        for m_bare in re.finditer(r"scripts/(\S+\.\w+)", scripts_desc):
            _add("scripts/" + m_bare.group(1), scripts_desc)

    # 6. Infer default when section says files are needed but none identified
    if scripts_desc and not _should_skip(scripts_desc):
        has_script = any(f.path.startswith("scripts/") for f in files)
        if not has_script:
            default = "scripts/main.py"
            m_cmd = _RUN_COMMAND_RE.search(blueprint_text)
            if m_cmd:
                for token in m_cmd.group(1).split():
                    token = token.strip("`'\"，,。.;；").lstrip("./")
                    if token.startswith("scripts/") and Path(token).suffix in _SCRIPT_EXTENSIONS:
                        if not _is_probable_prompt_leaked_script(
                            token,
                            purpose=scripts_desc,
                            blueprint_text=blueprint_text,
                        ):
                            default = token
                            break
            _add(default, scripts_desc or "Skill 主执行脚本")
            warnings.append(
                f"脚本文件名未在蓝图中明确指定，已默认为 {default}，请在面板中确认或修改。"
            )

    # 7. Run-command line as additional source for script paths
    m_cmd = _RUN_COMMAND_RE.search(blueprint_text)
    if m_cmd:
        cmd_desc = scripts_desc or "Skill 主执行脚本（从运行命令推断）"
        for token in m_cmd.group(1).split():
            token = token.strip("`'\"，,。.;；").lstrip("./")
            if token.startswith("scripts/") and Path(token).suffix in _SCRIPT_EXTENSIONS:
                _add(token, cmd_desc)

    # ------------------------------------------------------------------
    # 8. references/ section
    # ------------------------------------------------------------------
    m_refs = _SECTION_REFERENCES_RE.search(blueprint_text)
    refs_desc = m_refs.group(1).strip() if m_refs else ""
    if refs_desc and not _should_skip(refs_desc):
        ref_files = _extract_inline_paths(blueprint_text, "references")
        for path in ref_files:
            _add(path, refs_desc, required=False, can_skip=True)

        for m_tree in _TREE_FILE_RE.finditer(blueprint_text):
            tree_path = m_tree.group(1).strip().strip("`'\"，,。.;；")
            for prefix in ("scripts/", "references/", "assets/"):
                idx = tree_path.find(prefix)
                if idx >= 0:
                    tree_path = tree_path[idx:]
                    break
            if tree_path.startswith("references/"):
                _add(tree_path, refs_desc + "（从目录结构提取）", required=False, can_skip=True)

        for m_bare in re.finditer(r"references/(\S+\.\w+)", refs_desc):
            _add("references/" + m_bare.group(1), refs_desc, required=False, can_skip=True)

        if not any(f.path.startswith("references/") for f in files):
            default_ref = "references/guide.md"
            _add(default_ref, refs_desc, required=False, can_skip=True)
            warnings.append(
                f"参考资料文件名未在蓝图中明确指定，已默认为 {default_ref}，请在面板中确认或修改。"
            )

    # ------------------------------------------------------------------
    # 9. assets/ section
    # ------------------------------------------------------------------
    m_assets = _SECTION_ASSETS_RE.search(blueprint_text)
    assets_desc = m_assets.group(1).strip() if m_assets else ""
    if assets_desc and not _should_skip(assets_desc):
        asset_files = _extract_inline_paths(blueprint_text, "assets")
        for path in asset_files:
            _add(path, assets_desc, required=False, can_skip=True)

        for m_tree in _TREE_FILE_RE.finditer(blueprint_text):
            tree_path = m_tree.group(1).strip().strip("`'\"，,。.;；")
            for prefix in ("scripts/", "references/", "assets/"):
                idx = tree_path.find(prefix)
                if idx >= 0:
                    tree_path = tree_path[idx:]
                    break
            if tree_path.startswith("assets/"):
                _add(tree_path, assets_desc + "（从目录结构提取）", required=False, can_skip=True)

        for m_bare in re.finditer(r"assets/(\S+\.\w+)", assets_desc):
            _add("assets/" + m_bare.group(1), assets_desc, required=False, can_skip=True)

        if not any(f.path.startswith("assets/") for f in files):
            default_asset = "assets/template.md"
            _add(default_asset, assets_desc, required=False, can_skip=True)
            warnings.append(
                f"模板/资源文件名未在蓝图中明确指定，已默认为 {default_asset}，请在面板中确认或修改。"
            )

    return files, warnings


def build_skill_plan_from_files(
    *,
    skill_name: str,
    files: list[FileSpec],
    warnings: list[str] | None = None,
    blueprint_text: str = "",
) -> SkillPlan:
    """Build the role/contract plan used by Creator generation and validation.

    This is a second guard against prompt-leaked placeholder scripts.  If a
    placeholder-like script still has no concrete contract after role resolution,
    remove it from the plan instead of merely marking it low confidence.
    """
    reference_files = [file.path for file in files if file.path.startswith("references/")]
    entries: list[SkillPlanEntry] = []
    plan_warnings = list(warnings or [])

    for dep_match in re.finditer(r"dependencies\s*[：:=]\s*\[?([^\]\n;]+)\]?", blueprint_text or "", re.I):
        for raw_dep in re.split(r"[,，、]\s*", dep_match.group(1)):
            dep = raw_dep.strip().strip("'\"")
            if dependency_is_output_semantic(dep):
                warning = f"已从 dependencies 移除输出/动态路径 {dep}；dependencies 只能表示输入依赖。"
                if warning not in plan_warnings:
                    plan_warnings.append(warning)

    for file in files:
        if _is_probable_prompt_leaked_script(
            file.path,
            purpose=file.purpose,
            blueprint_text=blueprint_text,
        ):
            plan_warnings.append(
                f"已从 SkillPlan 中移除疑似提示词示例/泄露脚本 {file.path}；"
                "如确实需要，请明确 role/inputs/outputs/capabilities 后手动添加。"
            )
            continue

        refs_for_file = reference_files if file.path == "SKILL.md" or file.path.startswith("scripts/") else []
        entry = build_skill_plan_entry(
            file_path=file.path,
            purpose=file.purpose,
            required=file.required,
            can_skip=file.can_skip,
            blueprint_summary=blueprint_text[:4000],
            reference_files=refs_for_file,
        )

        # If role classification still says low-confidence generic_script and the
        # filename is placeholder-like, remove it instead of entering repair loops.
        if (
            file.path.startswith("scripts/")
            and entry.role == "generic_script"
            and entry.confidence < 0.7
            and Path(file.path).stem.lower() in _PLACEHOLDER_SCRIPT_STEMS
        ):
            plan_warnings.append(
                f"已从 SkillPlan 中移除 {file.path}：该脚本为低置信 generic_script，"
                "且文件名像示例占位符。请先确认职责、输入、输出和能力后再添加。"
            )
            continue

        entries.append(entry)

        if file.path.startswith("scripts/") and entry.confidence < 0.7:
            plan_warnings.append(
                f"{file.path} 未声明明确 role，已使用保守 generic_script；"
                "不会自动启用图片生成/PDF 生成等高影响能力。"
            )

    normalized = normalize_skill_plan(SkillPlan(skill_name=skill_name, files=entries, warnings=plan_warnings))
    semantic_issues = validate_file_plan_semantics(normalized)
    dataflow_issues = validate_skill_plan_dataflow(normalized)
    return SkillPlan(skill_name=normalized.skill_name, files=normalized.files, warnings=[*normalized.warnings, *semantic_issues, *dataflow_issues])


def parse_blueprint(messages: list[dict]) -> BlueprintPlan:
    """Parse a Skill blueprint from the conversation message history.

    Returns a BlueprintPlan with a best-effort file list and any warnings.
    If no blueprint is found, returns a minimal plan containing only SKILL.md.
    """
    blueprint_text = extract_blueprint_text(messages)
    if not blueprint_text:
        files = [FileSpec(path="SKILL.md", purpose="Skill 核心说明文件", required=True)]
        warnings = ["未在对话历史中找到蓝图，将创建最小 Skill 包（仅 SKILL.md）。"]
        return BlueprintPlan(
            skill_name="new-skill",
            files=files,
            warnings=warnings,
            skill_plan=build_skill_plan_from_files(
                skill_name="new-skill", files=files, warnings=warnings, blueprint_text=""
            ),
        )

    skill_name = parse_skill_name(blueprint_text)
    warnings: list[str] = []
    if skill_name is None:
        skill_name = "new-skill"
        warnings.append(
            "未能从蓝图中解析出合法 Skill 名称，已默认为 'new-skill'，请在面板中修改。"
        )

    files, file_warnings = parse_files_from_blueprint(blueprint_text)
    warnings.extend(file_warnings)

    skill_plan = build_skill_plan_from_files(
        skill_name=skill_name, files=files, warnings=warnings, blueprint_text=blueprint_text
    )
    return BlueprintPlan(
        skill_name=skill_name,
        files=files,
        warnings=skill_plan.warnings,
        skill_plan=skill_plan,
    )
