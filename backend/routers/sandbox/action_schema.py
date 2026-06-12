"""Action Schema 提取与验证。"""

import csv
import io
import json
import logging
import re
import shlex
from pathlib import Path

from ...config import settings
from ...services.skill_dataflow import (
    parse_schema_default_values,
    parse_schema_input_item,
    placeholder_pattern,
)
from ..chat_utils import (
    _extract_all_fenced_blocks,
    _is_within_sandbox,
)
from ..chat_models import MarkdownBlock
from .path_resolution import (
    _normalize_skill_resource_path,
    _available_scripts_for_root,
)
from .resource_catalog import _resource_catalog_by_handle
from .stdout_render import _validate_structured_stdout_payload

logger = logging.getLogger(__name__)


_COMMAND_BLOCK_LANGS = {"bash", "sh", "shell", "zsh", "console", "terminal"}
_COMMAND_BLOCK_CODE_RE = re.compile(
    r"(?im)(^|\n)\s*(?:python(?:3)?\s+)?scripts/[^\s`]+|"
    r"(^|\n)\s*(?:python|python3|node|npm|npx|bash|sh)\s+[^\n]*scripts/"
)
_HOST_COMMAND_INSTRUCTION_RE = re.compile(
    r"(?i)fenced\s+code\s+block|```|run_command|run command|execute command|"
    r"执行命令|运行命令|执行脚本|运行脚本|调用脚本|scripts/|输出[^\n]{0,30}(?:命令|可执行)"
)

_SKILL_LOCAL_RESOURCE_RE = re.compile(r"(?<![\w./-])(?P<path>(?:scripts|references|assets)/[A-Za-z0-9_./-]+)")
_ACTION_SCHEMA_FIELD_RE = re.compile(r"(?<![A-Za-z0-9_])(?:{field})\s*[：:=]\s*\[?([^\]\n;]+)\]?", re.I)
_ACTION_SCHEMA_ROLE_RE = re.compile(r"(?:role|角色|职责)\s*[：:=]\s*(text_generator|image_generator|composite_generator|pdf_builder|docx_builder|pptx_builder|html_asset_builder|asset_builder|generic_script)", re.I)
_RUNTIME_PLACEHOLDER_RE = placeholder_pattern()
_SCRIPT_ROLES = {"text_generator", "image_generator", "composite_generator", "pdf_builder", "docx_builder", "pptx_builder", "html_asset_builder", "asset_builder", "generic_script"}
_HIGH_IMPACT_CAPABILITIES = {"image_generation", "pdf_generation", "docx_generation", "pptx_generation", "html_generation", "html_asset_generation"}


def _extract_script_path_from_command(command: str) -> str | None:
    """Return the skill-local scripts/... path invoked by a command."""
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


