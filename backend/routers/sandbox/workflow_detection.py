"""命令检测与工作流强制。"""

import re

from ..chat_utils import _extract_all_fenced_blocks
from .path_resolution import _normalize_skill_resource_path

logger = __import__("logging").getLogger(__name__)

# Import constants from action_schema to avoid duplication
from .action_schema import _HOST_COMMAND_INSTRUCTION_RE, _COMMAND_BLOCK_LANGS, _COMMAND_BLOCK_CODE_RE


def _final_instruction_requests_host_command(final_instruction: str) -> bool:
    return bool(_HOST_COMMAND_INSTRUCTION_RE.search(final_instruction or ""))


def _extract_executable_command_blocks_from_text(text: str) -> list[str]:
    """Extract host-executable script commands from final_instruction text.

    Prefer shell fenced blocks, but also accept a bare single-line command when
    the planner returned ``final_instruction`` as plain text.  Every returned
    command is still validated later against the current Skill's
    ``available_scripts`` and Action schema before execution.
    """
    commands: list[str] = []
    seen: set[str] = set()

    def add(command: str) -> None:
        command = (command or "").strip()
        if not command or command in seen:
            return
        if not _COMMAND_BLOCK_CODE_RE.search(command):
            return
        seen.add(command)
        commands.append(command)

    for block in _extract_all_fenced_blocks(text or ""):
        lang = (block.lang or "").lower()
        command = (block.code or "").strip()
        if lang not in _COMMAND_BLOCK_LANGS or not command:
            continue
        add(command)

    # Some planners put the SKILL.md command example directly in
    # final_instruction instead of wrapping it in a fenced block.  Only accept
    # self-contained command-looking lines; prose remains ignored.
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        line = re.sub(r"^(?:[-*]\s+|\$\s+)", "", line)
        if not line or line.startswith("```"):
            continue
        if not re.match(r"^(?:python(?:3)?|node|bash|sh)\s+", line):
            continue
        add(line)

    return commands


def _has_successful_run_command_observation(results: list[dict]) -> bool:
    return any(
        isinstance(item, dict)
        and item.get("action") == "run_command"
        and item.get("success", True)
        for item in results
    )


def _execution_requires_run_command_observation(runtime_plan: dict) -> bool:
    final_instruction = str(runtime_plan.get("final_instruction") or "")
    return bool(
        _extract_executable_command_blocks_from_text(final_instruction)
        or _final_instruction_requests_host_command(final_instruction)
    )


def _should_force_skill_workflow(*, command_contract: dict, user_text: str = "") -> str:
    """Return a reason when a declared multi-script Skill must run deterministically."""
    action_schema = (command_contract or {}).get("action_schema") or {}
    entries = [entry for entry in (action_schema.get("entries") or []) if isinstance(entry, dict)]
    script_entries = [
        entry for entry in entries
        if _normalize_skill_resource_path(str(entry.get("script_path") or "")).startswith("scripts/")
    ]
    if not script_entries:
        return ""

    roles = {str(entry.get("role") or "") for entry in script_entries}
    commands_text = "\n".join(str(entry.get("command") or "") for entry in script_entries)
    output_text = " ".join(
        " ".join(str(item) for item in (entry.get("outputs") or []))
        for entry in script_entries
    )
    artifact_requested = bool(re.search(
        r"(?i)(生成|创建|导出|制作|文件|图片|插图|PDF|Word|PPT|docx|pptx|pdf|image|illustration|file)",
        user_text or "",
    ))
    artifact_roles = {
        "image_generator",
        "pdf_builder",
        "docx_builder",
        "pptx_builder",
        "html_asset_builder",
        "asset_builder",
        "composite_generator",
    }
    artifact_declared = bool(
        roles & artifact_roles
        or re.search(
            r"(?i)(\.pdf|\.png|\.jpe?g|\.gif|\.webp|\.docx|\.pptx|\.xlsx|\.html?)",
            output_text + "\n" + commands_text,
        )
    )

    if len(script_entries) >= 2:
        if artifact_requested or artifact_declared:
            return "Action schema 声明了多个 scripts/*.py 执行入口，且任务/输出涉及文件类产物，必须由后端按 schema 顺序执行 workflow"
        return "Action schema 声明了多个 scripts/*.py 执行入口，属于复合 Skill，必须由后端按 schema 顺序执行 workflow"
    return ""


# Public alias
extract_executable_command_blocks_from_text = _extract_executable_command_blocks_from_text
