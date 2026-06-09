"""Sandbox-mode chat router — thin facade over the sandbox sub-package.

This module re-exports the router and public API from the sandbox sub-package
so that existing imports (main.py, creator_chat.py, tests) continue to work
without any changes.
"""

# Router — mounted by main.py
from .sandbox.stream_pipeline import router  # noqa: F401

# Public functions — imported by creator_chat.py and test files
from .sandbox.legacy_fallback import plan_and_execute_generated_output  # noqa: F401
from .sandbox.metadata_decisions import (  # noqa: F401
    parse_need_body_decision,
    parse_child_skill_decision,
)
from .sandbox.resource_catalog import (  # noqa: F401
    parse_resource_selection_decision,
    extract_runtime_resource_catalog,
    resource_catalog_for_planner,
    _compose_resource_selection_prompt,
)
from .sandbox.runtime_planner import (  # noqa: F401
    normalize_skill_runtime_plan,
    compose_skill_runtime_planner_prompt,
)
from .sandbox.resource_loader import compose_loaded_resources_prompt  # noqa: F401
from .sandbox.action_schema import (  # noqa: F401
    extract_skill_command_contract,
    build_runtime_action_schema,
    validate_runtime_command_against_action_schema,
    validate_stdout_against_action_entry,
)
from .sandbox.workflow_detection import extract_executable_command_blocks_from_text  # noqa: F401
from .sandbox.task_executor import execute_single_task  # noqa: F401
from .sandbox.multimodal import request_messages_with_inline_images  # noqa: F401
from .sandbox.stdout_render import (  # noqa: F401
    render_success_stdout_payload,
    validate_success_stdout_json_if_structured,
)
from .sandbox.output_links import finalize_answer_output_file_links  # noqa: F401
from .sandbox.path_resolution import available_scripts_for_root  # noqa: F401
from .sandbox.error_correction import (  # noqa: F401
    output_files_from_stdout_json,
    parse_error_correction_decision,
    apply_error_correction,
    compose_error_correction_prompt,
)
from .sandbox.workflow_dataflow import (  # noqa: F401
    execute_skill_workflow,
    validate_workflow_dataflow_plan,
    render_command_template,
    merge_step_output,
)
from .sandbox.sop_planner import format_task_checklist_markdown  # noqa: F401
from .sandbox.stream_pipeline import build_skill_context  # noqa: F401
from .sandbox.final_answer import _run_block_planner_round  # noqa: F401
from .sandbox.instruction_analysis import _run_instruction_analysis_round  # noqa: F401
from .sandbox.chat_utils_compat import _is_within_sandbox  # noqa: F401

# Backward-compatible aliases with underscore prefix (used by test files)
_plan_and_execute_generated_output = plan_and_execute_generated_output  # noqa: F401
_parse_need_body_decision = parse_need_body_decision  # noqa: F401
_parse_child_skill_decision = parse_child_skill_decision  # noqa: F401
_parse_resource_selection_decision = parse_resource_selection_decision  # noqa: F401
_extract_runtime_resource_catalog = extract_runtime_resource_catalog  # noqa: F401
_resource_catalog_for_planner = resource_catalog_for_planner  # noqa: F401
_normalize_skill_runtime_plan = normalize_skill_runtime_plan  # noqa: F401
_compose_skill_runtime_planner_prompt = compose_skill_runtime_planner_prompt  # noqa: F401
_compose_loaded_resources_prompt = compose_loaded_resources_prompt  # noqa: F401
_extract_skill_command_contract = extract_skill_command_contract  # noqa: F401
_build_runtime_action_schema = build_runtime_action_schema  # noqa: F401
_validate_runtime_command_against_action_schema = validate_runtime_command_against_action_schema  # noqa: F401
_validate_stdout_against_action_entry = validate_stdout_against_action_entry  # noqa: F401
_extract_executable_command_blocks_from_text = extract_executable_command_blocks_from_text  # noqa: F401
_execute_single_task = execute_single_task  # noqa: F401
_request_messages_with_inline_images = request_messages_with_inline_images  # noqa: F401
_render_success_stdout_payload = render_success_stdout_payload  # noqa: F401
_validate_success_stdout_json_if_structured = validate_success_stdout_json_if_structured  # noqa: F401
_finalize_answer_output_file_links = finalize_answer_output_file_links  # noqa: F401
_available_scripts_for_root = available_scripts_for_root  # noqa: F401
_output_files_from_stdout_json = output_files_from_stdout_json  # noqa: F401
_parse_error_correction_decision = parse_error_correction_decision  # noqa: F401
_apply_error_correction = apply_error_correction  # noqa: F401
_compose_error_correction_prompt = compose_error_correction_prompt  # noqa: F401
_execute_skill_workflow = execute_skill_workflow  # noqa: F401
_validate_workflow_dataflow_plan = validate_workflow_dataflow_plan  # noqa: F401
_format_task_checklist_markdown = format_task_checklist_markdown  # noqa: F401
_compose_resource_selection_prompt = _compose_resource_selection_prompt  # noqa: F401
_run_block_planner_round = _run_block_planner_round  # noqa: F401
