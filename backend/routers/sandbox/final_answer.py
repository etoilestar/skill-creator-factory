"""最终答案与块规划器。"""

import json
import logging

from ...services.llm_proxy import complete_chat_once
from ..chat_utils import (
    _ALLOWED_PLAN_ACTIONS,
    _blocks_for_planner,
    _request_messages_with_files,
    _strip_markdown_json_fence,
)
from ..chat_models import ChatRequest, MarkdownBlock

logger = logging.getLogger(__name__)


def _compose_final_answer_prompt() -> str:
    """Generate final answer from action observations."""
    return (
        "你是 Skill Agent 的最终回答生成器。\n\n"
        "你会收到用户请求、Loaded SKILL.md、运行时 action plan、主模型动作前草稿 assistant_draft "
        "以及 executor observation。\n\n"
        "你的任务是基于这些材料生成最终给用户看的结果。\n\n"
        "核心规则：\n"
        "1. 必须遵循 Loaded SKILL.md 的输出格式要求。\n"
        "2. 如果 assistant_draft 中包含有用的正文草稿，可以保留并整理。\n"
        "3. 如果 assistant_draft 中包含用于执行的 fenced command block，最终回答中不要保留这些命令块。\n"
        "4. 如果命令 stdout 是 JSON，应解析其中的 text、markdown、image、image_path、file、path 等字段。\n"
        "5. 如果 observation 中有 output_files，应把对应 url/path 作为 Markdown 链接或图片插入。\n"
        "6. 如果生成的是图片文件，优先用 Markdown 图片语法展示：![说明](路径或URL)。\n"
        "7. 不要输出 base64 data URI，除非 observation 里没有文件路径且 Skill 明确要求 base64。\n"
        "8. 不要输出内部 JSON、plan、完整 SKILL.md 或执行日志。\n"
        "9. 不要假装执行未发生的动作；如果命令失败，简要说明失败原因。\n"
    )

async def _generate_final_answer_from_observation(
    *,
    body_prompt: str,
    request: ChatRequest,
    model: str,
    plan: dict,
    execution_result: dict,
) -> str:
    messages = [
        {"role": "system", "content": _compose_final_answer_prompt()},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "loaded_skill_prompt": body_prompt,
                    "user_messages": _request_messages_with_files(request),
                    "plan": plan,
                    "execution_result": execution_result,
                },
                ensure_ascii=False,
            ),
        },
    ]

    return await complete_chat_once(messages, model)

def _compose_block_planner_prompt() -> str:
    return (
        "你是 Agent 运行时的动作规划器。\n\n"
        "你的唯一输入依据是：主模型已经生成的 assistant_text，以及从 assistant_text 中抽取出的 fenced code block。\n"
        "你不能根据 SKILL.md 模板、系统提示或用户原始意图凭空生成动作。\n\n"
        "核心规则：\n"
        "1. 只能判断 assistant_text 中已经出现的代码块。\n"
        "2. write_file 的文件内容必须来自对应 block 的 code，不能来自其他 block，不能来自解释文字。\n"
        "3. write_file 的 path 必须出现在该代码块紧邻前文中，通常应是代码块前最后 1 到 3 行里的「写入文件：<path>」或「保存到：<path>」。\n"
        "3a. 如果 assistant_text 中已经创建了某个 Skill 根目录，例如 `skills/ai-course-skill/scripts`、"
        "`skills/ai-course-skill/references` 或 `skills/ai-course-skill/assets`，"
        "那么后续写入 `SKILL.md` 必须绑定为 `skills/ai-course-skill/SKILL.md`，"
        "写入 `scripts/main.py` 必须绑定为 `skills/ai-course-skill/scripts/main.py`。\n"
        "3b. 禁止把新 Skill 的 `SKILL.md` 规划为宿主根目录下的 `SKILL.md`。\n"
        "3c. 禁止把新 Skill 的脚本规划为宿主根目录下的 `scripts/main.py`。\n"
        "4. 如果 path 出现在更早的段落、标题、列表或其他代码块附近，不允许把它绑定到当前 block。\n"
        "5. 如果当前 block 前后同时出现多个路径，或者路径与当前 block 内容主题明显不一致，不要猜测，写入 errors。\n"
        "6. 如果当前 block 的前文说写入 SKILL.md，但 block 内容明显是在描述其他文件、步骤、说明文字或另一个文件内容，不允许写入 SKILL.md。\n"
        "7. 如果当前 block 的前文说写入某个文件，但 block 内容明显不是该文件的完整内容，不允许写入该文件。\n"
        "8. 如果代码块表达的是创建目录，不要输出 run_command，必须输出 create_directory。\n"
        "9. 如果一个代码块中创建多个目录，必须拆成多个 create_directory 任务，每个任务一个 path。\n"
        "10. 对于修改宿主状态但宿主没有原生动作支持的操作，应优先 ignore，不要强行归类为 run_command。\n"
        "11. run_command 只用于确实需要运行外部程序、脚本或工具的命令，不要把目录创建、文件写入这类可由宿主原生动作完成的操作归类为 run_command。\n"
        "12. 如果代码块只是示例、说明、模板、教程、展示内容，则 action=display 或 ignore。\n"
        "13. 如果路径、执行意图、命令来源不明确，不要猜测，把问题写入 errors。\n"
        "14. 不允许根据用户希望、SKILL.md 用法、资源清单或常识补全缺失路径。\n"
        "15. 只输出严格 JSON，不要 Markdown，不要解释。\n\n"
        "允许的 action：display、ignore、write_file、run_command、create_directory。\n\n"
        "write_file 的 encoding 字段：\n"
        "- 文本文件（.py/.js/.md/.txt/.json/.yaml/.csv/.html/.css 等）：不需要指定 encoding，默认为 text。\n"
        "- 二进制文件（.png/.jpg/.pdf/.xlsx/.docx/.zip 等）：必须设置 encoding 为 \"base64\"，此时 content 应为 base64 编码字符串。\n\n"
        "输出格式：\n"
        "{\n"
        "  \"tasks\": [\n"
        "    {\"block_index\": 0, \"action\": \"create_directory\", \"path\": \"...\", \"reason\": \"...\"},\n"
        "    {\"block_index\": 1, \"action\": \"write_file\", \"path\": \"...\", \"reason\": \"...\"},\n"
        "    {\"block_index\": 2, \"action\": \"write_file\", \"path\": \"assets/logo.png\", \"encoding\": \"base64\", \"reason\": \"...\"},\n"
        "    {\"block_index\": 3, \"action\": \"run_command\", \"command\": \"...\", \"reason\": \"...\"}\n"
        "  ],\n"
        "  \"errors\": []\n"
        "}\n"
    )

