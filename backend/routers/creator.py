"""Creator router — file-by-file Skill generation endpoints.

These endpoints decouple the file-creation phase from the main
/api/chat/creator conversation endpoint:

- POST /api/creator/analyze-blueprint  — extract file list from blueprint (no LLM)
- POST /api/creator/init-skill          — create Skill directory structure
- POST /api/creator/generate-file       — SSE: stream single-file content from LLM
- POST /api/creator/write-file          — write generated content to disk
- POST /api/creator/validate-skill      — validate SKILL.md format
- POST /api/creator/package-skill       — package Skill directory into .skill archive
"""

import json
import logging
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..config import settings
from ..services.blueprint_parser import BlueprintPlan, parse_blueprint
from ..services.llm_proxy import stream_chat
from ..services.skill_executor import run_action

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/creator", tags=["creator"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Directories allowed as parents when writing non-SKILL.md files.
_ALLOWED_FOLDERS: frozenset[str] = frozenset({"scripts", "references", "assets"})

# Trailing conversation turns to include in file-generation prompts.
_MAX_HISTORY_TURNS = 6

# Human-readable language labels indexed by file extension.
_LANG_LABELS: dict[str, str] = {
    ".py":       "Python",
    ".js":       "JavaScript",
    ".mjs":      "JavaScript",
    ".cjs":      "JavaScript",
    ".ts":       "TypeScript",
    ".sh":       "Bash",
    ".bash":     "Bash",
    ".rb":       "Ruby",
    ".go":       "Go",
    ".md":       "Markdown",
    ".yaml":     "YAML",
    ".yml":      "YAML",
    ".json":     "JSON",
    ".toml":     "TOML",
    ".txt":      "Text",
    ".jinja":    "Jinja2 模板",
    ".jinja2":   "Jinja2 模板",
    ".template": "模板文件",
    ".tmpl":     "模板文件",
}


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class AnalyzeBlueprintRequest(BaseModel):
    messages: list[dict]
    model: Optional[str] = None


class FileSpecOut(BaseModel):
    path: str
    purpose: str
    required: bool
    can_skip: bool


class AnalyzeBlueprintResponse(BaseModel):
    skill_name: str
    files: list[FileSpecOut]
    warnings: list[str]


class InitSkillRequest(BaseModel):
    skill_name: str


class InitSkillResponse(BaseModel):
    success: bool
    path: Optional[str] = None
    message: str


class GenerateFileRequest(BaseModel):
    skill_name: str
    file_path: str
    purpose: str
    blueprint_text: str
    conversation_history: list[dict]
    model: Optional[str] = None


class WriteFileRequest(BaseModel):
    skill_name: str
    file_path: str
    content: str


class WriteFileResponse(BaseModel):
    success: bool
    path: Optional[str] = None
    bytes: int = 0
    message: str


class SkillActionRequest(BaseModel):
    skill_name: str


class SkillActionResponse(BaseModel):
    success: bool
    path: Optional[str] = None
    message: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _validate_file_path(file_path: str) -> None:
    """Raise HTTP 400 if file_path is outside allowed locations."""
    p = Path(file_path)
    if p.is_absolute() or ".." in p.parts:
        raise HTTPException(status_code=400, detail=f"非法文件路径: {file_path}")

    if file_path == "SKILL.md":
        return

    if not p.parts or p.parts[0] not in _ALLOWED_FOLDERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"文件路径 '{file_path}' 不合法。"
                f"只允许 SKILL.md 或 scripts/*、references/*、assets/* 下的文件。"
            ),
        )

    filename = p.name
    if not filename or filename.startswith(".") or "\x00" in filename or len(filename) > 255:
        raise HTTPException(status_code=400, detail=f"文件名非法: {filename!r}")


def _validate_skill_name(skill_name: str) -> str:
    """Strip, validate, and return the skill_name or raise HTTP 400."""
    name = skill_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="skill_name 不能为空。")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise HTTPException(
            status_code=400,
            detail="skill_name 只能包含小写字母、数字和连字符，且必须以字母或数字开头。",
        )
    return name


