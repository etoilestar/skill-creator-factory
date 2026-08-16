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
    """Use executable responsibility and declared dataflow, never user keywords."""
    action_schema = (command_contract or {}).get("action_schema") or {}
    entries = [entry for entry in (action_schema.get("entries") or []) if isinstance(entry, dict)]
    script_entries = [
        entry for entry in entries
        if _normalize_skill_resource_path(str(entry.get("script_path") or "")).startswith("scripts/")
    ]
    if not script_entries:
        return ""

    if len(script_entries) >= 2:
        return "Action schema 声明多个宿主执行入口，需要 Sandbox Runtime Planner 实例化步骤和数据依赖"
    return ""


# Public alias
extract_executable_command_blocks_from_text = _extract_executable_command_blocks_from_text