async def _run_block_planner_round(
        *,
        assistant_text: str,
        blocks: list[MarkdownBlock],
        request: ChatRequest,
        model: str,
) -> dict:
    """Run a silent planning round after the main model has produced assistant_text."""
    if not blocks:
        return {"tasks": [], "errors": []}

    planner_payload = {
        "user_messages": _request_messages_with_files(request),
        "assistant_text": assistant_text,
        "blocks": _blocks_for_planner(blocks),
        "runtime_constraints": {
            "block_source": "assistant_text_only",
            "path_source": "assistant_text_near_block_context",
            "content_source": "selected_block_code",
            "command_source": "assistant_text_executable_block_or_near_block_context",
            "directory_creation": {
                "preferred_action": "create_directory",
                "rule": "目录创建应使用 create_directory，不应使用 run_command。",
                "multiple_paths": "如果一次创建多个目录，拆成多个 create_directory 任务。",
            },
            "do_not_use": [
                "SKILL.md code example that was not present in assistant_text",
                "system prompt",
                "resource manifest",
                "implicit intent",
                "guessed path",
                "guessed command",
            ],
        },
    }

    messages = [
        {"role": "system", "content": _compose_block_planner_prompt()},
        {"role": "user", "content": json.dumps(planner_payload, ensure_ascii=False)},
    ]

    planner_text = await complete_chat_once(messages, model)

    try:
        stripped = _strip_markdown_json_fence(planner_text)
        plan = json.loads(stripped)
    except json.JSONDecodeError:
        logger.warning(
            "Block planner returned non-JSON on first attempt, retrying with correction prompt: %s",
            planner_text[:300],
        )
        retry_messages = messages + [
            {"role": "assistant", "content": planner_text},
            {
                "role": "user",
                "content": (
                    "你的上一次回复不是合法 JSON。请把它修正为一个严格 JSON object。\n"
                    "只输出 JSON，不要 Markdown，不要解释，不要代码块标记。\n"
                    "格式必须是：{\"tasks\":[...],\"errors\":[...]}。"
                ),
            },
        ]
        planner_text = await complete_chat_once(retry_messages, model)
        try:
            stripped = _strip_markdown_json_fence(planner_text)
            plan = json.loads(stripped)
        except json.JSONDecodeError as exc:
            logger.error("Received invalid JSON response from planner after retry: %s", planner_text)
            raise ValueError(f"规划模型没有返回合法 JSON: {planner_text[:500]}") from exc

    if not isinstance(plan, dict):
        raise ValueError("规划模型输出必须是 JSON object")

    tasks = plan.get("tasks", [])
    errors = plan.get("errors", [])

    if not isinstance(tasks, list):
        raise ValueError("规划模型输出的 tasks 必须是数组")

    if not isinstance(errors, list):
        errors = []

    normalized_tasks: list[dict] = []

    for task in tasks:
        if not isinstance(task, dict):
            continue

        action = str(task.get("action", "")).strip()

        if action not in _ALLOWED_PLAN_ACTIONS:
            errors.append({"error": f"不支持的 action: {action}", "task": task})
            continue

        try:
            block_index = int(task.get("block_index", -1))
        except (TypeError, ValueError):
            block_index = -1

        if action in {"write_file", "run_command"} and not (0 <= block_index < len(blocks)):
            errors.append({"error": "任务缺少合法 block_index", "task": task})
            continue

        if action in {"write_file", "create_directory"} and not str(task.get("path") or "").strip():
            errors.append({"error": f"{action} 缺少 path", "task": task})
            continue

        if action == "run_command":
            block = blocks[block_index]
            command = str(task.get("command") or block.code or "").strip()
            if not command:
                errors.append({"error": "run_command 缺少 command", "task": task})
                continue
            task["command"] = command

        task["block_index"] = block_index
        normalized_tasks.append(task)

    return {"tasks": normalized_tasks, "errors": errors}