def _strip_code_fence(content: str) -> str:
    """Strip wrapping code-fence markers that a model may output despite instructions.

    Handles:
      ```python\\n<code>\\n```
      ```\\n<code>\\n```
      ~~~~\\n<code>\\n~~~~
    """
    stripped = content.strip()
    # Opening fence with optional language tag
    m_open = re.match(r"^(`{3,}|~{3,})[^\n]*\n", stripped)
    if m_open:
        fence_char = stripped[0]
        rest = stripped[m_open.end():]
        close_pat = re.compile(r"\n" + re.escape(fence_char) + r"{3,}\s*$")
        m_close = close_pat.search(rest)
        if m_close:
            return rest[: m_close.start()].strip()
        return rest.strip()
    return stripped


def _build_generate_file_prompt(
    file_path: str,
    skill_name: str,
    purpose: str,
    blueprint_text: str,
    conversation_history: list[dict],
) -> list[dict]:
    """Build a minimal generation prompt for a single Skill file.

    The model is asked to output *only* raw file content — no fences, no JSON,
    no explanations.  This maximises reliability for small or unstable models.
    """
    ext = Path(file_path).suffix.lower()
    lang = _LANG_LABELS.get(ext, "文本")

    if file_path == "SKILL.md":
        instruction = (
            f'你正在为 Skill 包 "{skill_name}" 生成 SKILL.md 文件。\n\n'
            "要求：\n"
            "1. 只输出 SKILL.md 的文件内容，不要任何解释，不要 Markdown 代码块包裹。\n"
            "2. 文件必须以 YAML frontmatter 开始，格式严格如下（冒号后有一个空格）：\n"
            "---\n"
            f"name: {skill_name}\n"
            "description: <一句话说明本 Skill 的用途>\n"
            "---\n"
            "3. frontmatter 闭合后，输出 Skill 的核心执行说明（普通 Markdown 正文）。\n"
            "4. 执行说明应指导宿主 AI 如何理解用户请求、调用脚本/工具、生成回答。\n"
            "5. 不要在输出内容的外侧套 ``` 代码块。\n\n"
            f"以下是已确认的蓝图，你的内容必须与此一致：\n\n{blueprint_text}"
        )
    elif file_path.startswith("scripts/"):
        instruction = (
            f'你正在为 Skill 包 "{skill_name}" 生成 {file_path} 文件。\n\n'
            f"职责说明：{purpose}\n\n"
            "要求：\n"
            f"1. 只输出完整可运行的 {lang} 代码，不要任何说明文字。\n"
            "2. 不要用 ``` 代码块包裹输出内容。\n"
            "3. 脚本的命令行参数、stdin/stdout 接口必须与蓝图中描述的一致。\n"
            "4. 所有导入的第三方库必须真实存在且常见。\n"
            "5. 包含必要的错误处理逻辑（如参数校验、文件不存在提示等）。\n\n"
            f"以下是已确认的蓝图：\n\n{blueprint_text}"
        )
    elif file_path.startswith("references/"):
        instruction = (
            f'你正在为 Skill 包 "{skill_name}" 生成 {file_path} 参考资料文件。\n\n'
            f"职责说明：{purpose}\n\n"
            "要求：\n"
            "1. 只输出 Markdown 文档内容，不要额外的说明文字。\n"
            "2. 不要在文档外套 ``` 代码块。\n"
            "3. 内容应是有实际指导价值的参考资料，不是对参考资料的再描述。\n\n"
            f"以下是已确认的蓝图（参考资料职责说明见 references/ 部分）：\n\n{blueprint_text}"
        )
    elif file_path.startswith("assets/"):
        instruction = (
            f'你正在为 Skill 包 "{skill_name}" 生成 {file_path} 资源文件。\n\n'
            f"职责说明：{purpose}\n\n"
            "要求：\n"
            f"1. 只输出 {lang} 格式的文件内容，不要任何说明文字。\n"
            "2. 不要用 ``` 代码块包裹输出。\n\n"
            f"以下是已确认的蓝图：\n\n{blueprint_text}"
        )
    else:
        instruction = (
            f'你正在为 Skill 包 "{skill_name}" 生成 {file_path} 文件。\n\n'
            f"职责说明：{purpose}\n\n"
            "要求：直接输出文件内容，不要任何解释，不要 Markdown 代码块包裹。\n\n"
            f"蓝图：\n\n{blueprint_text}"
        )

    messages: list[dict] = [{"role": "system", "content": instruction}]

    # Include recent conversation turns for context but cap to limit token usage.
    for msg in conversation_history[-_MAX_HISTORY_TURNS:]:
        if isinstance(msg, dict) and msg.get("role") in {"user", "assistant"}:
            messages.append(msg)

    return messages


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/analyze-blueprint", response_model=AnalyzeBlueprintResponse)
async def analyze_blueprint(request: AnalyzeBlueprintRequest):
    """Extract a file-creation plan from the conversation blueprint.

    Pure rule-based extraction — no LLM call is made.
    """
    plan: BlueprintPlan = parse_blueprint(request.messages)
    return AnalyzeBlueprintResponse(
        skill_name=plan.skill_name,
        files=[
            FileSpecOut(
                path=f.path,
                purpose=f.purpose,
                required=f.required,
                can_skip=f.can_skip,
            )
            for f in plan.files
        ],
        warnings=plan.warnings,
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


@router.post("/generate-file")
async def generate_file(request: GenerateFileRequest):
    """Stream the generated content for a single Skill file.

    SSE event shapes:
      ``{ "content": "<chunk>" }``  — content chunk
      ``{ "done": true }``           — generation complete
      ``{ "error": "<message>" }``   — generation failed
    """
    _validate_file_path(request.file_path)
    skill_name = _validate_skill_name(request.skill_name)
    model = request.model or settings.default_model

    prompt_messages = _build_generate_file_prompt(
        file_path=request.file_path,
        skill_name=skill_name,
        purpose=request.purpose,
        blueprint_text=request.blueprint_text,
        conversation_history=request.conversation_history,
    )

    async def event_stream():
        try:
            async for chunk in stream_chat(prompt_messages, model):
                yield _sse({"content": chunk})
            yield _sse({"done": True})
        except Exception as exc:
            logger.exception(
                "generate-file stream error skill=%s file=%s",
                skill_name,
                request.file_path,
            )
            # Return a safe user-facing message; full stack trace is in server logs.
            yield _sse({"error": "文件内容生成失败，请重试。详情已记录在服务器日志中。"})
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/write-file", response_model=WriteFileResponse)
async def write_file(request: WriteFileRequest):
    """Write generated content to a Skill file on disk.

    Automatically strips spurious code-fence wrappers the LLM may add.
    """
    _validate_file_path(request.file_path)
    skill_name = _validate_skill_name(request.skill_name)

    content = _strip_code_fence(request.content)

    if request.file_path == "SKILL.md":
        result = run_action({"action": "write", "name": skill_name, "content": content})
    else:
        p = Path(request.file_path)
        folder = p.parts[0]   # "scripts" / "references" / "assets"
        filename = p.name
        result = run_action(
            {
                "action": "write_file",
                "name": skill_name,
                "folder": folder,
                "filename": filename,
                "content": content,
            }
        )

    return WriteFileResponse(
        success=result["success"],
        path=result.get("path"),
        bytes=len(content.encode("utf-8")) if result["success"] else 0,
        message=result["message"],
    )


@router.post("/validate-skill", response_model=SkillActionResponse)
async def validate_skill(request: SkillActionRequest):
    """Validate SKILL.md format for a Skill package."""
    skill_name = _validate_skill_name(request.skill_name)
    result = run_action({"action": "validate", "name": skill_name})
    return SkillActionResponse(
        success=result["success"],
        path=result.get("path"),
        message=result["message"],
    )


@router.post("/package-skill", response_model=SkillActionResponse)
async def package_skill(request: SkillActionRequest):
    """Package a Skill directory into a distributable .skill archive."""
    skill_name = _validate_skill_name(request.skill_name)
    result = run_action({"action": "package", "name": skill_name})
    return SkillActionResponse(
        success=result["success"],
        path=result.get("path"),
        message=result["message"],
    )
