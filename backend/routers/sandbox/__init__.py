"""Sandbox chat sub-package — re-exports public API.

模块组织：
- path_resolution: 路径解析与沙箱边界检查
- resource_catalog: 资源目录提取与 LLM 资源选择
- resource_loader: 资源读取与加载提示
- metadata_decisions: 元数据与子技能决策
- multimodal: 多模态图片嵌入与清单
- instruction_analysis: 用户指令语义分析
- sop_planner: SOP 生成与计划确认
- action_schema: Action Schema 提取与验证
- workflow_detection: 命令检测与工作流强制
- runtime_planner: 运行时规划器
- final_answer: 最终答案与块规划器
- command_executor: 命令准备与子进程执行
- error_correction: LLM 错误纠正与重试
- workflow_dataflow: 工作流数据流规划与执行
- task_executor: 单任务与批量任务执行
- output_links: 输出文件链接重写
- stdout_render: Stdout 验证与渲染
- legacy_fallback: 旧版 Markdown 块执行兜底
- stream_pipeline: 主流式管道与路由端点
"""

# Router — main.py imports this
from .stream_pipeline import router

# Public functions used by creator_chat.py
from .legacy_fallback import plan_and_execute_generated_output

# Public functions used by test files
from .metadata_decisions import parse_need_body_decision, parse_child_skill_decision
from .resource_catalog import parse_resource_selection_decision
from .runtime_planner import normalize_skill_runtime_plan
from .resource_catalog import extract_runtime_resource_catalog
from .resource_loader import compose_loaded_resources_prompt
from .action_schema import (
    extract_skill_command_contract,
    build_runtime_action_schema,
    validate_runtime_command_against_action_schema,
)
from .workflow_detection import extract_executable_command_blocks_from_text
from .task_executor import execute_single_task
from .multimodal import request_messages_with_inline_images
from .stdout_render import (
    render_success_stdout_payload,
    validate_success_stdout_json_if_structured,
)
from .output_links import finalize_answer_output_file_links
from .runtime_planner import compose_skill_runtime_planner_prompt
from .path_resolution import available_scripts_for_root
from .error_correction import output_files_from_stdout_json
from .resource_catalog import resource_catalog_for_planner
from .sop_planner import format_task_checklist_markdown
from .error_correction import (
    parse_error_correction_decision,
    apply_error_correction,
    compose_error_correction_prompt,
)
from .workflow_dataflow import execute_skill_workflow, validate_workflow_dataflow_plan
from .action_schema import validate_stdout_against_action_entry
from .stream_pipeline import build_skill_context