def _command_json_argv_keys(command: str, script_path: str | None = None) -> set[str] | None:
    """Return JSON argv keys after the script path, accepting template placeholders."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    for idx, part in enumerate(parts):
        normalized = part.replace("\\", "/").lstrip("./")
        matches = bool(script_path and normalized.endswith(script_path)) or normalized.startswith("scripts/")
        if not matches:
            continue
        if idx + 1 >= len(parts):
            return set()
        candidate = parts[idx + 1]
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return {str(key) for key in payload.keys()}
    return None


def _parse_schema_list_field(text: str, field: str) -> list[str]:
    pattern = _ACTION_SCHEMA_FIELD_RE.pattern.format(field=re.escape(field))
    matches = list(re.finditer(pattern, text or "", re.I))
    if not matches:
        return []
    # Use the nearest declaration before the command block.  A multi-step
    # SKILL.md may contain several Action schema snippets in one file.
    match = matches[-1]
    values = [item.strip().strip("'\"") for item in re.split(r"[,，、]\s*", match.group(1)) if item.strip()]
    cleaned: list[str] = []
    for item in values:
        key, _default = parse_schema_input_item(item)
        if key:
            cleaned.append(key)
    return cleaned



def _parse_optional_schema_inputs(text: str) -> list[str]:
    """Extract optional JSON argv keys declared near an Action schema block."""
    optional = set(_parse_schema_list_field(text, "optional_inputs"))
    optional.update(_parse_schema_list_field(text, "optional inputs"))
    inputs_match = re.search(_ACTION_SCHEMA_FIELD_RE.pattern.format(field=re.escape("inputs")), text or "", re.I)
    if inputs_match:
        for raw_item in re.split(r"[,，、]\s*", inputs_match.group(1)):
            if re.search(r"(?:\?|optional|可选|选填)", raw_item, re.I):
                key = re.split(r"\s*(?:：|:|=|（|\(|\s)\s*", raw_item.strip().strip("'\""), maxsplit=1)[0].rstrip("?")
                key = re.sub(r"[^A-Za-z0-9_./-]", "", key)
                if key:
                    optional.add(key)
    return sorted(optional)


def _block_context(text: str, block: MarkdownBlock) -> str:
    before = (block.before_context or "")[-800:]
    after = (block.after_context or "")[:800]
    return before + "\n" + after


def _extract_action_schemas_from_text(text: str, *, source_path: str) -> list[dict]:
    """Extract portable Action schema entries from Markdown shell blocks.

    This keeps the fenced-block compatibility path but normalizes it into a
    schema the runtime can validate before execution.
    """
    schemas: list[dict] = []
    for block in _extract_all_fenced_blocks(text or ""):
        lang = (block.lang or "").lower()
        command = (block.code or "").strip()
        if lang not in _COMMAND_BLOCK_LANGS or not command or not _COMMAND_BLOCK_CODE_RE.search(command):
            continue
        script_path = _extract_script_path_from_command(command)
        if not script_path:
            continue
        context = (block.before_context or "")[-800:]
        if not (
            _ACTION_SCHEMA_ROLE_RE.search(context)
            or _parse_schema_list_field(context, "inputs")
            or _parse_schema_list_field(context, "outputs")
        ):
            context = _block_context(text, block)
        role_matches = list(_ACTION_SCHEMA_ROLE_RE.finditer(context))
        role = role_matches[-1].group(1).lower() if role_matches else "generic_script"
        inputs = _parse_schema_list_field(context, "inputs")
        outputs = _parse_schema_list_field(context, "outputs")
        optional_inputs = _parse_optional_schema_inputs(context)
        default_values = parse_schema_default_values(context, field_pattern_factory=lambda field: _ACTION_SCHEMA_FIELD_RE.pattern.format(field=re.escape(field)))
        required_capabilities = _parse_schema_list_field(context, "required_capabilities")
        forbidden_capabilities = _parse_schema_list_field(context, "forbidden_capabilities")
        command_keys = _command_json_argv_keys(command, script_path)
        placeholder_keys = set(_RUNTIME_PLACEHOLDER_RE.findall(command))
        local_description = re.sub(r"\s+", " ", context.strip())[:1200]
        schemas.append({
            "script_path": script_path,
            "command": command,
            "source_path": source_path,
            "local_description": local_description,
            "role": role,
            "inputs": inputs,
            "optional_inputs": optional_inputs,
            "default_values": default_values,
            "outputs": outputs,
            "required_capabilities": required_capabilities,
            "forbidden_capabilities": forbidden_capabilities,
            "command_keys": sorted(command_keys) if command_keys is not None else None,
            "placeholder_keys": sorted(placeholder_keys),
        })
    return schemas


def _reference_contract_texts(execution_root: Path | None) -> dict[str, str]:
    if execution_root is None:
        return {}
    root = execution_root.resolve()
    references_dir = root / "references"
    if not references_dir.is_dir() or not _is_within_sandbox(references_dir, root):
        return {}
    texts: dict[str, str] = {}
    for path in sorted(references_dir.rglob("*.md")):
        if not path.is_file() or not _is_within_sandbox(path, root):
            continue
        rel = path.relative_to(root).as_posix()
        texts[rel] = path.read_text(encoding="utf-8", errors="replace")[: settings.skill_resource_max_chars]
    return texts


def _validate_action_schema_entries(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (errors, warnings) for runtime action schemas."""
    errors: list[dict] = []
    warnings: list[dict] = []
    by_script: dict[str, list[dict]] = {}
    for entry in entries:
        by_script.setdefault(str(entry.get("script_path") or ""), []).append(entry)
        role = str(entry.get("role") or "generic_script")
        if role not in _SCRIPT_ROLES:
            # 宽松校验：未知 role 降级为 generic_script 并警告，不阻断执行
            warnings.append({
                "warning": f"未知 script role '{role}'，已降级为 generic_script",
                "script_path": entry.get("script_path"),
                "source_path": entry.get("source_path"),
                "original_role": role,
            })
        command_keys = set(entry.get("command_keys") or [])
        inputs = set(entry.get("inputs") or [])
        optional_inputs = set(entry.get("optional_inputs") or [])
        required_inputs = inputs - optional_inputs
        # 宽松校验：inputs 与 command_keys 不一致降级为警告，不阻断执行
        if inputs and not (required_inputs <= command_keys <= inputs):
            warnings.append({
                "warning": "命令块 JSON keys 与 Action schema inputs 不一致（已降级为警告）",
                "script_path": entry.get("script_path"),
                "source_path": entry.get("source_path"),
                "inputs": sorted(inputs),
                "optional_inputs": sorted(optional_inputs),
                "required_inputs": sorted(required_inputs),
                "command_keys": sorted(command_keys),
            })
        if role == "generic_script" and set(entry.get("required_capabilities") or []) & _HIGH_IMPACT_CAPABILITIES:
            errors.append({
                "error": "generic_script 不允许声明高风险能力，必须显式声明 image_generator、pdf_builder、docx_builder、pptx_builder、html_asset_builder 或 composite_generator role",
                "script_path": entry.get("script_path"),
                "required_capabilities": entry.get("required_capabilities"),
            })
        if role == "generic_script":
            warnings.append({
                "warning": "低置信度/未显式 role 的 generic_script runtime fallback；不会自动启用图片/PDF/Word/PPT/HTML 等高风险能力",
                "script_path": entry.get("script_path"),
                "source_path": entry.get("source_path"),
            })

    for script_path, script_entries in by_script.items():
        distinct_commands = {str(item.get("command") or "").strip() for item in script_entries}
        if len(distinct_commands) > 1:
            errors.append({
                "error": "同一 script 存在多个不一致执行入口",
                "script_path": script_path,
                "sources": [item.get("source_path") for item in script_entries],
            })
        elif len(script_entries) > 1:
            warnings.append({
                "warning": "同一 script 的执行入口在多个文档中重复声明；runtime 将按唯一命令执行",
                "script_path": script_path,
                "sources": [item.get("source_path") for item in script_entries],
            })
    return errors, warnings


def _build_runtime_action_schema(body_prompt: str, *, execution_root: Path | None = None) -> dict:
    """Build a unified Action schema from SKILL.md and reference command blocks."""
    from .multimodal import _strip_runtime_resource_manifest
    skill_text = _strip_runtime_resource_manifest(body_prompt)
    reference_texts = _reference_contract_texts(execution_root)
    texts: list[tuple[str, str]] = [("SKILL.md", skill_text), *reference_texts.items()]
    entries: list[dict] = []
    for source_path, text in texts:
        entries.extend(_extract_action_schemas_from_text(text, source_path=source_path))
    errors, warnings = _validate_action_schema_entries(entries)
    errors.extend(_validate_referenced_assets_in_texts(texts, execution_root=execution_root))
    canonical: dict[str, dict] = {}
    for entry in entries:
        script_path = str(entry.get("script_path") or "")
        if script_path and script_path not in canonical:
            canonical[script_path] = entry
    return {
        "version": "skill-action-schema/v1",
        "entries": list(canonical.values()),
        "errors": errors,
        "warnings": warnings,
    }


def _find_runtime_action_entry(action_schema: dict, command: str) -> dict | None:
    script_path = _extract_script_path_from_command(command)
    if not script_path:
        return None
    for entry in action_schema.get("entries") or []:
        if entry.get("script_path") == script_path:
            return entry
    return None


def _validate_runtime_command_against_action_schema(command: str, *, execution_root: Path | None) -> dict | None:
    """Ensure a runtime command block is declared by SKILL.md/reference schema."""
    script_path = _extract_script_path_from_command(command)
    if not script_path:
        return None
    if execution_root is not None:
        root = execution_root.resolve()
        available_scripts = set(_available_scripts_for_root(root))
        script_file = (root / script_path).resolve()
        if script_path not in available_scripts or not _is_within_sandbox(script_file, root) or not script_file.is_file():
            raise ValueError(
                f"命令调用 {script_path}，但该脚本不在当前 Skill available_scripts 中："
                f"available={sorted(available_scripts)}"
            )
    skill_md = ""
    if execution_root is not None and (execution_root / "SKILL.md").is_file():
        skill_md = (execution_root / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    action_schema = _build_runtime_action_schema(skill_md, execution_root=execution_root)
    if action_schema.get("errors"):
        raise ValueError("Skill Action schema 校验失败: " + json.dumps(action_schema["errors"], ensure_ascii=False))
    entry = _find_runtime_action_entry(action_schema, command)
    if entry is None:
        raise ValueError(f"命令调用 {script_path}，但 SKILL.md/references 中没有唯一声明的执行入口")
    expected_keys = set(entry.get("inputs") or entry.get("command_keys") or [])
    optional_keys = set(entry.get("optional_inputs") or [])
    required_keys = expected_keys - optional_keys
    actual_keys = _command_json_argv_keys(command, script_path)
    if actual_keys is None:
        raise ValueError(f"命令 {script_path} 必须使用可解析 JSON argv")
    # 宽松校验：只检查必填参数是否都提供，额外参数允许（脚本自行处理或忽略）
    missing_required = required_keys - actual_keys
    if missing_required:
        raise ValueError(
            f"命令 {script_path} 缺少必填参数: {sorted(missing_required)}. "
            f"expected={sorted(expected_keys)} optional={sorted(optional_keys)} actual={sorted(actual_keys)}"
        )
    if required_keys and actual_keys > expected_keys:
        logger.info(
            "命令 %s 包含额外参数（允许）: extra=%s expected=%s actual=%s",
            script_path, sorted(actual_keys - expected_keys), sorted(expected_keys), sorted(actual_keys),
        )
    return entry



def _payload_has_file_field(payload: dict, *keys: str) -> bool:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, list) and any(isinstance(item, str) and item.strip() for item in value):
            return True
    return False

def _json_value_non_empty(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return any(_json_value_non_empty(item) for item in value)
    if isinstance(value, dict):
        return any(_json_value_non_empty(item) for item in value.values())
    return True


def _validate_stdout_against_action_entry(stdout: str, entry: dict | None) -> None:
    stripped = (stdout or "").strip()
    if not stripped:
        return
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        # 只有声明了 outputs 的 entry 才强制要求 JSON 格式（数据流需要结构化输出）
        if entry and entry.get("outputs"):
            raise ValueError("角色脚本 stdout 必须是 JSON object（已声明 outputs）")
        if entry:
            logger.info("脚本 stdout 非 JSON 格式（允许，未声明 outputs）")
        return
    if not isinstance(payload, dict):
        if entry and entry.get("outputs"):
            raise ValueError("角色脚本 stdout 必须是 JSON object（已声明 outputs）")
        if entry:
            logger.info("脚本 stdout 非 JSON object（允许，未声明 outputs）")
        return
    if "error" in payload:
        raise ValueError("stdout JSON 不得包含 error 字段")
    if entry and entry.get("outputs") and not any(_json_value_non_empty(value) for value in payload.values()):
        logger.warning("stdout JSON 没有非空字段，但允许继续执行")
    _validate_structured_stdout_payload(payload)




def _image_dimensions_from_bytes(data: bytes) -> tuple[int, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a") and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data.startswith(b"\xff\xd8"):
        idx = 2
        while idx + 9 < len(data):
            if data[idx] != 0xFF:
                idx += 1
                continue
            marker = data[idx + 1]
            idx += 2
            if marker in {0xD8, 0xD9}:
                continue
            if idx + 2 > len(data):
                break
            length = int.from_bytes(data[idx:idx + 2], "big")
            if length < 2:
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and idx + 7 <= len(data):
                return int.from_bytes(data[idx + 5:idx + 7], "big"), int.from_bytes(data[idx + 3:idx + 5], "big")
            idx += length
    return None


def _validate_runtime_asset_contract(path: Path, *, root: Path | None = None) -> None:
    """Type-aware runtime asset validation for referenced/read assets."""
    if not path.is_file():
        raise ValueError(f"asset 不存在或不是文件: {path}")
    if root is not None and not _is_within_sandbox(path, root):
        raise ValueError(f"asset 路径越界: {path}")
    ext = path.suffix.lower()
    data = path.read_bytes()
    if not data:
        raise ValueError(f"asset 不能为空: {path}")
    if ext == ".json":
        json.loads(data.decode("utf-8"))
    elif ext == ".csv":
        rows = list(csv.reader(io.StringIO(data.decode("utf-8"))))
        if len(rows) < 2 or not rows[0] or any(not str(cell).strip() for cell in rows[0]):
            raise ValueError(f"CSV asset 必须包含非空表头和数据行: {path}")
    elif ext in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        magic_ok = data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff") or data.startswith(b"GIF87a") or data.startswith(b"GIF89a") or (data.startswith(b"RIFF") and b"WEBP" in data[:16])
        if not magic_ok:
            raise ValueError(f"image asset 文件头不合法: {path}")
        dims = _image_dimensions_from_bytes(data)
        if dims is not None and (dims[0] < 1 or dims[1] < 1):
            raise ValueError(f"image asset 尺寸不合法: {path}")
    elif ext == ".pdf":
        if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-4096:]:
            raise ValueError(f"PDF asset 文件格式不合法: {path}")
    elif ext in {".md", ".txt", ".yaml", ".yml", ".jinja", ".jinja2", ".template", ".tmpl"}:
        text = data.decode("utf-8", errors="replace").strip()
        if not text:
            raise ValueError(f"Markdown/text asset 不能为空: {path}")


def _validate_referenced_assets_in_texts(texts: list[tuple[str, str]], *, execution_root: Path | None) -> list[dict]:
    if execution_root is None:
        return []
    root = execution_root.resolve()
    errors: list[dict] = []
    seen: set[str] = set()
    for source_path, text in texts:
        for match in _SKILL_LOCAL_RESOURCE_RE.finditer(text or ""):
            rel_path = match.group("path")
            if not rel_path.startswith("assets/") or rel_path in seen:
                continue
            seen.add(rel_path)
            try:
                _validate_runtime_asset_contract((root / rel_path).resolve(), root=root)
            except Exception as exc:
                errors.append({"error": str(exc), "source_path": source_path, "asset_path": rel_path})
    return errors


def _extract_skill_command_contract(body_prompt: str, reference_texts: dict[str, str] | None = None, execution_root: Path | None = None) -> dict:
    """Extract concrete host-executable command examples declared in SKILL.md.

    The sandbox must not ask the final model to invent script invocations from an
    inline `scripts/...` mention.  A skill that wants host execution must include
    a concrete shell fenced block that shows the invocation shape.
    """
    from .multimodal import _strip_runtime_resource_manifest
    skill_text = _strip_runtime_resource_manifest(body_prompt)
    texts: list[tuple[str, str]] = [("SKILL.md", skill_text)]
    if reference_texts is not None:
        texts.extend((path, text) for path, text in reference_texts.items())
    elif execution_root is not None:
        texts.extend((path, text) for path, text in _reference_contract_texts(execution_root).items())

    command_blocks: list[dict] = []
    action_entries: list[dict] = []
    for source_path, text in texts:
        blocks = _extract_all_fenced_blocks(text)
        for block in blocks:
            lang = (block.lang or "").lower()
            code = (block.code or "").strip()
            if lang not in _COMMAND_BLOCK_LANGS or not code:
                continue
            if not _COMMAND_BLOCK_CODE_RE.search(code):
                continue
            command_blocks.append({
                "block_index": block.index,
                "lang": lang,
                "code": code[:600],
                "before_context": block.before_context[-300:],
                "source_path": source_path,
            })
        action_entries.extend(_extract_action_schemas_from_text(text, source_path=source_path))

    errors, warnings = _validate_action_schema_entries(action_entries)
    errors.extend(_validate_referenced_assets_in_texts(texts, execution_root=execution_root))

    return {
        "has_executable_command_block": bool(command_blocks),
        "command_blocks": command_blocks[:5],
        "action_schema": {
            "version": "skill-action-schema/v1",
            "entries": action_entries[:20],
            "errors": errors,
            "warnings": warnings,
        },
    }


# Public aliases
extract_skill_command_contract = _extract_skill_command_contract
build_runtime_action_schema = _build_runtime_action_schema
validate_runtime_command_against_action_schema = _validate_runtime_command_against_action_schema
validate_stdout_against_action_entry = _validate_stdout_against_action_entry
