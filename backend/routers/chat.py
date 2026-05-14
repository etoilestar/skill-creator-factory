import asyncio
import functools
import hashlib
import os
import shutil
import json
import logging
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..config import settings
from ..services.kernel_loader import (
    load_kernel_metadata_prompt,
    load_kernel_body_prompt,
    load_skill_metadata_prompt,
    load_skill_body_prompt,
    load_child_skill_body_prompt,
    read_skill_resource_text,
)
from ..services.llm_proxy import complete_chat_once, stream_chat

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])


class Message(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    messages: list[Message]
    model: Optional[str] = None
    input_files: list[dict] = []  # [{"path": "inputs/session/file.csv", "filename": "file.csv"}, ...]


@dataclass
class MarkdownBlock:
    """A fenced Markdown block from the main model output."""
    index: int
    lang: str
    code: str
    before_context: str
    after_context: str


@dataclass
class CreatorRequirementAnalysis:
    """Best-effort slot analysis for the creator requirement-collection phase."""

    user_turns: int
    collected_slots: list[str]
    missing_slots: list[str]
    ready_for_blueprint: bool
    next_question: str


@dataclass
class CreatorStateContext:
    """Resolved creator state together with requirement-analysis context."""

    state: str
    blueprint_shown: bool
    requirements: CreatorRequirementAnalysis


_EXECUTABLE_FENCE_RE = re.compile(
    r"```(?P<lang>bash|sh|shell)\s*\n(?P<code>.*?)\n```",
    re.IGNORECASE | re.DOTALL,
)

_ALL_FENCE_RE = re.compile(
    r"(?P<fence>`{3,})(?P<info>[^\n`]*)\n(?P<code>[\s\S]*?)\n(?=\1)",
    re.IGNORECASE | re.DOTALL,
)

_PYTHON_HEREDOC_RE = re.compile(
    r"^\s*(?P<python>python3?|[\w./-]*python3?)\s+-\s+<<[ \t]*['\"]?(?P<tag>[A-Za-z_][A-Za-z0-9_]*)['\"]?[ \t]*\n"
    r"(?P<script>.*?)\n(?P=tag)[ \t]*;?\s*$",
    re.DOTALL,
)

_CONFIRM_KEYWORDS = (
    "对，开始做吧",
    "开始做吧",
    "开始创建",
    "开始生成",
    "确认，开始",
    "确认开始",
    "可以开始",
    "没问题，开始",
)

# Marker written by the model when it outputs a blueprint (state B).
_BLUEPRINT_MARKERS = ("📋 Skill 蓝图",)
_CREATOR_PATTERN_CONTEXT_CHARS = 40

_CREATOR_INPUT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(输入|用户会提供|用户输入|接收|读取|上传|原始数据|原文|素材|文本|文件|参数)"),
    re.compile(
        rf"(根据|基于|把|将).{{0,{_CREATOR_PATTERN_CONTEXT_CHARS}}}(整理|转换|提取|生成|改写|总结|分类|分析)",
        re.DOTALL,
    ),
)
_CREATOR_OUTPUT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(输出|返回|生成|产出|给出|得到|结果|报告|摘要|内容|结论)"),
    re.compile(r"(整理成|转换成|提取出|生成出)"),
)
_CREATOR_SCENARIO_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(例如|比如|场景|触发|真实例子|用户会说|示例)"),
)
_CREATOR_RESOURCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(脚本|script|references/|assets/|api|接口|数据库|环境变量|依赖|模型|文件处理|外部服务)", re.IGNORECASE),
    re.compile(r"(不需要|无需|只靠模型|纯提示词).{0,20}(脚本|api|接口|数据库|依赖|外部服务)", re.IGNORECASE),
)

_FORBIDDEN_PATH_PARTS = {"..", ""}
_SHELL_META_CHARS = ("|", "&", ";", ">", "<", "$", "`", "\n")
_ALLOWED_PLAN_ACTIONS = {"display", "ignore", "write_file", "run_command", "create_directory"}

# 脚本扩展名 → 自动注入的解释器（方案 A+B）
# 注意：".ts" 是特殊情况，实际命令为 `npx ts-node <file>`，见 _prepare_command_argv。
_SCRIPT_INTERPRETERS: dict[str, str] = {
    ".js":   "node",
    ".mjs":  "node",
    ".cjs":  "node",
    ".py":   "python3",
    ".sh":   "bash",
    ".bash": "bash",
    ".rb":   "ruby",
    ".ts":   "ts-node",   # 特殊处理：通过 `npx ts-node` 执行
}

# 解释器 → apt 包名映射（运行时自动安装兜底）
_INTERPRETER_APT_PACKAGES: dict[str, str] = {
    "node":    "nodejs",
    "nodejs":  "nodejs",
    "npm":     "npm",
    "npx":     "npm",      # npx 随 npm 一起安装
    "ts-node": "ts-node",  # 通过 npm 全局安装，见下方特殊处理
    "ruby":    "ruby",
    "bash":    "bash",
    "python3": "python3",
}

# 已尝试自动安装的解释器集合（避免同一进程内重复安装）
_auto_install_attempted: set[str] = set()
_auto_install_lock = __import__("threading").Lock()
_apt_update_performed: bool = False

# Python import 名 → pip 包名映射表（约 70 条常用别名）
_IMPORT_TO_PACKAGE: dict[str, str] = {
    "cv2":           "opencv-python",
    "PIL":           "Pillow",
    "pptx":          "python-pptx",
    "sklearn":       "scikit-learn",
    "bs4":           "beautifulsoup4",
    "yaml":          "pyyaml",
    "dotenv":        "python-dotenv",
    "magic":         "python-magic",
    "usb":           "pyusb",
    "serial":        "pyserial",
    "dateutil":      "python-dateutil",
    "boto3":         "boto3",
    "botocore":      "botocore",
    "paramiko":      "paramiko",
    "cryptography":  "cryptography",
    "nacl":          "pynacl",
    "Crypto":        "pycryptodome",
    "OpenSSL":       "pyOpenSSL",
    "jwt":           "PyJWT",
    "aiohttp":       "aiohttp",
    "httpx":         "httpx",
    "requests":      "requests",
    "flask":         "Flask",
    "fastapi":       "fastapi",
    "uvicorn":       "uvicorn",
    "django":        "Django",
    "sqlalchemy":    "SQLAlchemy",
    "pymysql":       "PyMySQL",
    "psycopg2":      "psycopg2-binary",
    "redis":         "redis",
    "pymongo":       "pymongo",
    "celery":        "celery",
    "pika":          "pika",
    "kafka":         "kafka-python",
    "numpy":         "numpy",
    "pandas":        "pandas",
    "matplotlib":    "matplotlib",
    "scipy":         "scipy",
    "tensorflow":    "tensorflow",
    "torch":         "torch",
    "transformers":  "transformers",
    "tqdm":          "tqdm",
    "rich":          "rich",
    "click":         "click",
    "typer":         "typer",
    "loguru":        "loguru",
    "pydantic":      "pydantic",
    "toml":          "toml",
    "arrow":         "arrow",
    "pendulum":      "pendulum",
    "docx":          "python-docx",
    "openpyxl":      "openpyxl",
    "xlrd":          "xlrd",
    "xlwt":          "xlwt",
    "pypdf":         "pypdf",
    "fitz":          "PyMuPDF",
    "jinja2":        "Jinja2",
    "markdown":      "Markdown",
    "lxml":          "lxml",
    "pytest":        "pytest",
    "faker":         "Faker",
    "hypothesis":    "hypothesis",
    "parameterized": "parameterized",
}

# Node.js 内置模块集合（过滤用，不安装这些模块）
_NODE_BUILTIN_MODULES: frozenset[str] = frozenset({
    "assert", "async_hooks", "buffer", "child_process", "cluster",
    "console", "constants", "crypto", "dgram", "diagnostics_channel",
    "dns", "domain", "events", "fs", "http", "http2", "https",
    "inspector", "module", "net", "os", "path", "perf_hooks",
    "process", "punycode", "querystring", "readline", "repl",
    "stream", "string_decoder", "sys", "timers", "tls", "trace_events",
    "tty", "url", "util", "v8", "vm", "wasi", "worker_threads", "zlib",
    "node:assert", "node:async_hooks", "node:buffer", "node:child_process",
    "node:cluster", "node:console", "node:crypto", "node:dgram",
    "node:dns", "node:domain", "node:events", "node:fs", "node:http",
    "node:http2", "node:https", "node:inspector", "node:module",
    "node:net", "node:os", "node:path", "node:perf_hooks", "node:process",
    "node:readline", "node:repl", "node:stream", "node:string_decoder",
    "node:timers", "node:tls", "node:tty", "node:url", "node:util",
    "node:v8", "node:vm", "node:worker_threads", "node:zlib",
})

# 已创建 venv 的 skill 目录缓存（进程级，避免重复创建）
_venv_created_dirs: set[str] = set()
_venv_created_lock = __import__("threading").Lock()

# run_command 依赖缺失时最多重试次数
_MAX_DEP_RETRY = 3

# 目录快照时跳过的子目录名（虚拟环境、包缓存等）
_SNAPSHOT_EXCLUDE_DIRS: frozenset[str] = frozenset({
    ".venv", "node_modules", "__pycache__", ".runtime", ".git",
})


def _snapshot_dir_files(path: Path) -> set[str]:
    """Return a set of relative POSIX paths for all files under *path*.

    Directories in _SNAPSHOT_EXCLUDE_DIRS are skipped to avoid scanning
    virtual-env trees or node_modules.
    """
    result: set[str] = set()
    if not path.exists():
        return result
    for f in path.rglob("*"):
        if not f.is_file():
            continue
        try:
            rel = f.relative_to(path)
        except ValueError:
            continue
        if any(part in _SNAPSHOT_EXCLUDE_DIRS for part in rel.parts):
            continue
        result.add(rel.as_posix())
    return result


def _try_auto_install_interpreter(interpreter: str) -> bool:
    """尝试通过系统包管理器（apt-get）自动安装缺失的解释器。

    只在 Linux 环境且具备 apt-get 时生效；安装结果会被缓存，
    同一进程内同一解释器只尝试安装一次。
    返回 True 表示安装后解释器可用，False 表示安装失败或不支持。
    """
    global _apt_update_performed

    with _auto_install_lock:
        if interpreter in _auto_install_attempted:
            return shutil.which(interpreter) is not None
        _auto_install_attempted.add(interpreter)

    apt_get = shutil.which("apt-get")
    if apt_get is None:
        logger.info("auto-install: apt-get not available, skipping install of %s", interpreter)
        return False

    # ts-node 通过 npm 全局安装，而不是 apt-get
    if interpreter == "ts-node":
        npm_bin = shutil.which("npm")
        if npm_bin is None:
            logger.warning("auto-install: npm not found, cannot install ts-node")
            return False
        logger.info("auto-install: installing ts-node via npm ...")
        try:
            result = subprocess.run(
                [npm_bin, "install", "-g", "ts-node", "typescript"],
                timeout=120,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                logger.info("auto-install: ts-node installed successfully")
            else:
                logger.warning("auto-install: ts-node install failed: %s", result.stderr[:500])
        except Exception as exc:
            logger.warning("auto-install: ts-node install exception: %s", exc)
        return shutil.which("ts-node") is not None

    pkg = _INTERPRETER_APT_PACKAGES.get(interpreter)
    if pkg is None:
        logger.info("auto-install: no apt package known for interpreter %s", interpreter)
        return False

    logger.info("auto-install: apt-get install -y %s (for interpreter %s) ...", pkg, interpreter)
    try:
        # 先更新索引（只在当次进程首次 apt 安装时执行）
        if not _apt_update_performed:
            subprocess.run(
                [apt_get, "update", "-qq"],
                timeout=60,
                capture_output=True,
                check=False,
            )
            _apt_update_performed = True

        result = subprocess.run(
            [apt_get, "install", "-y", "--no-install-recommends", pkg],
            timeout=120,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            logger.info("auto-install: %s installed successfully", pkg)
        else:
            logger.warning("auto-install: install failed (rc=%d): %s", result.returncode, result.stderr[:500])
    except subprocess.TimeoutExpired:
        logger.warning("auto-install: apt-get install %s timed out", pkg)
    except Exception as exc:
        logger.warning("auto-install: apt-get install %s exception: %s", pkg, exc)

    return shutil.which(interpreter) is not None


# ---------------------------------------------------------------------------
# Per-skill isolated environment helpers
# ---------------------------------------------------------------------------

def _get_skill_venv_python(skill_dir: Path) -> Path:
    """Ensure skill_dir/.venv exists and return its python executable path.

    Uses a process-level cache so the venv is created at most once per skill
    per process lifetime.
    """
    venv_dir = skill_dir / ".venv"
    venv_python = venv_dir / "bin" / "python"
    key = str(skill_dir.resolve())

    with _venv_created_lock:
        if key not in _venv_created_dirs:
            if not venv_dir.exists():
                logger.info("skill-env: creating venv at %s", venv_dir)
                result = subprocess.run(
                    ["python3", "-m", "venv", str(venv_dir)],
                    timeout=60,
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    raise RuntimeError(
                        f"创建 skill venv 失败 ({venv_dir}): {result.stderr[:500]}"
                    )
            _venv_created_dirs.add(key)

    return venv_python


def _scan_and_install_python_deps(script_path: Path, venv_python: Path) -> None:
    """Static-scan a .py script and pip-install any missing third-party imports
    into the per-skill venv before the script is executed.
    """
    import ast
    import sys

    try:
        source = script_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(script_path))
    except SyntaxError:
        return  # 语法错误留给执行时报告

    top_level_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level_names.append(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top_level_names.append(node.module.split(".")[0])

    stdlib_names: frozenset[str] = frozenset(getattr(sys, "stdlib_module_names", set()))

    to_install: list[str] = []
    seen: set[str] = set()
    for name in top_level_names:
        if not name or name.startswith("_") or name in stdlib_names or name in seen:
            continue
        # Only proceed if the name is a safe identifier (AST-sourced, but be explicit)
        if not name.isidentifier():
            continue
        seen.add(name)
        pkg = _IMPORT_TO_PACKAGE.get(name, name)
        # Use importlib.util.find_spec via argv rather than f-string interpolation
        check = subprocess.run(
            [
                str(venv_python),
                "-c",
                "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec(sys.argv[1]) is not None else 1)",
                name,
            ],
            capture_output=True,
            timeout=10,
        )
        if check.returncode != 0:
            to_install.append(pkg)

    if to_install:
        logger.info("skill-env: pip installing into venv: %s", to_install)
        subprocess.run(
            [str(venv_python), "-m", "pip", "install", "--quiet"] + to_install,
            timeout=180,
            capture_output=True,
            text=True,
        )


def _scan_and_install_node_deps(script_path: Path, skill_dir: Path) -> None:
    """Static-scan a .js/.mjs script and npm-install any missing third-party
    modules into the per-skill node_modules before the script is executed.

    策略（按优先级）：
    1. 解析 skill_dir/package.json 的 dependencies / devDependencies。
    2. 递归跟踪本地 require('./xxx.js') / import './xxx.js' 引用（最多 2 层深）。
    3. 正则扫描主脚本及所有被递归发现的子脚本中的第三方 import/require。
    """
    npm_bin = shutil.which("npm")
    if not npm_bin:
        return

    import_patterns = [
        # CommonJS: require('pkg') / require("pkg")
        re.compile(r"""require\s*\(\s*['"]([^'"./][^'"]*)['"]\s*\)"""),
        # ES module: import ... from 'pkg'
        re.compile(r"""from\s+['"]([^'"./][^'"]*)['"]\s*"""),
        # Side-effect import: import 'pkg'
        re.compile(r"""import\s+['"]([^'"./][^'"]*)['"]\s*"""),
        # Dynamic import: import('pkg')
        re.compile(r"""import\s*\(\s*['"]([^'"./][^'"]*)['"]\s*\)"""),
    ]
    local_require_pattern = re.compile(
        r"""require\s*\(\s*['"](\.{1,2}/[^'"]+)['"]\s*\)|"""
        r"""from\s+['"](\.{1,2}/[^'"]+)['"]\s*""",
    )

    def _collect_third_party(source: str) -> list[str]:
        found: list[str] = []
        for pat in import_patterns:
            for m in pat.finditer(source):
                raw = m.group(1)
                if raw.startswith("@"):
                    parts = raw.split("/")
                    name = "/".join(parts[:2]) if len(parts) >= 2 else parts[0]
                else:
                    name = raw.split("/")[0]
                if name and name not in found:
                    found.append(name)
        return found

    def _collect_local_refs(source: str, base: Path, skill_root: Path) -> list[Path]:
        refs: list[Path] = []
        for m in local_require_pattern.finditer(source):
            rel = m.group(1) or m.group(2)
            if not rel:
                continue
            candidate = (base / rel).resolve()
            if not candidate.suffix:
                candidate = candidate.with_suffix(".js")
            # Security: only follow references that stay inside the skill directory
            try:
                candidate.relative_to(skill_root)
            except ValueError:
                continue
            if candidate.is_file():
                refs.append(candidate)
        return refs

    names: list[str] = []
    skill_root = skill_dir.resolve()

    # 1. Parse package.json if present
    pkg_json = skill_root / "package.json"
    if pkg_json.is_file():
        try:
            pkg_data = json.loads(pkg_json.read_text(encoding="utf-8", errors="replace"))
            for section in ("dependencies", "devDependencies"):
                for pkg_name in (pkg_data.get(section) or {}).keys():
                    if pkg_name and pkg_name not in names:
                        names.append(pkg_name)
        except (json.JSONDecodeError, OSError):
            pass

    # 2 + 3. Recursively scan entry script and local requires (up to 2 levels deep)
    visited: set[Path] = set()
    queue: list[tuple[Path, int]] = [(script_path.resolve(), 0)]
    while queue:
        current, depth = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        try:
            source = current.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pkg_name in _collect_third_party(source):
            if pkg_name not in names:
                names.append(pkg_name)
        if depth < 2:
            for child in _collect_local_refs(source, current.parent, skill_root):
                if child not in visited:
                    queue.append((child, depth + 1))

    to_install = [
        n for n in names
        if n not in _NODE_BUILTIN_MODULES
        and not (skill_dir / "node_modules" / n).exists()
    ]

    if to_install:
        logger.info("skill-env: npm installing into %s: %s", skill_dir, to_install)
        subprocess.run(
            [npm_bin, "install", "--prefix", str(skill_dir), "--quiet"] + to_install,
            timeout=180,
            capture_output=True,
            text=True,
            cwd=str(skill_dir),
        )


def _retry_install_python_dep(module_name: str, venv_python: Path) -> bool:
    """Error-driven: install a single missing Python module into the skill venv.

    Returns True if the install command succeeded.
    """
    pkg = _IMPORT_TO_PACKAGE.get(module_name, module_name)
    logger.info("skill-env: error-driven pip install %s (for import %s)", pkg, module_name)
    result = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "--quiet", pkg],
        timeout=180,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _retry_install_node_dep(module_name: str, skill_dir: Path) -> bool:
    """Error-driven: install a single missing Node.js module into skill node_modules.

    Returns True if the install command succeeded.
    """
    npm_bin = shutil.which("npm")
    if not npm_bin:
        return False
    logger.info("skill-env: error-driven npm install %s into %s", module_name, skill_dir)
    result = subprocess.run(
        [npm_bin, "install", "--prefix", str(skill_dir), "--quiet", module_name],
        timeout=180,
        capture_output=True,
        text=True,
        cwd=str(skill_dir),
    )
    return result.returncode == 0


def _friendly_error(exc: Exception) -> str:
    """Convert LLM proxy exceptions to user-facing messages without leaking internals."""
    if isinstance(exc, httpx.ConnectError):
        return "无法连接到 LLM 服务，请确认 Ollama 已启动，且端口可访问"

    if isinstance(exc, httpx.HTTPStatusError):
        return f"LLM 服务返回错误: HTTP {exc.response.status_code}"

    if isinstance(exc, httpx.TimeoutException):
        return "LLM 服务响应超时，请重试"

    return "生成时发生错误，请重试"


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _thought(step: str, label: str, detail: str, data: dict | None = None) -> str:
    """Build a 'thought' SSE event that carries internal decision/execution data.

    step values:
      metadata_decision | body_loaded | child_decision | resource_selection |
      planner_output | action_start | action_result | final_answer
    """
    return _sse({
        "thought": {
            "step": step,
            "label": label,
            "detail": detail,
            "data": data or {},
            "ts": time.time(),
        }
    })


def _expand_arg_env_vars(arg: str, env: dict) -> str:
    """Expand shell-style $VAR / ${VAR} references in a single command argument.

    Uses the supplied *env* dict rather than the live process environment so
    that values injected by the skill runtime (OUTPUT_DIR, INPUT_DIR, …) are
    always honoured even when subprocess uses shell=False.
    """
    if "$" not in arg:
        return arg
    result = arg
    # Replace ${VAR} first — unambiguous because of the braces.
    for var, val in env.items():
        result = result.replace(f"${{{var}}}", val)
    # Replace bare $VAR in decreasing name-length order so that a
    # shorter name that is a prefix of a longer one (e.g. INPUT_DIR
    # vs INPUT_SESSION_DIR) never clobbers the longer match.
    for var in sorted(env, key=len, reverse=True):
        result = result.replace(f"${var}", env[var])
    return result


def _request_messages(request: ChatRequest) -> list[dict]:
    return [{"role": m.role, "content": m.content} for m in request.messages]


def _request_messages_with_files(request: ChatRequest) -> list[dict]:
    """Like _request_messages, but appends a compact file-attachment note to the
    last user message so the LLM sees the files as part of the conversation turn
    rather than only in the system prompt.
    """
    if not request.input_files:
        return _request_messages(request)

    messages = [{"role": m.role, "content": m.content} for m in request.messages]
    # Find the last user message and append a file-attachment note.
    for i in reversed(range(len(messages))):
        if messages[i]["role"] == "user":
            names = ", ".join(
                f.get("filename") or Path(f.get("path", "")).name
                for f in request.input_files
            )
            messages[i] = {
                "role": "user",
                "content": messages[i]["content"] + f"\n\n【已附上传文件：{names}】",
            }
            break
    return messages


def _extract_input_session_dir(input_files: list[dict], execution_root: "Path | None") -> "Path | None":
    """Derive the session-specific input directory from the first uploaded file's path.

    Uploaded files are stored at  inputs/<session_id>/<filename>  relative to the
    skill root.  This helper resolves the absolute  inputs/<session_id>/  path so
    subprocesses can receive it as INPUT_SESSION_DIR, allowing scripts to discover
    all uploaded files without hard-coding the session ID.
    """
    if not input_files or execution_root is None:
        return None
    first_path = input_files[0].get("path", "")
    parts = Path(first_path).parts  # ("inputs", "<session_id>", "<filename>")
    if len(parts) >= 2 and parts[0] == "inputs":
        return execution_root / "inputs" / parts[1]
    return None


def _rewrite_argv_input_paths(
    argv: list[str],
    input_files: list[dict],
    execution_root: "Path | None",
    session_input_dir: "Path | None",
) -> list[str]:
    """Rewrite argv elements that reference uploaded files to absolute paths.

    The LLM may generate file arguments using conventions like:
      - ``uploads/<filename>``
      - a bare ``<filename>`` that matches an uploaded file

    These won't resolve when the subprocess runs with cwd=skill_root or
    cwd=scripts/.  This helper replaces such arguments with the real absolute
    path so the script can open the file.

    When ``uploads/<name>`` is used but ``<name>`` does not match any uploaded
    file exactly (e.g. the LLM copied a placeholder filename like ``data.xlsx``
    from a SKILL.md example), the function falls back to:
      1. The single uploaded file whose extension matches ``<name>``'s extension.
      2. ``session_input_dir/<name>`` so the subprocess CWD issue is at least
         resolved (the script itself will then report a meaningful error if the
         file is truly absent).
    """
    if not input_files or execution_root is None:
        return argv

    # Build a filename → absolute-path map from uploaded file records.
    filename_to_abs: dict[str, str] = {}
    for f in input_files:
        rel = f.get("path", "")
        fname = f.get("filename") or (Path(rel).name if rel else "")
        if rel and fname:
            abs_path = execution_root / rel
            filename_to_abs[fname] = str(abs_path)

    if not filename_to_abs:
        return argv

    # Build an extension → list-of-absolute-paths index for fuzzy fallback.
    # Used when the LLM copies a placeholder filename from SKILL.md (e.g.
    # "uploads/data.xlsx") but the user actually uploaded "report.xlsx".
    ext_to_abs: dict[str, list[str]] = {}
    for fname, abs_p in filename_to_abs.items():
        ext = Path(fname).suffix.lower()
        ext_to_abs.setdefault(ext, []).append(abs_p)

    result: list[str] = []
    for arg in argv:
        rewritten = arg
        # Pattern 1: uploads/<filename>  or  uploads\<filename>
        for prefix in ("uploads/", "uploads\\"):
            if rewritten.startswith(prefix):
                candidate = rewritten[len(prefix):]
                if candidate in filename_to_abs:
                    # Exact filename match — use the known absolute path.
                    rewritten = filename_to_abs[candidate]
                else:
                    # Exact match failed: the LLM may have copied a placeholder
                    # filename from the SKILL.md example.  Fall back to the
                    # only uploaded file whose extension matches the placeholder.
                    placeholder_ext = Path(candidate).suffix.lower()
                    matches = ext_to_abs.get(placeholder_ext, [])
                    if len(matches) == 1:
                        rewritten = matches[0]
                    elif session_input_dir is not None:
                        # Multiple (or zero) extension matches: redirect the
                        # directory portion to the session input dir and keep
                        # the original filename so the script can report a
                        # meaningful "file not found" error if needed.
                        rewritten = str(session_input_dir / candidate)
                break
        # Pattern 2: bare filename that exactly matches an upload (only when the
        # argument doesn't already look like an absolute or relative path).
        if (
            rewritten == arg  # not yet rewritten
            and "/" not in rewritten
            and "\\" not in rewritten
            and rewritten in filename_to_abs
        ):
            rewritten = filename_to_abs[rewritten]
        result.append(rewritten)
    return result


def _last_user_text(request: ChatRequest) -> str:
    for message in reversed(request.messages):
        if message.role == "user":
            return message.content or ""
    return ""


def _has_creation_confirmation(request: ChatRequest) -> bool:
    """Only execute generated file-operation blocks after explicit user confirmation."""
    text = _last_user_text(request).strip()
    return any(keyword in text for keyword in _CONFIRM_KEYWORDS)


def _creator_user_texts(request: ChatRequest) -> list[str]:
    """Return non-empty user utterances in order."""
    return [
        (message.content or "").strip()
        for message in request.messages
        if message.role == "user" and (message.content or "").strip()
    ]


def _creator_has_slot(patterns: tuple[re.Pattern[str], ...], text: str) -> bool:
    """Return True when any pattern indicates the slot is present."""
    return any(pattern.search(text) for pattern in patterns)


def _creator_has_follow_up_round(request: ChatRequest) -> bool:
    """Return True when the user has already answered at least one assistant follow-up."""
    seen_first_user = False
    saw_assistant_follow_up = False

    for message in request.messages:
        content = (message.content or "").strip()
        if not content:
            continue

        if message.role == "assistant":
            if seen_first_user and not any(marker in content for marker in _BLUEPRINT_MARKERS):
                saw_assistant_follow_up = True
            continue

        if message.role != "user":
            continue

        if not seen_first_user:
            seen_first_user = True
            continue

        if saw_assistant_follow_up:
            return True

    return False


def _build_creator_clarifying_question(missing_slot: str) -> str:
    """Return a deterministic single-question follow-up for state A."""
    shared_input_output_question = "好的，我先确认一个关键信息：用户实际会提供什么输入，它最终又应该输出什么结果？最好直接给我一条真实示例。"
    prompts = {
        "purpose": "好的，我先确认一个关键信息：这个 Skill 最核心要解决什么问题？请用一句话说清它最主要的用途。",
        "input": shared_input_output_question,
        "output": shared_input_output_question,
        "scenario": "好的，我先确认一个关键信息：请给我一个最典型的使用场景，最好是一句用户真的会说的话。",
        "resources": "好的，我先确认一个关键信息：这个 Skill 是否需要脚本、参考资料、外部 API、数据库或其他依赖配置？如果都不需要，也请直接说明。",
        "mandatory_follow_up": "好的，我再确认一个关键细节：这个 Skill 还有没有必须遵守的限制、偏好或交付要求？如果没有，也请直接说“没有”。",
    }
    return prompts.get(missing_slot, prompts["input"])


def _is_creator_requirement_collection_complete(
    missing_slots: list[str], has_follow_up_round: bool
) -> bool:
    """Blueprint output is allowed only after all slots are covered and a real follow-up is answered.

    The mandatory follow-up question does not itself complete the gate; the user must answer it so
    creator mode has at least one full clarification round before entering blueprint generation.
    """
    return not missing_slots and has_follow_up_round


def _analyze_creator_requirements(request: ChatRequest) -> CreatorRequirementAnalysis:
    """Best-effort requirement-slot analysis for creator mode.

    当前实现是规则启发式，不依赖额外 LLM 调用。目标不是完美理解需求，
    而是为“先澄清、再出蓝图”提供稳定且可预测的后端闸门。
    """
    user_texts = _creator_user_texts(request)
    combined = "\n".join(user_texts)

    has_purpose = bool(combined)
    has_input = _creator_has_slot(_CREATOR_INPUT_PATTERNS, combined)
    has_output = _creator_has_slot(_CREATOR_OUTPUT_PATTERNS, combined)
    has_scenario = _creator_has_slot(_CREATOR_SCENARIO_PATTERNS, combined)
    has_resources = _creator_has_slot(_CREATOR_RESOURCE_PATTERNS, combined)
    has_follow_up_round = _creator_has_follow_up_round(request)

    collected_slots: list[str] = []
    missing_slots: list[str] = []

    for slot_name, present in (
        ("purpose", has_purpose),
        ("input", has_input),
        ("output", has_output),
        ("scenario", has_scenario),
        ("resources", has_resources),
    ):
        if present:
            collected_slots.append(slot_name)
        else:
            missing_slots.append(slot_name)

    ready_for_blueprint = _is_creator_requirement_collection_complete(
        missing_slots, has_follow_up_round
    )
    if missing_slots:
        next_prompt_key = missing_slots[0]
    elif not has_follow_up_round:
        next_prompt_key = "mandatory_follow_up"
    else:
        next_prompt_key = ""

    return CreatorRequirementAnalysis(
        user_turns=len(user_texts),
        collected_slots=collected_slots,
        missing_slots=missing_slots,
        ready_for_blueprint=ready_for_blueprint,
        next_question=_build_creator_clarifying_question(next_prompt_key),
    )


def _detect_creator_state(request: ChatRequest) -> CreatorStateContext:
    """Detect the current creator state-machine position from conversation history.

    Returns:
        "A"  – requirement collection is not complete yet
        "B"  – requirement slots are complete and the model may output/revise blueprint
        "C"  – user's last message contains an explicit confirmation keyword
               AND a blueprint was already shown (state C: file creation allowed)
    """
    blueprint_shown = any(
        msg.role == "assistant"
        and any(marker in (msg.content or "") for marker in _BLUEPRINT_MARKERS)
        for msg in request.messages
    )
    requirement_analysis = _analyze_creator_requirements(request)

    last_user = _last_user_text(request).strip()
    if blueprint_shown and any(kw in last_user for kw in _CONFIRM_KEYWORDS):
        return CreatorStateContext(
            state="C",
            blueprint_shown=True,
            requirements=requirement_analysis,
        )

    if blueprint_shown or requirement_analysis.ready_for_blueprint:
        return CreatorStateContext(
            state="B",
            blueprint_shown=blueprint_shown,
            requirements=requirement_analysis,
        )

    return CreatorStateContext(
        state="A",
        blueprint_shown=blueprint_shown,
        requirements=requirement_analysis,
    )


def _compose_creator_state_injection(
    state: str,
    *,
    blueprint_shown: bool = False,
    requirement_analysis: CreatorRequirementAnalysis | None = None,
) -> str:
    """Return a system-message string that tells the model its current state.

    Injected as a second system message so it acts as a hard constraint that
    overrides any ambiguity in the model's self-assessment of conversation state.
    """
    blueprint_marker = _BLUEPRINT_MARKERS[0]
    if state == "A":
        if requirement_analysis is None:
            raise RuntimeError(
                "Internal error: requirement_analysis is required when composing state A injection. "
                "This indicates the caller has not provided requirement analysis context. "
                "Ensure _detect_creator_state() runs before _compose_creator_state_injection()."
            )
        missing_desc = "、".join(requirement_analysis.missing_slots) or "无"
        return (
            "【后端状态注入】当前状态：A（需求收集）\n\n"
            f"对话历史中尚未满足蓝图输出条件；当前缺失槽位：{missing_desc}。\n"
            "本轮必须处于状态 A，严格执行以下规则：\n"
            "1. 只允许输出一个问题，询问当前最缺失的需求信息。\n"
            f"2. 禁止输出蓝图（{blueprint_marker}）。\n"
            "3. 禁止输出任何 fenced code block（```）。\n"
            "4. 禁止输出 SKILL.md、scripts/、references/、assets/ 的内容。\n"
            "5. 禁止说'我来帮你创建'、'以下是设计文档'、'下面是实现代码'等。\n"
            "回复格式：好的，我先确认一个关键信息：<只问一个问题>"
        )
    if state == "B":
        if not blueprint_shown:
            return (
                "【后端状态注入】当前状态：B（蓝图输出阶段）\n\n"
                "关键信息已收集完成，且用户至少完成了一轮补充说明。\n"
                "本轮必须只输出完整蓝图，不得输出任何文件内容、代码块、测试命令或创建报告。\n"
                "蓝图结尾必须使用“这是我理解的需求，对吗？”以及 A/B/C 三个确认选项。"
            )
        return (
            "【后端状态注入】当前状态：B（蓝图已展示，等待用户确认）\n\n"
            "对话历史中已包含蓝图，但用户尚未发出确认语。\n"
            "本轮必须处于状态 B，严格执行以下规则：\n"
            "1. 禁止创建任何文件或目录。\n"
            "2. 禁止输出任何 SKILL.md 正文或脚本代码块。\n"
            "3. 如果用户要求修改蓝图，根据意见调整后重新展示完整蓝图，仍然以'这是我理解的需求，对吗？'结尾。\n"
            "4. 等待用户发出确认语后，后端才会解锁文件创建权限。"
        )
    # state == "C"
    return (
        "【后端状态注入】当前状态：C（创建阶段）\n\n"
        "用户已明确发出确认语，系统已进入创建阶段。\n"
        "本轮可以：创建目录、写入文件、输出 SKILL.md 和脚本代码块、执行校验、报告结果。\n"
        "按照蓝图逐步完成所有文件的创建，完成后给出简短报告。"
    )


def _simple_sse_content_response(content: str) -> list[str]:
    """Return a minimal SSE response payload containing one assistant message."""
    return [
        _sse({"status": None}),
        _sse({"content": content}),
        "data: [DONE]\n\n",
    ]


def _strip_markdown_json_fence(text: str) -> str:
    """Remove common markdown code fences around JSON.

    Handles three cases in order:
    1. The whole response is a ```json ... ``` fence (most common for well-behaved models).
    2. The whole response is a bare ``` ... ``` fence.
    3. The JSON fence is embedded inside a longer natural-language response — the model
       added prose before/after the JSON block.  In this case we search for the first
       ```json ... ``` or ``` ... ``` block whose content starts with ``{`` or ``[``.
    4. Last resort: find the first ``{`` or ``[`` that could start a JSON object/array.
    """
    stripped = text.strip()

    # Case 1 & 2: fence at the start of the response.
    if stripped.startswith("```json"):
        stripped = stripped[len("```json"):].strip()
        if stripped.endswith("```"):
            stripped = stripped[:-3].strip()
        return stripped

    if stripped.startswith("```"):
        stripped = stripped[len("```"):].strip()
        if stripped.endswith("```"):
            stripped = stripped[:-3].strip()
        # Only return early if the result looks like JSON.
        if stripped.startswith("{") or stripped.startswith("["):
            return stripped

    # If the text already looks like JSON, return it directly.
    if stripped.startswith("{") or stripped.startswith("["):
        return stripped

    # Case 3: embedded ```json ... ``` block anywhere in the text.
    m = re.search(r"```json\s*([\s\S]*?)\s*```", text)
    if m:
        candidate = m.group(1).strip()
        if candidate.startswith("{") or candidate.startswith("["):
            return candidate

    # Embedded ``` ... ``` block whose content looks like JSON.
    m = re.search(r"```\s*([\s\S]*?)\s*```", text)
    if m:
        candidate = m.group(1).strip()
        if candidate.startswith("{") or candidate.startswith("["):
            return candidate

    # Case 4: bare JSON object/array anywhere in the text.
    m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
    if m:
        return m.group(1).strip()

    return stripped


def _parse_need_body_decision(text: str) -> bool:
    """Parse first-round metadata decision.

    解析失败时默认进入正文阶段，避免模型格式错误导致 Skill 无法执行。
    """
    stripped = _strip_markdown_json_fence(text)

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        logger.warning("metadata decision is not valid JSON: %s", text[:500])
        return True

    need_body = data.get("need_body", True)

    if isinstance(need_body, bool):
        return need_body

    if isinstance(need_body, str):
        return need_body.strip().lower() in {"true", "1", "yes", "y"}

    return bool(need_body)

def _parse_child_skill_decision(
    text: str,
    *,
    valid_child_refs: set[str] | None = None,
) -> dict:
    """Parse child-skill loading decision.

    关键规则：
    - 只有 child_ref 出现在 Child Skills Manifest 的真实 ref 中，才允许 need_child=true。
    - 模型复制示例 ref 或猜测不存在 ref 时，一律降级为 need_child=false。
    """
    valid_child_refs = valid_child_refs or set()
    stripped = _strip_markdown_json_fence(text)

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        logger.warning("child skill decision is not valid JSON: %s", text[:500])
        return {"need_child": False, "child_ref": "", "reason": "JSON 解析失败"}

    if not isinstance(data, dict):
        return {"need_child": False, "child_ref": "", "reason": "输出不是 JSON object"}

    need_child = data.get("need_child", False)

    if isinstance(need_child, str):
        need_child = need_child.strip().lower() in {"true", "1", "yes", "y"}
    else:
        need_child = bool(need_child)

    child_ref = str(data.get("child_ref") or "").strip()
    reason = str(data.get("reason") or "").strip()

    if not need_child:
        return {"need_child": False, "child_ref": "", "reason": reason}

    if not child_ref:
        return {
            "need_child": False,
            "child_ref": "",
            "reason": "need_child=true 但缺少 child_ref",
        }

    if child_ref not in valid_child_refs:
        return {
            "need_child": False,
            "child_ref": "",
            "reason": (
                "模型返回的 child_ref 不在 Child Skills Manifest 中，已忽略："
                + child_ref
            ),
        }

    return {
        "need_child": True,
        "child_ref": child_ref,
        "reason": reason,
    }

def _extract_child_refs_from_metadata_prompt(metadata_prompt: str) -> set[str]:
    """Extract valid child skill refs from Child Skills Manifest.

    只信任 metadata prompt 中真实出现的：
    - ref: `xxx`
    """
    refs: set[str] = set()

    marker = "## Child Skills Manifest"
    index = metadata_prompt.find(marker)
    if index < 0:
        return refs

    section = metadata_prompt[index:]

    # 截到下一个 markdown 分隔符，避免误扫后面的 resource manifest
    next_sep = section.find("\n---\n")
    if next_sep >= 0:
        section = section[:next_sep]

    for match in re.finditer(r"-\s+ref:\s+`([^`]+)`", section):
        ref = match.group(1).strip()
        if ref and ref != "无":
            refs.add(ref)

    return refs

async def _run_metadata_round(
    *,
    metadata_prompt: str,
    request: ChatRequest,
    model: str,
) -> bool:
    """First internal model round.

    这一轮只给模型 metadata，不给 SKILL.md 正文。
    不向前端流式输出，只用于决定是否进入正文阶段。
    """
    messages = [{"role": "system", "content": metadata_prompt}]
    messages.extend(_request_messages_with_files(request))

    decision_text = await complete_chat_once(messages, model)
    return _parse_need_body_decision(decision_text)

def _compose_child_skill_selection_prompt() -> str:
    return (
        "你是 Skill 分层加载运行时的子 Skill 选择器。\n\n"
        "你会看到父 Skill 的 metadata prompt、valid_child_refs 和用户请求。\n"
        "你的任务是根据用户请求判断是否需要加载某一个子 Skill 的完整 SKILL.md 正文。\n\n"
        "重要规则：\n"
        "1. 只能从 valid_child_refs 中选择 child_ref。\n"
        "2. 如果 valid_child_refs 为空，必须 need_child=false。\n"
        "3. Child Skill 必须是包含 SKILL.md 的子目录，不是普通 references/*.md 文件。\n"
        "4. references/*.md、assets/*、scripts/* 都不是子 Skill，不能作为 child_ref 返回。\n"
        "5. 如果用户请求只需要父 Skill 就能完成，need_child=false。\n"
        "6. 如果用户请求明显匹配某个子 Skill 的 description，need_child=true，并返回 valid_child_refs 中的原样 ref。\n"
        "7. 不要猜测不存在的 ref。\n"
        "8. 不要复制示例占位符。\n"
        "9. 只输出严格 JSON，不要 Markdown，不要解释。\n\n"
        "如果需要子 Skill，输出：\n"
        "{\n"
        "  \"need_child\": true,\n"
        "  \"child_ref\": \"<必须是 valid_child_refs 中的一个值>\",\n"
        "  \"reason\": \"简短原因\"\n"
        "}\n\n"
        "如果不需要子 Skill，输出：\n"
        "{\n"
        "  \"need_child\": false,\n"
        "  \"child_ref\": \"\",\n"
        "  \"reason\": \"简短原因\"\n"
        "}\n"
    )

async def _run_child_skill_selection_round(
    *,
    parent_metadata_prompt: str,
    request: ChatRequest,
    model: str,
) -> dict:
    """Decide whether a child Skill body should be loaded.

    这一轮只使用父 Skill metadata prompt 中的 Child Skills Manifest。
    不读取子 Skill 正文。
    """
    valid_child_refs = _extract_child_refs_from_metadata_prompt(parent_metadata_prompt)

    if not valid_child_refs:
        return {
            "need_child": False,
            "child_ref": "",
            "reason": "Child Skills Manifest 中没有可用子 Skill",
        }

    messages = [
        {"role": "system", "content": _compose_child_skill_selection_prompt()},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "valid_child_refs": sorted(valid_child_refs),
                    "parent_metadata_prompt": parent_metadata_prompt,
                    "user_messages": _request_messages_with_files(request),
                },
                ensure_ascii=False,
            ),
        },
    ]

    decision_text = await complete_chat_once(messages, model)
    return _parse_child_skill_decision(
        decision_text,
        valid_child_refs=valid_child_refs,
    )

def _allowed_skill_roots() -> list[Path]:
    """Return directories under which the executor may create or modify files."""
    roots: list[Path] = []

    configured_skills_path = getattr(settings, "skills_path", None)
    if configured_skills_path:
        roots.append(Path(configured_skills_path).expanduser().resolve())

    roots.append((Path.cwd() / ".agents" / "skills").resolve())
    roots.append((Path.home() / ".agents" / "skills").resolve())

    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            deduped.append(root)

    return deduped

def _skill_root_for_name(skill_name: str) -> Path:
    """Resolve an existing sandbox skill root by skill_name."""
    if not skill_name or "/" in skill_name or "\\" in skill_name or ".." in skill_name:
        raise ValueError(f"非法 skill_name: {skill_name}")

    for root in _allowed_skill_roots():
        candidate = (root / skill_name).resolve()
        skill_md = candidate / "SKILL.md"
        if skill_md.exists():
            return candidate

    allowed_text = "、".join(str(root) for root in _allowed_skill_roots())
    raise FileNotFoundError(f"未找到 Skill: {skill_name}；搜索目录: {allowed_text}")

def _resolve_safe_path(raw_path: str, base_dir: Path | None = None) -> Path:
    """Resolve file paths and ensure they stay within allowed directories.

    确保文件路径是相对于 skill 根目录的，而不是宿主目录。
    """
    path = Path(raw_path).expanduser()

    if path.is_absolute():
        return path

    # 如果是相对路径，基于 execution_root 或者 inferred_skill_root 解析路径
    base_dir = base_dir or Path.cwd()
    return base_dir / path


def _is_within_sandbox(entry: Path, sandbox_root: Path) -> bool:
    """Return True only when *entry* resolves to a path inside *sandbox_root*.

    Rejects symlinks that point outside the skill sandbox, preventing a
    malicious skill from exposing files such as /etc/passwd via read_resource.
    """
    try:
        entry.resolve().relative_to(sandbox_root)
        return True
    except ValueError:
        return False

def _looks_like_skill_resource_dir(path: Path) -> bool:
    return path.name in {"scripts", "references", "assets"}


def _infer_skill_root_from_tasks(plan: dict, *, execution_root: Path | None = None) -> Path | None:
    """Infer the active skill root from create_directory/write_file tasks.

    用于 /creator legacy fallback：
    如果模型先创建了 <skill-root>/scripts、references、assets，
    后续相对写入 SKILL.md、scripts/main.py 都应以 <skill-root> 为根。
    """
    candidates: list[Path] = []

    for task in plan.get("tasks", []):
        if not isinstance(task, dict):
            continue

        action = str(task.get("action") or "").strip()
        raw_path = str(task.get("path") or "").strip()
        if not raw_path:
            continue

        try:
            resolved = _resolve_safe_path(raw_path, base_dir=execution_root)
        except Exception:
            continue

        if action == "create_directory":
            if _looks_like_skill_resource_dir(resolved):
                candidates.append(resolved.parent)
            else:
                candidates.append(resolved)

        elif action == "write_file":
            if resolved.name == "SKILL.md":
                candidates.append(resolved.parent)
            elif resolved.parent.name in {"scripts", "references", "assets"}:
                candidates.append(resolved.parent.parent)

    if not candidates:
        return None

    # 优先选择位于 allowed skill roots 下的最深目录
    allowed_roots = _allowed_skill_roots()
    valid: list[Path] = []

    for candidate in candidates:
        for allowed_root in allowed_roots:
            try:
                candidate.resolve().relative_to(allowed_root.resolve())
                valid.append(candidate.resolve())
                break
            except ValueError:
                continue

    if not valid:
        return None

    return sorted(valid, key=lambda p: len(p.parts), reverse=True)[0]


def _resolve_planned_file_path(
    raw_path: str,
    *,
    execution_root: Path | None = None,
    inferred_skill_root: Path | None = None,
) -> Path:
    """Resolve file path for planned write/create actions.

    规则：
    - 绝对路径保持绝对路径；
    - sandbox 有 execution_root 时，相对路径基于 execution_root；
    - creator 推断出 inferred_skill_root 时，Skill 内部相对路径基于 inferred_skill_root；
    - 否则退回原有逻辑。
    """
    path = Path(raw_path).expanduser()

    if path.is_absolute():
        return _resolve_safe_path(raw_path, base_dir=execution_root)

    if inferred_skill_root is not None:
        first = path.parts[0] if path.parts else ""

        # SKILL.md、scripts/main.py、references/xx、assets/xx 都属于当前 skill 根
        if raw_path == "SKILL.md" or first in {"scripts", "references", "assets"}:
            return _resolve_safe_path(raw_path, base_dir=inferred_skill_root)

    return _resolve_safe_path(raw_path, base_dir=execution_root)

def _parse_path_argument(path_expr: str) -> str:
    try:
        parts = shlex.split(path_expr)
    except ValueError as exc:
        raise ValueError(f"路径参数解析失败: {path_expr}") from exc

    if len(parts) != 1:
        raise ValueError(f"只允许一个路径参数: {path_expr}")

    return parts[0]


def _extract_executable_blocks(text: str) -> list[str]:
    """Compatibility helper retained for older flow/tests."""
    return [match.group("code").strip() for match in _EXECUTABLE_FENCE_RE.finditer(text)]


def _normalize_fence_lang(info: str) -> str:
    """Return the first token of a Markdown fence info string."""
    info = (info or "").strip()
    if not info:
        return ""
    return info.split()[0].strip().lower()


def _extract_all_fenced_blocks(text: str, *, context_chars: int = 420) -> list[MarkdownBlock]:
    """Extract fenced code blocks using a line-based parser.

    支持外层 ````markdown 包含内部 ```bash 的情况。
    只有遇到长度 >= opening fence 的同字符 fence，才关闭当前 block。
    """
    blocks: list[MarkdownBlock] = []
    lines = text.splitlines(keepends=True)

    pos = 0
    i = 0
    block_index = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        indent_len = len(line) - len(stripped)

        match = re.match(r"(`{3,}|~{3,})([^\n`]*)\n?$", stripped.rstrip("\n"))
        if not match:
            pos += len(line)
            i += 1
            continue

        fence = match.group(1)
        fence_char = fence[0]
        fence_len = len(fence)
        info = match.group(2).strip()
        start_pos = pos
        code_start_pos = pos + len(line)

        code_lines: list[str] = []
        pos += len(line)
        i += 1

        closed = False
        while i < len(lines):
            close_line = lines[i]
            close_stripped = close_line.lstrip()

            close_match = re.match(
                rf"{re.escape(fence_char)}{{{fence_len},}}\s*$",
                close_stripped.rstrip("\n"),
            )

            if close_match:
                end_pos = pos + len(close_line)
                before = text[max(0, start_pos - context_chars):start_pos].strip()
                after = text[end_pos:min(len(text), end_pos + context_chars)].strip()

                blocks.append(
                    MarkdownBlock(
                        index=block_index,
                        lang=_normalize_fence_lang(info),
                        code="".join(code_lines).rstrip("\n"),
                        before_context=before,
                        after_context=after,
                    )
                )
                block_index += 1

                pos += len(close_line)
                i += 1
                closed = True
                break

            code_lines.append(close_line)
            pos += len(close_line)
            i += 1

        if not closed:
            # 未闭合代码块不执行，避免写入半截文件
            break

    return blocks

def _validate_skill_md(skill_md: Path) -> None:
    if not skill_md.exists():
        raise ValueError("缺少 SKILL.md 文件")

    text = skill_md.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md 缺少 YAML frontmatter")

    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("SKILL.md frontmatter 未正确闭合")

    frontmatter = parts[1]
    name_match = re.search(r"^name:\s*([a-z0-9-]+)\s*$", frontmatter, re.M)
    desc_match = re.search(r"^description:\s*(.+)\s*$", frontmatter, re.M)

    if not name_match:
        raise ValueError("frontmatter 缺少合法的 name 字段，只能使用小写字母、数字和连字符")

    if len(name_match.group(1)) > 64:
        raise ValueError("name 字段超过 64 个字符")

    if not desc_match or not desc_match.group(1).strip():
        raise ValueError("frontmatter 缺少 description 字段")


def _find_created_skill_roots(touched_paths: list[Path]) -> list[Path]:
    roots: set[Path] = set()
    allowed_roots = _allowed_skill_roots()

    for path in touched_paths:
        current = path if path.is_dir() else path.parent
        while current != current.parent:
            if (current / "SKILL.md").exists():
                roots.add(current)
                break
            if any(current.parent == root for root in allowed_roots):
                roots.add(current)
                break
            current = current.parent

    return sorted(roots)


def _blocks_for_planner(blocks: list[MarkdownBlock]) -> list[dict]:
    return [
        {
            "index": block.index,
            "lang": block.lang,
            "code_preview": block.code[:4000],
            "before_context": "\n".join(block.before_context.splitlines()[-8:]),
            "after_context": "\n".join(block.after_context.splitlines()[:4]),
        }
        for block in blocks
    ]


def _planner_model_name(default_model: str) -> str:
    """Select a separate planner model when configured.

    建议在 config 中增加 planner_model 或 action_planner_model。
    推荐部署方式：Ollama/OpenAI-compatible 接口，不建议在当前 FastAPI 进程内本地加载大模型。
    """
    return settings.planner_model or default_model

def _extract_runtime_resource_catalog(body_prompt: str, *, execution_root: "Path | None" = None) -> list[dict]:
    """Extract host-owned resource catalog from Loaded SKILL.md prompt.

    关键原则：
    - 真实 path 只归宿主管理；
    - planner 只能看到 resource_handle；
    - planner 不能自己生成 read_resource.path。

    策略：
    1. 用宽松正则匹配所有 backtick 引用（列表、表格、行内等写法均可识别）。
    2. 若传入 execution_root，从磁盘直接扫 scripts/、references/、assets/ 三个子目录，
       将未被正则发现的文件追加进 catalog（彻底兜底）。
    """
    catalog: list[dict] = []
    seen: set[str] = set()

    # 宽松正则：匹配所有被 backtick 包裹的 references/assets/scripts 路径
    # 覆盖列表（- `scripts/xxx`）、表格单元格、行内引用等写法
    # 可选地捕获紧随其后的「：标题」（兼容旧的列表格式）
    pattern = re.compile(
        r"`(?P<path>(references|assets|scripts)/[^`]+)`(?P<title>：[^\n]+)?",
        re.M,
    )

    def _add_entry(path: str, title: str = "") -> None:
        if path in seen:
            return
        seen.add(path)
        kind = path.split("/", 1)[0]
        if kind == "references":
            allowed_actions = ["read_resource"]
            usage_hint = "参考资料，可在任务需要领域知识、示例、规范时读取。"
        elif kind == "assets":
            allowed_actions = ["read_resource"]
            usage_hint = "模板或配置，可在任务需要固定格式、配置、模板时读取。"
        else:
            allowed_actions = ["run_command"]
            usage_hint = "脚本资源，默认用于执行，不用于读取源码，除非用户明确要求查看脚本内容。"
        catalog.append(
            {
                "resource_handle": f"resource:{len(catalog)}",
                "kind": kind,
                "path": path,
                "title": title,
                "allowed_actions": allowed_actions,
                "usage_hint": usage_hint,
            }
        )

    for match in pattern.finditer(body_prompt):
        title = (match.group("title") or "").lstrip("：").strip()
        _add_entry(match.group("path").strip(), title)

    # 文件系统兜底：扫描磁盘上真实存在的文件，补充正则未捕获的条目
    if execution_root is not None:
        execution_root_resolved = execution_root.resolve()
        # Guard: only scan if execution_root itself is within an allowed root.
        if any(_is_within_sandbox(execution_root_resolved, r.resolve()) for r in _allowed_skill_roots()):
            for subdir in ("scripts", "references", "assets"):
                scan_dir = execution_root_resolved / subdir
                if not scan_dir.is_dir():
                    continue
                for entry in sorted(scan_dir.iterdir()):
                    # Reject symlinks that escape the skill sandbox
                    if not _is_within_sandbox(entry, execution_root_resolved):
                        continue
                    if entry.is_file():
                        _add_entry(f"{subdir}/{entry.name}")

    return catalog


def _resource_catalog_for_planner(catalog: list[dict]) -> list[dict]:
    """Expose resource tree to planner without exposing executable paths for read_resource."""
    return [
        {
            "resource_handle": item["resource_handle"],
            "kind": item["kind"],
            "title": item.get("title", ""),
            "allowed_actions": item.get("allowed_actions", []),
            "usage_hint": item.get("usage_hint", ""),
        }
        for item in catalog
    ]


def _resource_catalog_by_handle(catalog: list[dict]) -> dict[str, dict]:
    return {str(item["resource_handle"]): item for item in catalog}


def _compose_resource_selection_prompt() -> str:
    return (
        "你是 Skill 资源按需加载选择器。\n\n"
        "你会看到 Loaded SKILL.md、resource_catalog 和用户请求。"
        "你的任务是判断当前阶段是否需要读取 references/assets/scripts 中的资源正文。\n\n"
        "重要规则：\n"
        "1. 只能从 resource_catalog 中选择 resource_handle。\n"
        "2. 禁止生成、拼接、改写资源 path。\n"
        "3. references 通常用于方法论、规范、示例，creator 生成 Skill 文件前应优先考虑。\n"
        "4. scripts 在 creator 阶段可以读取源码作为实现参考，但不要执行。\n"
        "5. assets 在需要模板或配置时读取。\n"
        "6. 如果 SKILL.md body 已经足够完成任务，可以不读取资源。\n"
        "7. 最多选择 5 个资源，避免一次加载过多。\n"
        "8. 只输出严格 JSON，不要 Markdown，不要解释。\n\n"
        "输出格式：\n"
        "{\n"
        "  \"need_resources\": true,\n"
        "  \"resource_handles\": [\"resource:0\", \"resource:1\"],\n"
        "  \"reason\": \"简短原因\"\n"
        "}\n\n"
        "如果不需要资源：\n"
        "{\n"
        "  \"need_resources\": false,\n"
        "  \"resource_handles\": [],\n"
        "  \"reason\": \"简短原因\"\n"
        "}\n"
    )


def _parse_resource_selection_decision(
    text: str,
    *,
    resource_catalog: list[dict],
) -> dict:
    stripped = _strip_markdown_json_fence(text)

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        logger.warning("resource selection decision is not valid JSON: %s", text[:500])
        return {"need_resources": False, "resource_handles": [], "reason": "JSON 解析失败"}

    if not isinstance(data, dict):
        return {"need_resources": False, "resource_handles": [], "reason": "输出不是 JSON object"}

    need_resources = data.get("need_resources", False)
    if isinstance(need_resources, str):
        need_resources = need_resources.strip().lower() in {"true", "1", "yes", "y"}
    else:
        need_resources = bool(need_resources)

    resource_by_handle = _resource_catalog_by_handle(resource_catalog)
    raw_handles = data.get("resource_handles", [])

    if not isinstance(raw_handles, list):
        raw_handles = []

    selected: list[str] = []
    for item in raw_handles:
        handle = str(item or "").strip()
        if not handle:
            continue
        if handle not in resource_by_handle:
            continue
        if handle not in selected:
            selected.append(handle)
        if len(selected) >= 5:
            break

    if not need_resources or not selected:
        return {
            "need_resources": False,
            "resource_handles": [],
            "reason": str(data.get("reason") or "").strip(),
        }

    return {
        "need_resources": True,
        "resource_handles": selected,
        "reason": str(data.get("reason") or "").strip(),
    }


async def _run_resource_selection_round(
    *,
    body_prompt: str,
    request: ChatRequest,
    model: str,
    resource_catalog: list[dict],
) -> dict:
    if not resource_catalog:
        return {"need_resources": False, "resource_handles": [], "reason": "无可用资源"}

    messages = [
        {"role": "system", "content": _compose_resource_selection_prompt()},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "loaded_skill_prompt": body_prompt,
                    "resource_catalog": _resource_catalog_for_planner(resource_catalog),
                    "user_messages": _request_messages_with_files(request),
                    "last_user_text": _last_user_text(request),
                },
                ensure_ascii=False,
            ),
        },
    ]

    decision_text = await complete_chat_once(messages, _planner_model_name(model))
    return _parse_resource_selection_decision(
        decision_text,
        resource_catalog=resource_catalog,
    )

def _compose_loaded_resources_prompt(
    *,
    skill_name: str,
    resource_catalog: list[dict],
    selected_handles: list[str],
) -> str:
    resource_by_handle = _resource_catalog_by_handle(resource_catalog)
    sections: list[str] = []

    for handle in selected_handles:
        resource = resource_by_handle.get(handle)
        if not resource:
            continue

        path = resource["path"]
        try:
            observation = read_skill_resource_text(
                skill_name,
                path,
                max_chars=settings.skill_resource_max_chars,
            )
        except Exception as exc:
            sections.append(
                f"### {handle}\n"
                f"- path: `{path}`\n"
                f"- load_error: {exc}\n"
            )
            continue

        content = observation.get("content", "")
        truncated = observation.get("truncated", False)

        sections.append(
            f"### {handle}\n"
            f"- kind: {resource.get('kind')}\n"
            f"- path: `{path}`\n"
            f"- truncated: {truncated}\n\n"
            "```text\n"
            f"{content}\n"
            "```"
        )

    if not sections:
        return ""

    return (
        "\n\n---\n\n"
        "## Loaded On-Demand Resources\n\n"
        "以下资源由宿主根据当前请求按需读取。"
        "这些内容现在可以作为执行当前 Skill 的依据。\n\n"
        + "\n\n".join(sections)
    )

def _strip_runtime_resource_manifest(body_prompt: str) -> str:
    """Remove generated resource manifest section from planner text.

    避免 planner 从 Markdown 资源清单中拼接路径。
    真实资源树通过 resource_catalog 单独传入。
    """
    marker = "## Bundled Resources Manifest"
    index = body_prompt.find(marker)
    if index < 0:
        return body_prompt

    before = body_prompt[:index].rstrip()
    return (
        before
        + "\n\n---\n\n"
        + "## Bundled Resources Manifest\n\n"
        + "资源清单已由宿主以结构化 resource_catalog 单独提供。"
        + "规划 read_resource 时只能使用 resource_handle，不能生成 path。\n"
    )

def _compose_skill_runtime_planner_prompt() -> str:
    return (
        "你是 Skill Agent 运行时动作规划器。\n\n"
        "【重要】你只能输出一个严格的 JSON 对象，绝对不能输出任何自然语言、解释、思考过程或 Markdown 文本。"
        "你的全部输出必须是可直接被 json.loads() 解析的 JSON，不得有任何前缀或后缀。\n\n"
        "你的任务不是回答用户问题，而是根据 Loaded SKILL.md、resource_catalog、available_scripts 和用户请求，"
        "判断当前 Skill 应该直接回答，还是需要宿主执行结构化 action。\n\n"
        "核心原则：\n"
        "1. Loaded SKILL.md 是当前 Skill 的执行规范。\n"
        "2. resource_catalog 是宿主提供的真实资源树。\n"
        "3. available_scripts 是宿主从磁盘实时扫描到的真实脚本文件列表（权威来源）。"
        "available_scripts 中出现的脚本无需查 resource_catalog 即可直接规划 run_command。\n"
        "4. 你不能假设某个脚本存在；只能根据 available_scripts 或 resource_catalog 中真实出现的 scripts 资源规划 run_command。\n"
        "5. 你不能把函数名、伪代码函数、Python 函数、自然语言动作当成系统命令执行。\n"
        "6. 如果当前 Skill 是写作、故事生成、公文生成、报告生成、总结、翻译、润色、分析、咨询等语言生成类任务，"
        "且最终产物是纯文本或 Markdown（不是 .pptx/.xlsx/.docx 等格式文件），"
        "通常应使用 mode=direct_answer，不要规划 run_command。\n"
        "7. 如果 available_scripts 和 resource_catalog 均没有 scripts 资源，默认不得规划 run_command。\n"
        "8. 只有当 Loaded SKILL.md 明确要求运行外部命令，且该命令引用的脚本/资源确实存在于 available_scripts、"
        "resource_catalog 或系统可执行环境中，才允许规划 run_command。\n"
        "9. read_resource 只能使用 resource_handle，禁止输出 path。\n"
        "10. resource_handle 必须来自 resource_catalog。\n"
        "11. 如果任务需要 references/assets 的知识、示例、模板或配置，应优先规划 read_resource。\n"
        "12. 不要假装读取、假装执行、假装写入。\n"
        "13. 只输出严格 JSON，不要 Markdown，不要解释。\n\n"
        "允许的 action：\n"
        "- read_resource：读取 resource_catalog 中的资源，只能传 resource_handle。\n"
        "- run_command：执行一个真实可执行的命令。命令不得是函数名或伪代码。\n"
        "- write_file：写入文件。\n"
        "- create_directory：创建目录。\n"
        "- display / ignore：展示或忽略。\n\n"
        "文件生成任务强制规则（高优先级，覆盖规则 6）：\n"
        "当用户明确请求生成 PPT/PPTX/幻灯片、Excel/XLSX、Word/DOCX、CSV、图表图片、PDF 等可下载格式文件时：\n"
        "  a. 如果 available_scripts 或 resource_catalog 中存在可执行的 scripts 资源（如 build_pptx.js、read_excel.py 等），"
        "必须使用 mode=execute 并规划 run_command；不得使用 direct_answer。\n"
        "  b. 文本模型无法直接生成二进制文件（.pptx/.xlsx/.docx），必须通过执行脚本生成。\n"
        "  c. SKILL.md 中为文件生成任务指定了专用脚本时，stdin 字段应包含完整的输入内容（如幻灯片 JSON 数组）。\n\n"
        "mode 选择规则：\n"
        "- direct_answer：Skill 可由模型直接完成，且产物是纯文本/Markdown（不是格式化文件），例如写故事、公文、总结、翻译、分析。\n"
        "- execute：需要宿主执行 action，例如读取资源、运行脚本、写入文件，或生成 PPT/Excel 等格式文件。\n"
        "- ask_user：缺少必要输入，或 SKILL.md 要求的脚本/资源不存在，无法安全执行。\n"
        "- not_applicable：用户请求与当前 Skill 明显不匹配。\n\n"
        "run_command 约束：\n"
        "1. 不得输出类似 generate_story、process、main、run_task 这样的函数名作为 command。\n"
        "2. 不得凭空生成不在 available_scripts 中的 scripts/main.py、scripts/run.py 等路径。\n"
        "3. 如果 command 引用了 scripts/...，该路径必须能在 available_scripts 或 resource_catalog 中看到。\n"
        "4. 如果 available_scripts 和 resource_catalog 的 scripts 均为空，而任务又可由语言模型直接完成，应使用 direct_answer。\n"
        "5. 如果 Loaded SKILL.md 中只有示例命令，但对应脚本不在 available_scripts 中，应使用 ask_user，并在 errors 中说明脚本不存在。\n"
        "6. command 必须是完整的可执行命令行，包含脚本所需的所有参数，并用用户消息中的实际值替换 Loaded SKILL.md 里的占位符\n"
        "（例如 `<filepath>`、`{file}`、`<input>` 等）；不得在 command 中保留任何占位符或省略必要参数。\n"
        "7. 如果某个必要参数（例如文件路径、用户数据）在用户消息中未提供且无法从上下文推断，"
        "必须使用 ask_user 模式，并在 missing 列表中说明缺少哪些信息；不得用不完整的命令继续 execute。\n\n"
        "输出格式：\n"
        "{\n"
        "  \"mode\": \"execute | direct_answer | ask_user | not_applicable\",\n"
        "  \"actions\": [\n"
        "    {\n"
        "      \"action\": \"read_resource\",\n"
        "      \"resource_handle\": \"resource:0\",\n"
        "      \"reason\": \"需要读取参考资料\"\n"
        "    },\n"
        "    {\n"
        "      \"action\": \"run_command\",\n"
        "      \"command\": \"scripts/process.py $INPUT_SESSION_DIR/data.xlsx --format markdown\",\n"
        "      \"stdin\": \"<可选：需要传给命令的标准输入>\",\n"
        "      \"reason\": \"需要运行真实存在的脚本或工具，命令包含从用户消息中提取的实际参数值\"\n"
        "    }\n"
        "  ],\n"
        "  \"final_instruction\": \"执行完成后优先基于 observation 回答；direct_answer 时按 Loaded SKILL.md 直接回答\",\n"
        "  \"missing\": [],\n"
        "  \"errors\": []\n"
        "}\n"
        "\n"
        "重要：如果用户上传了文件，命令中引用该文件时应使用环境变量路径。\n"
        "- Shell 脚本：`$INPUT_SESSION_DIR/<文件名>` 或 `$INPUT_DIR/<相对路径>`\n"
        "- Python 脚本：`os.environ['INPUT_SESSION_DIR'] + '/<文件名>'` 或 "
        "`os.path.join(os.environ['INPUT_DIR'], '<相对路径>')`\n"
        "不得使用 `uploads/`、`inputs/` 等相对路径，因为执行目录并非上传文件的存储位置。\n"
        "特别注意：Loaded SKILL.md 中的示例命令（例如 `uploads/data.xlsx`）只是占位符格式说明，"
        "其中的文件名（如 `data.xlsx`）并非真实文件名。\n"
        "必须从用户消息（user_messages 中的【已附上传文件：...】）中提取真实文件名，"
        "并以 `$INPUT_SESSION_DIR/<真实文件名>` 形式写入 command，不得保留 SKILL.md 中的示例文件名。\n"
    )

def _normalize_skill_runtime_plan(
    plan: dict,
    *,
    resource_catalog: list[dict] | None = None,
    execution_root: Path | None = None,
) -> dict:
    """Normalize planner JSON into executor-compatible plan.

    关键原则：
    - read_resource 的真实 path 不来自模型，而是由宿主根据 resource_handle 映射得到；
    - run_command 不允许凭空执行函数名或不存在的脚本；
    - command 只做通用可执行性校验，不硬编码 python/node/bash。
    """
    if not isinstance(plan, dict):
        raise ValueError("运行时规划模型输出必须是 JSON object")

    resource_by_handle = _resource_catalog_by_handle(resource_catalog or [])

    mode = str(plan.get("mode") or "").strip()
    if mode not in {"execute", "direct_answer", "ask_user", "not_applicable"}:
        mode = "ask_user"

    actions = plan.get("actions", [])
    errors = plan.get("errors", [])
    missing = plan.get("missing", [])

    if not isinstance(actions, list):
        actions = []

    if not isinstance(errors, list):
        errors = []

    if not isinstance(missing, list):
        missing = []

    normalized_actions: list[dict] = []

    for action_item in actions:
        if not isinstance(action_item, dict):
            continue

        action = str(action_item.get("action") or "").strip()

        if action not in {"run_command", "write_file", "create_directory", "read_resource", "display", "ignore"}:
            errors.append({"error": f"不支持的 action: {action}", "action_item": action_item})
            continue

        if action == "run_command":
            command = str(action_item.get("command") or "").strip()
            if not command:
                errors.append({"error": "run_command 缺少 command", "action_item": action_item})
                continue

            stdin_text = action_item.get("stdin", None)
            if stdin_text is not None:
                stdin_text = str(stdin_text)

            # 运行前预检：不执行，只验证命令形态和 Skill 内资源路径。
            try:
                _prepare_command_argv(command, base_dir=execution_root)
            except Exception as exc:
                errors.append({
                    "error": "run_command 预检失败",
                    "command": command,
                    "detail": str(exc),
                    "hint": (
                        "不要把函数名、伪代码或不存在的脚本当成命令。"
                        "如果当前 Skill 可直接由模型完成，请使用 mode=direct_answer。"
                    ),
                })
                continue

            action_item["command"] = command
            action_item["stdin"] = stdin_text

        elif action == "read_resource":
            resource_handle = str(action_item.get("resource_handle") or "").strip()
            if not resource_handle:
                errors.append({"error": "read_resource 缺少 resource_handle", "action_item": action_item})
                continue

            resource = resource_by_handle.get(resource_handle)
            if not resource:
                errors.append({
                    "error": "read_resource 使用了不存在的 resource_handle",
                    "resource_handle": resource_handle,
                    "available_resource_handles": sorted(resource_by_handle.keys()),
                })
                continue

            allowed_actions = set(resource.get("allowed_actions") or [])
            if "read_resource" not in allowed_actions:
                errors.append({
                    "error": "该资源不允许 read_resource",
                    "resource_handle": resource_handle,
                    "kind": resource.get("kind"),
                    "allowed_actions": sorted(allowed_actions),
                })
                continue

            action_item["resource_handle"] = resource_handle
            action_item["path"] = resource["path"]
            action_item["resource_kind"] = resource["kind"]

        elif action in {"write_file", "create_directory"}:
            path = str(action_item.get("path") or "").strip()
            if not path:
                errors.append({"error": f"{action} 缺少 path", "action_item": action_item})
                continue
            action_item["path"] = path

        if action == "write_file":
            if "content" not in action_item:
                errors.append({"error": "write_file 缺少 content", "action_item": action_item})
                continue
            action_item["content"] = str(action_item.get("content") or "")

        action_item["block_index"] = int(action_item.get("block_index", -1))
        normalized_actions.append(action_item)

    # 如果 planner 要 execute，但所有 action 都被宿主校验拦掉，
    # 不要继续进入 executor，改为 ask_user，让前端看到可解释错误。
    if mode == "execute" and not normalized_actions and errors:
        mode = "ask_user"

    return {
        "mode": mode,
        "tasks": normalized_actions,
        "actions": normalized_actions,
        "missing": missing,
        "errors": errors,
        "final_instruction": str(plan.get("final_instruction") or "").strip(),
    }

async def _run_skill_runtime_planner_round(
    *,
    body_prompt: str,
    request: ChatRequest,
    model: str,
    execution_root: Path | None = None,
) -> dict:
    """Generate an action plan from Loaded SKILL.md and structured host resources.

    对齐反重力式宿主模型：
    - Skill.md 提供流程；
    - resource_catalog 提供资源树；
    - planner 只选择 resource_handle；
    - 真实 path 由宿主解析，不由模型生成。
    """
    resource_catalog = _extract_runtime_resource_catalog(body_prompt, execution_root=execution_root)
    planner_body_prompt = _strip_runtime_resource_manifest(body_prompt)

    # 扫描磁盘上真实存在的脚本文件，注入给 planner 以便直接规划 run_command
    available_scripts: list[str] = []
    if execution_root is not None:
        execution_root_resolved = execution_root.resolve()
        scripts_dir = execution_root_resolved / "scripts"
        if scripts_dir.is_dir() and _is_within_sandbox(scripts_dir, execution_root_resolved):
            available_scripts = sorted(
                "scripts/" + entry.name
                for entry in scripts_dir.iterdir()
                if entry.is_file()
                # Reject symlinks that escape the skill sandbox
                and _is_within_sandbox(entry, execution_root_resolved)
            )

    planner_payload = {
        "loaded_skill_prompt": planner_body_prompt,
        "resource_catalog": _resource_catalog_for_planner(resource_catalog),
        "available_scripts": available_scripts,
        "user_messages": _request_messages_with_files(request),
        "last_user_text": _last_user_text(request),
        "execution_root": str(execution_root) if execution_root else "",
        "runtime_contract": {
            "skill_md_is_markdown": True,
            "skill_md_code_blocks_have_no_action_tag": True,
            "resource_tree_is_structured": True,
            "planner_must_not_generate_resource_paths": True,
            "read_resource_uses_resource_handle_only": True,
            "resource_path_resolution_is_host_owned": True,
            "do_not_depend_on_main_model_markdown_output": True,
            "action_observation_loop": True,
        },
    }

    messages = [
        {"role": "system", "content": _compose_skill_runtime_planner_prompt()},
        {"role": "user", "content": json.dumps(planner_payload, ensure_ascii=False)},
    ]

    planner_model = _planner_model_name(model)
    planner_text = await complete_chat_once(messages, planner_model)

    try:
        stripped = _strip_markdown_json_fence(planner_text)
        raw_plan = json.loads(stripped)
    except json.JSONDecodeError:
        # First attempt failed.  Give the model one more chance with an explicit
        # correction prompt that reinforces the JSON-only requirement.
        logger.warning(
            "Planner returned non-JSON on first attempt, retrying with correction prompt: %s",
            planner_text[:300],
        )
        retry_messages = messages + [
            {"role": "assistant", "content": planner_text},
            {
                "role": "user",
                "content": (
                    "你的上一次回复包含了自然语言或 Markdown，不是合法的 JSON。\n"
                    "请重新输出，只输出一个符合格式要求的 JSON 对象，"
                    "不要任何解释、不要 Markdown、不要代码块标记。\n"
                    "直接输出 { ... }，不要其他内容。"
                ),
            },
        ]
        planner_text = await complete_chat_once(retry_messages, planner_model)
        try:
            stripped = _strip_markdown_json_fence(planner_text)
            raw_plan = json.loads(stripped)
        except json.JSONDecodeError as exc:
            logger.error(
                "Received invalid JSON response from skill runtime planner after retry: %s",
                planner_text,
            )
            raise ValueError(f"运行时规划模型没有返回合法 JSON: {planner_text[:500]}") from exc

    return await asyncio.to_thread(
        functools.partial(
            _normalize_skill_runtime_plan,
            raw_plan,
            resource_catalog=resource_catalog,
            execution_root=execution_root,
        )
    )


def _compose_final_answer_prompt() -> str:
    """Generate final answer from action observations."""
    return (
        "你是 Skill Agent 的最终回答生成器。\n\n"
        "你会收到用户请求、Loaded SKILL.md、运行时 action plan 和 executor observation。\n"
        "你必须基于 observation 回答用户，不要假装执行未发生的动作。\n"
        "如果命令执行成功，优先返回脚本 stdout 中的有效结果。\n"
        "如果命令执行失败，简要说明失败原因和 stderr/stdout 中的关键信息。\n"
        "如果 execution_result 中包含 output_files 列表（非空），必须在回答末尾以 Markdown 链接格式列出每个文件，"
        "格式示例：[下载 presentation.pptx](/api/skills/xxx/files/outputs/presentation.pptx)。\n"
        "不要输出内部 JSON，不要重复完整 SKILL.md，不要编造 observation 之外的执行结果。\n"
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
        "3. write_file 的 path 必须出现在该代码块紧邻前文中，通常应是代码块前最后 1 到 3 行里的“写入文件：<path>”或“保存到：<path>”。\n"
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
        "输出格式：\n"
        "{\n"
        "  \"tasks\": [\n"
        "    {\"block_index\": 0, \"action\": \"create_directory\", \"path\": \"...\", \"reason\": \"...\"},\n"
        "    {\"block_index\": 1, \"action\": \"write_file\", \"path\": \"...\", \"reason\": \"...\"},\n"
        "    {\"block_index\": 2, \"action\": \"run_command\", \"command\": \"...\", \"reason\": \"...\"}\n"
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
                "SKILL.md template",
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
    except json.JSONDecodeError as exc:
        logger.error("Received invalid JSON response from planner: %s", planner_text)
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


def _runtime_script_dir() -> Path:
    """Directory for executor-generated Python scripts converted from heredoc."""
    roots = _allowed_skill_roots()
    if not roots:
        raise ValueError("没有可用的 Skill 写入根目录")

    directory = roots[0] / ".runtime"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _materialize_python_heredoc(command: str) -> list[str] | None:
    """Convert `python - <<'PY' ... PY` into `python <safe-script>.py`.

    目的：兼容模型常输出的多行校验脚本，同时继续使用 shell=False，
    不开放真正 shell 的管道、重定向、变量展开、命令替换等能力。
    """
    match = _PYTHON_HEREDOC_RE.match(command.strip())
    if not match:
        return None

    python_bin = Path(match.group("python")).name
    if python_bin not in {"python", "python3"}:
        raise ValueError(f"只允许运行 python/python3 heredoc 命令: {command}")

    script = match.group("script").rstrip() + "\n"
    digest = hashlib.sha256(script.encode("utf-8")).hexdigest()[:16]
    script_path = _runtime_script_dir() / f"heredoc_{digest}.py"
    script_path.write_text(script, encoding="utf-8")

    resolved = _resolve_safe_path(str(script_path))
    return [python_bin, str(resolved)]

def _extract_skill_local_paths_from_argv(argv: list[str]) -> list[str]:
    """Extract skill-local resource paths mentioned in command argv.

    只识别 scripts/、references/、assets/ 这类 Skill 内资源路径。
    不关心具体语言，不硬编码 python/node/bash。
    """
    result: list[str] = []

    for raw in argv:
        if not raw:
            continue

        candidates = [raw]

        # 支持 --config=assets/config.yaml 这种形式
        if "=" in raw:
            _key, value = raw.split("=", 1)
            if value:
                candidates.append(value)

        for item in candidates:
            item = item.strip()
            if not item or item.startswith("-"):
                continue

            if item.startswith("./"):
                item = item[2:]

            try:
                path = Path(item)
            except Exception:
                continue

            parts = path.parts
            if not parts:
                continue

            if parts[0] in {"scripts", "references", "assets"}:
                normalized = Path(*parts).as_posix()
                if normalized not in result:
                    result.append(normalized)

    return result


def _validate_skill_local_command_paths(
    argv: list[str],
    *,
    base_dir: Path | None,
) -> None:
    """Validate skill-local paths referenced by a command.

    解决：
    - python scripts/main.py 但 scripts/main.py 不存在；
    - bash scripts/run.sh 但脚本不存在；
    - node scripts/index.js 但脚本不存在。

    这是资源存在性校验，不是工具类型白名单。
    """
    if base_dir is None:
        return

    root = base_dir.resolve()

    for rel_path in _extract_skill_local_paths_from_argv(argv):
        rel = Path(rel_path)

        if rel.is_absolute():
            raise ValueError(f"命令引用了非法绝对资源路径: {rel_path}")

        if any(part in {"", ".."} for part in rel.parts):
            raise ValueError(f"命令引用的资源路径越界: {rel_path}")

        resolved = (root / rel).resolve()

        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"命令引用的资源路径越界: {rel_path}") from exc

        if not resolved.exists():
            raise ValueError(f"命令引用的 Skill 资源不存在: {rel_path}")

        if not resolved.is_file():
            raise ValueError(f"命令引用的 Skill 资源不是文件: {rel_path}")


def _prepare_command_argv(
    command: str,
    *,
    base_dir: Path | None = None,
) -> list[str]:
    """Parse and preflight a command before subprocess.run.

    不限制具体执行工具类型；
    只做通用校验：
    - 命令不能为空；
    - 命令必须能被 shlex 解析；
    - argv[0] 必须是 PATH 中的可执行程序，或一个真实存在的路径；
    - command 中引用的 scripts/assets/references 路径必须真实存在。

    额外：对 Python 脚本使用每个 Skill 独立的 venv，执行前静态扫描依赖。
    对 Node.js 脚本执行前扫描并安装缺失的 npm 包。
    """
    argv = _safe_command_argv(command, base_dir=base_dir)

    if not argv:
        raise ValueError("命令为空")

    executable = argv[0]

    # 1. argv[0] 是路径形式：./tool、scripts/run.sh、/usr/bin/env 等
    if "/" in executable or "\\" in executable:
        exe_path = Path(executable).expanduser()

        if not exe_path.is_absolute():
            if base_dir is None:
                exe_path = exe_path.resolve()
            else:
                exe_path = (base_dir / exe_path).resolve()
        else:
            exe_path = exe_path.resolve()

        if not exe_path.exists():
            raise ValueError(f"命令不可执行，文件不存在: {executable}")

        if not exe_path.is_file():
            raise ValueError(f"命令不可执行，目标不是文件: {executable}")

        # 方案 A+B：对已知脚本扩展名（.py/.sh/.js 等）始终注入解释器，
        # 避免 execute bit 判断不一致导致的 PermissionError；
        # 对未知扩展名才依赖 execute bit 直接执行；
        # 若扩展名也无法识别，则给出明确错误提示。
        ext = exe_path.suffix.lower()
        if ext not in _SCRIPT_INTERPRETERS and os.access(exe_path, os.X_OK):
            # 非脚本文件且有执行权限，直接执行
            argv[0] = str(exe_path)
        else:
            interpreter = _SCRIPT_INTERPRETERS.get(ext)
            if interpreter is not None:
                # .ts 特殊处理：直接检查 ts-node 或通过 npx 运行
                if ext == ".ts":
                    if shutil.which("ts-node") is None:
                        _try_auto_install_interpreter("ts-node")
                    if shutil.which("ts-node") is not None:
                        argv = ["ts-node", str(exe_path)] + argv[1:]
                    elif shutil.which("npx") is not None:
                        argv = ["npx", "ts-node", str(exe_path)] + argv[1:]
                    else:
                        raise ValueError(
                            f"无法执行 {executable}：需要 ts-node 或 npx，但它们均不在 PATH 中。"
                        )
                elif ext == ".py" and base_dir is not None:
                    # 使用 Skill 独立 venv 执行 Python 脚本，并预装静态依赖
                    try:
                        venv_python = _get_skill_venv_python(base_dir)
                        _scan_and_install_python_deps(exe_path, venv_python)
                        argv = [str(venv_python), str(exe_path)] + argv[1:]
                    except Exception as venv_exc:
                        logger.warning(
                            "skill-env: venv setup failed, falling back to system python3: %s",
                            venv_exc,
                        )
                        if shutil.which("python3") is None:
                            _try_auto_install_interpreter("python3")
                        if shutil.which("python3") is None:
                            raise ValueError(
                                f"无法执行 {executable}：需要解释器 python3，但它不在 PATH 中。"
                            )
                        argv = ["python3", str(exe_path)] + argv[1:]
                elif ext in {".js", ".mjs", ".cjs"} and base_dir is not None:
                    # 预装 Node.js 依赖到 Skill 独立 node_modules
                    try:
                        _scan_and_install_node_deps(exe_path, base_dir)
                    except Exception as node_exc:
                        logger.warning("skill-env: node dep scan failed: %s", node_exc)
                    if shutil.which("node") is None:
                        _try_auto_install_interpreter("node")
                    if shutil.which("node") is None:
                        raise ValueError(
                            f"无法执行 {executable}：需要解释器 node，但它不在 PATH 中。"
                        )
                    argv = ["node", str(exe_path)] + argv[1:]
                else:
                    if shutil.which(interpreter) is None:
                        # 尝试自动安装后再检查一次
                        _try_auto_install_interpreter(interpreter)
                    if shutil.which(interpreter) is None:
                        raise ValueError(
                            f"无法执行 {executable}：需要解释器 {interpreter}，但它不在 PATH 中。"
                        )
                    argv = [interpreter, str(exe_path)] + argv[1:]
            else:
                raise ValueError(
                    f"命令没有执行权限: {executable}\n"
                    f"文件不可直接执行，且扩展名 '{ext or '(无)'}' 无法自动推断解释器。\n"
                    f"请使用 'node/python3/bash <脚本路径>' 的形式明确指定解释器。"
                )

    # 2. argv[0] 是裸命令：python、node、bash、ffmpeg、convert 等
    # 不做白名单，只检查系统 PATH 中是否存在。
    else:
        exe_name = Path(executable).name
        # 对裸 python/python3 + .py 脚本参数，替换为 Skill 独立 venv python
        if exe_name in {"python", "python3"} and len(argv) >= 2 and base_dir is not None:
            script_arg = argv[1]
            script_path_candidate: Path | None = None
            if not script_arg.startswith("-") and (
                "/" in script_arg or script_arg.endswith(".py")
            ):
                candidate = Path(script_arg)
                if not candidate.is_absolute():
                    candidate = (base_dir / candidate).resolve()
                # Guard: script must reside within the skill directory
                try:
                    candidate.relative_to(base_dir.resolve())
                    if candidate.exists() and candidate.suffix.lower() == ".py":
                        script_path_candidate = candidate
                except ValueError:
                    pass  # path escaped skill dir boundary — skip dep scan
            if script_path_candidate is not None:
                try:
                    venv_python = _get_skill_venv_python(base_dir)
                    _scan_and_install_python_deps(script_path_candidate, venv_python)
                    argv = [str(venv_python)] + argv[1:]
                except Exception as venv_exc:
                    logger.warning(
                        "skill-env: venv setup failed, using system %s: %s",
                        executable,
                        venv_exc,
                    )
                    if shutil.which(executable) is None:
                        _try_auto_install_interpreter(executable)
            else:
                if shutil.which(executable) is None:
                    _try_auto_install_interpreter(executable)
        # 对裸 node/nodejs + .js 脚本参数，预装 Node.js 依赖
        elif exe_name in {"node", "nodejs"} and len(argv) >= 2 and base_dir is not None:
            script_arg = argv[1]
            if not script_arg.startswith("-"):
                candidate = Path(script_arg)
                if not candidate.is_absolute():
                    candidate = (base_dir / candidate).resolve()
                # Guard: script must reside within the skill directory
                try:
                    candidate.relative_to(base_dir.resolve())
                    if candidate.exists() and candidate.suffix.lower() in {".js", ".mjs", ".cjs"}:
                        try:
                            _scan_and_install_node_deps(candidate, base_dir)
                        except Exception as node_exc:
                            logger.warning("skill-env: node dep scan failed: %s", node_exc)
                except ValueError:
                    pass  # path escaped skill dir boundary — skip dep scan
            if shutil.which(executable) is None:
                _try_auto_install_interpreter(executable)
        else:
            if shutil.which(executable) is None:
                # 尝试自动安装后再检查一次
                _try_auto_install_interpreter(executable)

        if shutil.which(executable) is None and not Path(argv[0]).exists():
            raise ValueError(
                f"命令不可执行：{executable} 不在 PATH 中，也不是当前 Skill 内的可执行文件。"
                "如果这是函数名或伪代码，请不要规划 run_command。"
            )

    _validate_skill_local_command_paths(argv, base_dir=base_dir)
    return argv

def _safe_command_argv(command: str, *, base_dir: Path | None = None) -> list[str]:
    """通用命令参数解析器。

    注意：
    - 不限制具体执行工具类型；
    - 不做 python/node/bash 白名单；
    - 真正的可执行性和资源存在性校验由 _prepare_command_argv 完成。
    """
    if not command or not command.strip():
        raise ValueError("命令为空")

    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"命令解析失败: {command}") from exc

    if not argv:
        raise ValueError("命令为空")

    return argv

def _execute_single_task(
    task: dict,
    blocks: "list[MarkdownBlock]",
    request: "ChatRequest",
    *,
    execution_root: "Path | None" = None,
    inferred_skill_root: "Path | None" = None,
    skill_name: str = "",
    session_input_dir: "Path | None" = None,
) -> "tuple[dict, list[Path]]":
    """Execute a single planned action task and return (result_dict, touched_paths).

    This is the per-task workhorse extracted from _execute_planned_actions so
    that callers (including the streaming execute loop in generate()) can run
    tasks one-at-a-time and observe results in real time.

    Returns:
        (result, touched) where *result* is the action result dict and
        *touched* is a (possibly empty) list of Path objects that were
        created or written during this task (used for post-loop validation).
    """
    if not isinstance(task, dict):
        return {}, []

    action = str(task.get("action") or "").strip()
    reason = str(task.get("reason") or "").strip()
    touched: list[Path] = []

    if action in {"display", "ignore"}:
        return {"action": action, "success": True, "reason": reason}, touched

    if action == "read_resource":
        rel_path = str(task.get("path") or "").strip()
        if not rel_path:
            raise ValueError("read_resource 任务缺少 path")
        if not skill_name:
            raise ValueError("read_resource 任务缺少 skill_name，无法确定读取哪个 Skill 的资源")
        observation = read_skill_resource_text(
            skill_name, rel_path, max_chars=settings.skill_resource_max_chars
        )
        return {
            "action": action,
            "path": rel_path,
            "success": True,
            "content": observation.get("content", ""),
            "truncated": observation.get("truncated", False),
            "reason": reason,
        }, touched

    if action == "create_directory":
        raw_path = str(task.get("path") or "").strip()
        if not raw_path:
            raise ValueError("create_directory 任务缺少 path")
        path = _resolve_planned_file_path(
            raw_path,
            execution_root=execution_root,
            inferred_skill_root=inferred_skill_root,
        )
        path.mkdir(parents=True, exist_ok=True)
        touched.append(path)
        return {"action": action, "path": str(path), "success": True, "reason": reason}, touched

    if action == "write_file":
        raw_path = str(task.get("path") or "").strip()
        if not raw_path:
            raise ValueError("write_file 任务缺少 path")
        content = task.get("content", None)
        if content is None:
            block_index = int(task.get("block_index", -1))
            if 0 <= block_index < len(blocks):
                content = blocks[block_index].code
            else:
                raise ValueError("write_file 任务缺少 content，且没有合法 block_index")
        path = _resolve_planned_file_path(
            raw_path,
            execution_root=execution_root,
            inferred_skill_root=inferred_skill_root,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")
        touched.append(path)
        return {
            "action": action,
            "path": str(path),
            "success": True,
            "bytes": len(str(content).encode("utf-8")),
            "reason": reason,
        }, touched

    if action == "run_command":
        command = str(task.get("command") or "").strip()
        if not command:
            raise ValueError("run_command 任务缺少 command")

        stdin_text = task.get("stdin", None)
        if stdin_text is not None:
            stdin_text = str(stdin_text)

        cwd = execution_root or inferred_skill_root

        # Per-task snapshot taken *before* execution to detect new output files.
        pre_snapshot: set[str] = _snapshot_dir_files(cwd) if cwd else set()

        materialized = _materialize_python_heredoc(command)
        if materialized is not None:
            argv = materialized
            argv = _prepare_command_argv(
                " ".join(shlex.quote(part) for part in argv), base_dir=cwd
            )
        else:
            argv = _prepare_command_argv(command, base_dir=cwd)

        argv = _rewrite_argv_input_paths(
            argv,
            getattr(request, "input_files", []) or [],
            cwd,
            session_input_dir,
        )

        _run_cmd_extra_env: dict[str, str] = {
            "OUTPUT_DIR": str(cwd / "outputs") if cwd else "",
            "INPUT_DIR": str(cwd / "inputs") if cwd else "",
        }
        if session_input_dir is not None:
            _run_cmd_extra_env["INPUT_SESSION_DIR"] = str(session_input_dir)

        _effective_env = {**os.environ, **_run_cmd_extra_env}
        argv = [_expand_arg_env_vars(arg, _effective_env) for arg in argv]

        # Error-driven retry: up to _MAX_DEP_RETRY times for missing deps.
        completed = None
        for _retry in range(_MAX_DEP_RETRY + 1):
            try:
                completed = subprocess.run(
                    argv,
                    shell=False,
                    input=stdin_text,
                    capture_output=True,
                    text=True,
                    timeout=settings.skill_command_timeout,
                    cwd=str(cwd) if cwd else None,
                    env={**os.environ, **_run_cmd_extra_env},
                )
            except FileNotFoundError as exc:
                raise ValueError(
                    "命令不可执行: " + command + "\n原因: " + str(exc)
                ) from exc
            except PermissionError as exc:
                raise ValueError(
                    "命令没有执行权限: " + command + "\n原因: " + str(exc)
                ) from exc

            if completed.returncode == 0 or _retry == _MAX_DEP_RETRY:
                break

            stderr = completed.stderr or ""
            retried = False

            py_missing = re.search(
                r"ModuleNotFoundError: No module named '([^']+)'", stderr
            )
            if py_missing and cwd is not None:
                module_name = py_missing.group(1).split(".")[0]
                try:
                    venv_python = _get_skill_venv_python(cwd)
                    if _retry_install_python_dep(module_name, venv_python):
                        retried = True
                except Exception as dep_exc:
                    logger.warning(
                        "skill-env: error-driven py dep install failed: %s", dep_exc
                    )

            node_missing = re.search(r"Cannot find module '([^']+)'", stderr)
            if node_missing and cwd is not None:
                raw_mod = node_missing.group(1)
                if raw_mod.startswith("@"):
                    parts = raw_mod.split("/")
                    module_name = "/".join(parts[:2]) if len(parts) >= 2 else parts[0]
                else:
                    module_name = raw_mod.split("/")[0]
                if module_name not in _NODE_BUILTIN_MODULES:
                    if _retry_install_node_dep(module_name, cwd):
                        retried = True

            if not retried and cwd is not None:
                chinese_missing = re.search(
                    r"缺少依赖[:：]\s*([^\n]+)",
                    (completed.stdout or "") + "\n" + stderr,
                )
                if chinese_missing:
                    raw_deps = chinese_missing.group(1)
                    pkg_list = [
                        p.strip()
                        for p in re.split(r"[,，、;；]\s*", raw_deps)
                        if p.strip()
                    ]
                    for dep in pkg_list:
                        if dep in _NODE_BUILTIN_MODULES:
                            continue
                        if (
                            dep.endswith(".js")
                            or (cwd / "node_modules").is_dir()
                            or shutil.which("node")
                        ):
                            if _retry_install_node_dep(dep, cwd):
                                retried = True
                        else:
                            try:
                                venv_python = _get_skill_venv_python(cwd)
                                if _retry_install_python_dep(dep, venv_python):
                                    retried = True
                            except Exception as dep_exc:
                                logger.warning(
                                    "skill-env: chinese dep install failed: %s", dep_exc
                                )

            if not retried:
                break

        assert completed is not None  # noqa: S101 — loop always runs at least once (range >= 1)
        success = completed.returncode == 0

        result: dict = {
            "action": action,
            "command": command,
            "stdin_used": stdin_text is not None,
            "success": success,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "reason": reason,
        }

        # Detect newly created files and attach download metadata.
        effective_skill_name = skill_name or (cwd.name if cwd else "")
        if success and cwd and effective_skill_name:
            post_snapshot = _snapshot_dir_files(cwd)
            new_files = sorted(post_snapshot - pre_snapshot)
            if new_files:
                result["output_files"] = [
                    {
                        "path": f,
                        "url": f"/api/skills/{effective_skill_name}/files/{f}",
                    }
                    for f in new_files
                ]

        return result, touched

    raise ValueError(f"不支持的规划动作: {action}")


def _execute_planned_actions(
    plan: dict,
    blocks: list[MarkdownBlock],
    request: ChatRequest,
    *,
    require_confirmation: bool = True,
    execution_root: Path | None = None,
    skill_name: str = "",
) -> dict:
    """执行结构化 action plan，并返回 executor observation。"""
    if require_confirmation and not _has_creation_confirmation(request):
        return {
            "executed": False,
            "reason": "未检测到用户明确确认开始创建，因此不会执行规划任务。",
            "plan": plan,
            "results": [],
            "logs": [],
        }

    inferred_skill_root = _infer_skill_root_from_tasks(
        plan,
        execution_root=execution_root,
    )

    # Pre-compute session input dir once (used for all run_command tasks).
    cwd_for_session = execution_root or inferred_skill_root
    session_input_dir = _extract_input_session_dir(
        getattr(request, "input_files", []) or [], cwd_for_session
    )

    touched: list[Path] = []
    results: list[dict] = []
    logs: list[str] = []

    for task in plan.get("tasks", []):
        if not isinstance(task, dict):
            continue

        action = str(task.get("action") or "").strip()

        result, task_touched = _execute_single_task(
            task,
            blocks,
            request,
            execution_root=execution_root,
            inferred_skill_root=inferred_skill_root,
            skill_name=skill_name,
            session_input_dir=session_input_dir,
        )

        touched.extend(task_touched)
        results.append(result)

        # Build logs from the result dict.
        if action == "read_resource":
            logs.append(f"读取资源成功: {result.get('path')}")
        elif action == "create_directory":
            logs.append(f"创建目录: {result.get('path')}")
        elif action == "write_file":
            logs.append(f"写入文件: {result.get('path')}")
        elif action == "run_command":
            command = str(task.get("command") or "").strip()
            stdin_used = result.get("stdin_used", False)
            if result.get("output_files"):
                logs.append(
                    "新生成文件: " + ", ".join(f["path"] for f in result["output_files"])
                )
            if not result.get("success", True):
                logs.append(
                    f"执行命令失败: {command}\n"
                    f"returncode={result.get('returncode')}\n"
                    f"stdin_used={stdin_used}\n"
                    f"stderr: {(result.get('stderr') or '').strip()}\n"
                    f"stdout: {(result.get('stdout') or '').strip()}"
                )
            else:
                logs.append(
                    f"执行命令成功: {command}\n"
                    f"stdin_used={stdin_used}\n"
                    f"输出: {(result.get('stdout') or '').strip()}"
                )

    validation_logs: list[str] = []

    for root in _find_created_skill_roots(touched):
        skill_md = root / "SKILL.md"
        if skill_md.exists():
            _validate_skill_md(skill_md)
            validation_logs.append(f"校验通过: {skill_md}")

    logs.extend(validation_logs)

    # 汇总所有 run_command 任务产生的新文件
    all_output_files: list[dict] = []
    for r in results:
        all_output_files.extend(r.get("output_files") or [])

    return {
        "executed": bool(results or touched),
        "reason": "已根据结构化 action plan 执行任务。" if (results or touched) else "规划中没有需要执行的任务。",
        "plan": plan,
        "results": results,
        "logs": logs,
        "output_files": all_output_files,
    }

# 兼容保留：旧的 bash-block 执行器。不再作为主路径使用。
def _execute_restricted_bash_block(code: str) -> dict:
    """Execute a restricted subset of generated bash.

    Deprecated: 新流程使用 main output -> block planner -> planned actions。
    保留该函数是为了兼容已有测试或外部调用。
    """
    blocks = [MarkdownBlock(index=0, lang="bash", code=code, before_context="执行命令：", after_context="")]
    plan = {
        "tasks": [{"block_index": 0, "action": "run_command", "command": code.strip(), "reason": "兼容旧执行路径"}],
        "errors": [],
    }

    class _ConfirmedRequest:
        messages = [Message(role="user", content="对，开始做吧")]
        model = None

    result = _execute_planned_actions(
        plan,
        blocks,
        _ConfirmedRequest(),
        require_confirmation=True,
        execution_root=None,
    )

    return {
        "success": result.get("executed", False),
        "touched": [],
        "logs": result.get("logs", []),
    }


def _format_execution_report(result: dict) -> str:
    if not result.get("executed"):
        reason = result.get("reason", "未知原因")
        errors = result.get("plan", {}).get("errors", []) if isinstance(result.get("plan"), dict) else []
        if errors:
            rendered_errors = "\n".join(f"- {json.dumps(item, ensure_ascii=False)}" for item in errors)
            return f"\n\n⚠️ 后台未执行规划任务：{reason}\n规划提示：\n{rendered_errors}"
        return f"\n\n⚠️ 后台未执行规划任务：{reason}"

    logs = result.get("logs") or []

    if not logs:
        for item in result.get("results", []):
            action = item.get("action")
            if action == "read_resource":
                logs.append(f"读取资源: {item.get('path')}")
            elif action == "write_file":
                logs.append(f"写入文件: {item.get('path')}")
            elif action == "run_command":
                logs.append(f"执行命令成功: {item.get('command')}")
            elif action == "create_directory":
                logs.append(f"创建目录: {item.get('path')}")

    if not logs:
        return "\n\n✅ 后台已执行规划任务。"

    rendered = "\n".join(f"- {line}" for line in logs)
    return f"\n\n✅ 后台已执行规划任务：\n{rendered}"


async def _plan_and_execute_generated_output(
    *,
    assistant_text: str,
    request: ChatRequest,
    model: str,
    require_confirmation: bool = True,
    execution_root: Path | None = None,
    skill_name: str = "",
) -> dict:
    """Legacy fallback: plan and execute actions from main model Markdown output.

    新主路径不再依赖这个函数。
    仅当 runtime planner 判断 direct_answer，或者旧 Skill 仍要求通过主模型 Markdown 输出动作时，才作为兜底。
    """
    blocks = _extract_all_fenced_blocks(assistant_text)

    if not blocks:
        return {
            "executed": False,
            "reason": "主模型回复中未检测到 fenced code block。",
            "plan": {"tasks": [], "errors": []},
            "results": [],
        }

    planner_model = _planner_model_name(model)

    plan = await _run_block_planner_round(
        assistant_text=assistant_text,
        blocks=blocks,
        request=request,
        model=planner_model,
    )

    if plan.get("errors") and not plan.get("tasks"):
        return {
            "executed": False,
            "reason": "规划模型未生成可执行任务。",
            "plan": plan,
            "results": [],
        }

    return await asyncio.to_thread(
        functools.partial(
            _execute_planned_actions,
            plan,
            blocks,
            request,
            require_confirmation=require_confirmation,
            execution_root=execution_root,
            skill_name=skill_name,
        )
    )


def _make_stream(skill_context: dict, request: ChatRequest):
    """Staged Skill execution with creator-safe action planning.

    关键逻辑：
    - /creator：用户未明确确认前，只让主模型按 Creator SKILL.md 做需求收集；
      不运行 runtime planner，不执行动作。
    - /creator：用户确认后，允许主模型生成文件块，再由 block planner/executor 写入。
    - /sandbox/{skill_name}：可以使用 runtime planner 执行具体 Skill。
    """
    model = request.model or settings.default_model
    _MAX_CMD_DISPLAY_LENGTH = 60
    force_body = bool(skill_context.get("force_body", False))
    enable_action_execution = bool(skill_context.get("enable_action_execution", False))
    require_action_confirmation = bool(skill_context.get("require_action_confirmation", True))
    strict_skill_execution = bool(skill_context.get("strict_skill_execution", False))
    strict_creator_generation = bool(skill_context.get("strict_creator_generation", False))
    execution_root = skill_context.get("execution_root")
    child_body_loader = skill_context.get("child_body_loader")
    parent_skill_name = skill_context.get("skill_name", "")
    disable_runtime_planner = bool(skill_context.get("disable_runtime_planner", False))
    enable_resource_preload = bool(skill_context.get("enable_resource_preload", False))

    skip_runtime_planner_before_confirmation = bool(
        skill_context.get("skip_runtime_planner_before_confirmation", False)
    )
    use_frontend_driven_creation = bool(
        skill_context.get("use_frontend_driven_creation", False)
    )

    if execution_root is not None:
        execution_root = Path(execution_root).resolve()
        # Verify the resolved path is within an allowed skill root so that
        # a crafted skill_context cannot steer execution outside the sandbox.
        allowed_roots = _allowed_skill_roots()
        if not any(_is_within_sandbox(execution_root, r.resolve()) for r in allowed_roots):
            raise ValueError(
                f"execution_root '{execution_root}' is outside all allowed skill roots."
            )

    async def generate():
        try:
            if force_body:
                need_body = True
                logger.debug("force_body=True, skip metadata decision and load SKILL.md body directly")
            else:
                yield _sse({"status": {"phase": "analyzing", "message": "分析请求匹配度…"}})
                need_body = await _run_metadata_round(
                    metadata_prompt=skill_context["metadata_prompt"],
                    request=request,
                    model=model,
                )
                yield _thought(
                    "metadata_decision",
                    "分析匹配度",
                    f"{'需要加载正文' if need_body else '请求与 Skill 不匹配，跳过正文'}",
                    {
                        "need_body": need_body,
                        "metadata_chars": len(skill_context.get("metadata_prompt", "")),
                    },
                )

            if not need_body:
                yield _sse({"status": None})
                fallback_messages = [
                    {
                        "role": "system",
                        "content": (
                            "当前用户请求与已选 Skill 及其子 Skill 的 metadata 不匹配。"
                            "请简短说明该 Skill 不适用，并提示用户重新描述需求。"
                        ),
                    }
                ]
                fallback_messages.extend(_request_messages_with_files(request))

                async for chunk in stream_chat(fallback_messages, model):
                    yield _sse({"content": chunk})

                yield "data: [DONE]\n\n"
                return

            # Detect creator state before loading the creator body so requirement collection
            # can be enforced as a backend gate instead of relying on prompt following.
            creator_state: str = "A"
            creator_state_ctx: CreatorStateContext | None = None
            if skip_runtime_planner_before_confirmation:
                creator_state_ctx = _detect_creator_state(request)
                creator_state = creator_state_ctx.state
                yield _thought(
                    "creator_state",
                    "创建者状态",
                    f"当前状态：{creator_state}",
                    {
                        "state": creator_state,
                        "blueprint_shown": creator_state_ctx.blueprint_shown,
                        "user_turns": creator_state_ctx.requirements.user_turns,
                        "collected_slots": creator_state_ctx.requirements.collected_slots,
                        "missing_slots": creator_state_ctx.requirements.missing_slots,
                    },
                )

                if creator_state == "A":
                    for event in _simple_sse_content_response(
                        creator_state_ctx.requirements.next_question
                    ):
                        yield event
                    return

            yield _sse({"status": {"phase": "loading", "message": "加载 Skill 正文…"}})
            body_prompt = skill_context["body_loader"]()
            yield _thought(
                "body_loaded",
                "加载 SKILL.md",
                f"正文已加载，共 {len(body_prompt)} 字符",
                {
                    "body_chars": len(body_prompt),
                    "skill_name": parent_skill_name,
                },
            )

            if child_body_loader:
                yield _sse({"status": {"phase": "loading_child", "message": "检查子 Skill…"}})
                child_decision = await _run_child_skill_selection_round(
                    parent_metadata_prompt=skill_context["metadata_prompt"],
                    request=request,
                    model=model,
                )
                yield _thought(
                    "child_decision",
                    "子 Skill 检查",
                    (
                        f"加载子 Skill：{child_decision.get('child_ref')}"
                        if child_decision.get("need_child")
                        else f"无需子 Skill：{child_decision.get('reason', '')}"
                    ),
                    {
                        "need_child": child_decision.get("need_child"),
                        "child_ref": child_decision.get("child_ref", ""),
                        "reason": child_decision.get("reason", ""),
                    },
                )

                if child_decision.get("need_child"):
                    child_ref = child_decision.get("child_ref", "")
                    yield _sse({"status": {"phase": "loading_child", "message": f"加载子 Skill：{child_ref}…"}})
                    try:
                        child_body_prompt = child_body_loader(child_ref)
                        body_prompt = (
                            f"{body_prompt}\n\n"
                            "---\n\n"
                            "## Loaded Child Skill Body\n\n"
                            f"父 Skill 已根据用户请求按需加载子 Skill：`{child_ref}`。\n"
                            "下面是该子 Skill 的完整执行正文。\n\n"
                            f"{child_body_prompt}"
                        )
                    except Exception as exc:
                        logger.warning(
                            "failed to load child skill body parent=%s child_ref=%s error=%s",
                            parent_skill_name,
                            child_ref,
                            exc,
                        )
                        body_prompt = (
                            f"{body_prompt}\n\n"
                            "---\n\n"
                            "## Child Skill Load Warning\n\n"
                            f"运行时尝试加载子 Skill `{child_ref}`，但加载失败：{exc}\n"
                            "请不要假装已经读取该子 Skill 正文。"
                        )

            # Skip resource preload entirely in state A (requirement-collection phase).
            # In state A the model only needs to ask one clarifying question — loading
            # references via an extra LLM call is wasteful and can produce spurious
            # "not valid JSON" warnings when the model ignores the JSON-only prompt.
            if enable_resource_preload and creator_state != "A":
                resource_catalog = _extract_runtime_resource_catalog(body_prompt)
                if resource_catalog:
                    yield _sse({"status": {"phase": "loading_resources", "message": "按需加载资源…"}})
                resource_decision = await _run_resource_selection_round(
                    body_prompt=body_prompt,
                    request=request,
                    model=model,
                    resource_catalog=resource_catalog,
                )
                yield _thought(
                    "resource_selection",
                    "资源选择",
                    (
                        f"加载 {len(resource_decision.get('resource_handles', []))} 个资源：{', '.join(resource_decision.get('resource_handles', []))}"
                        if resource_decision.get("need_resources")
                        else f"无需加载额外资源：{resource_decision.get('reason', '')}"
                    ),
                    {
                        "need_resources": resource_decision.get("need_resources"),
                        "resource_handles": resource_decision.get("resource_handles", []),
                        "catalog_size": len(resource_catalog),
                        "reason": resource_decision.get("reason", ""),
                    },
                )

                if resource_decision.get("need_resources"):
                    selected = resource_decision.get("resource_handles") or []
                    yield _sse({"status": {"phase": "loading_resources", "message": f"加载 {len(selected)} 个资源…"}})
                    loaded_resources_prompt = _compose_loaded_resources_prompt(
                        skill_name=parent_skill_name,
                        resource_catalog=resource_catalog,
                        selected_handles=selected,
                    )

                    if loaded_resources_prompt:
                        body_prompt = body_prompt + loaded_resources_prompt

            # Append uploaded input-file context to the body prompt so the LLM
            # knows which files are available. For small text files the content is
            # embedded directly so the LLM can reason about the data without running
            # a script first. Binary or large files are described by path only.
            if getattr(request, "input_files", None):
                _TEXT_CONTENT_SUFFIXES = frozenset({
                    ".txt", ".md", ".csv", ".tsv", ".json", ".jsonl",
                    ".yaml", ".yml", ".xml", ".html", ".htm", ".log",
                })
                _MAX_INLINE_BYTES = 100 * 1024  # 100 KB

                file_sections: list[str] = []
                for f in request.input_files:
                    rel_path = f.get("path", "")
                    filename = f.get("filename", rel_path.split("/")[-1] if rel_path else "")
                    suffix = Path(filename).suffix.lower() if filename else ""

                    # Try to read text content for embedding
                    content_block = ""
                    if rel_path and parent_skill_name and suffix in _TEXT_CONTENT_SUFFIXES:
                        try:
                            abs_path = (settings.skills_path / parent_skill_name / rel_path).resolve()
                            # Ensure path stays inside the skill directory
                            skill_dir_check = (settings.skills_path / parent_skill_name).resolve()
                            abs_path.relative_to(skill_dir_check)
                            if abs_path.is_file():
                                raw = abs_path.read_bytes()
                                if len(raw) <= _MAX_INLINE_BYTES:
                                    text = raw.decode("utf-8", errors="replace")
                                    # Choose a fence that doesn't appear in the content.
                                    # Prefer ``` but fall back to a tilde fence when the
                                    # file itself contains triple-backtick sequences.
                                    if "```" not in text:
                                        fence, content_text = "```", text
                                    else:
                                        fence = "~~~~"
                                        content_text = text.replace("~~~~", "~ ~ ~ ~")
                                    content_block = (
                                        f"\n\n  文件内容如下：\n\n  {fence}\n{content_text}\n  {fence}"
                                    )
                        except Exception:
                            pass  # fall back to path-only if read fails

                    if content_block:
                        file_sections.append(
                            f"- `{rel_path}`（文件名：`{filename}`）{content_block}"
                        )
                    else:
                        # Strip the leading "inputs/" component so the script only needs
                        # os.path.join(INPUT_DIR, remaining) — INPUT_DIR points to inputs/.
                        try:
                            _rel_path_obj = Path(rel_path)
                            # Use parts[0] to avoid Windows backslash ambiguity.
                            if _rel_path_obj.parts and _rel_path_obj.parts[0] == "inputs":
                                rel_to_input_dir = Path(*_rel_path_obj.parts[1:]).as_posix()
                            else:
                                rel_to_input_dir = rel_path
                        except (ValueError, IndexError):
                            rel_to_input_dir = rel_path
                        file_sections.append(
                            f"- `{rel_path}`（文件名：`{filename}`，"
                            f"脚本可通过 `os.path.join(os.environ['INPUT_DIR'], '{rel_to_input_dir}')` 读取；"
                            "或直接用 `os.environ['INPUT_SESSION_DIR']` 目录（该目录下包含本次会话所有上传文件）"
                            "）"
                        )

                if file_sections:
                    sections_text = "\n".join(file_sections)
                    body_prompt = (
                        body_prompt
                        + "\n\n---\n\n"
                        "## 当前对话已上传文件\n\n"
                        "用户在本次对话中上传了以下文件，你必须以这些文件为输入进行分析或处理。\n"
                        "- 对于文本/数据文件，内容已直接展示在下方，请直接阅读并回答。\n"
                        "- 需要执行计算、统计、转换等操作时，可生成 Python 脚本并运行，"
                        "脚本中使用 `os.environ['INPUT_SESSION_DIR']` 获取上传文件目录，"
                        "使用 `os.environ['OUTPUT_DIR']` 输出结果文件。\n\n"
                        f"{sections_text}\n"
                    )

            should_skip_runtime_planner = (
                skip_runtime_planner_before_confirmation
                and require_action_confirmation
                and not _has_creation_confirmation(request)
            )

            if enable_action_execution and not should_skip_runtime_planner and not disable_runtime_planner:
                try:
                    yield _sse({"status": {"phase": "planning", "message": "规划执行方案…"}})
                    runtime_plan = await _run_skill_runtime_planner_round(
                        body_prompt=body_prompt,
                        request=request,
                        model=model,
                        execution_root=execution_root,
                    )

                    mode = runtime_plan.get("mode")
                    tasks = runtime_plan.get("tasks") or []

                    # Emit planner_output thought with safe task summaries (no SKILL.md content).
                    yield _thought(
                        "planner_output",
                        "规划结果",
                        f"模式：{mode}，共 {len(tasks)} 个动作",
                        {
                            "mode": mode,
                            "task_count": len(tasks),
                            "tasks": [
                                {
                                    "action": t.get("action"),
                                    "command": (str(t.get("command") or ""))[:120] or None,
                                    "path": t.get("path") or t.get("resource_handle") or None,
                                    "reason": str(t.get("reason") or "")[:200],
                                }
                                for t in tasks
                            ],
                            "errors": runtime_plan.get("errors") or [],
                            "missing": runtime_plan.get("missing") or [],
                        },
                    )

                    if mode == "execute" and tasks:
                        # Set up shared execution context for the per-task loop.
                        _exec_inferred_root = _infer_skill_root_from_tasks(
                            runtime_plan, execution_root=execution_root
                        )
                        _exec_cwd = execution_root or _exec_inferred_root
                        _exec_session_dir = _extract_input_session_dir(
                            getattr(request, "input_files", []) or [], _exec_cwd
                        )

                        _exec_all_results: list[dict] = []
                        _exec_all_touched: list[Path] = []

                        # Execute tasks one at a time so the frontend receives
                        # real-time thought events after each task completes.
                        for task in tasks:
                            task_action = str(task.get("action") or "").strip()

                            # Announce what is about to happen.
                            if task_action == "run_command":
                                cmd = str(task.get("command") or "")
                                short_cmd = cmd[:_MAX_CMD_DISPLAY_LENGTH] + (
                                    "…" if len(cmd) > _MAX_CMD_DISPLAY_LENGTH else ""
                                )
                                yield _sse({"status": {"phase": "executing", "message": f"执行命令：{short_cmd}"}})
                                yield _thought(
                                    "action_start",
                                    "执行命令",
                                    short_cmd,
                                    {"action": "run_command", "command": cmd[:200]},
                                )
                            elif task_action == "read_resource":
                                res_path = str(task.get("path") or task.get("resource_handle") or "")
                                yield _sse({"status": {"phase": "reading", "message": f"读取资源：{res_path}"}})
                                yield _thought(
                                    "action_start",
                                    "读取资源",
                                    res_path,
                                    {"action": "read_resource", "path": res_path},
                                )
                            elif task_action == "write_file":
                                wf_path = str(task.get("path") or "")
                                yield _sse({"status": {"phase": "writing", "message": f"写入文件：{wf_path}"}})
                                yield _thought(
                                    "action_start",
                                    "写入文件",
                                    wf_path,
                                    {"action": "write_file", "path": wf_path},
                                )
                            elif task_action == "create_directory":
                                cd_path = str(task.get("path") or "")
                                yield _sse({"status": {"phase": "creating", "message": f"创建目录：{cd_path}"}})
                                yield _thought(
                                    "action_start",
                                    "创建目录",
                                    cd_path,
                                    {"action": "create_directory", "path": cd_path},
                                )
                            else:
                                yield _thought(
                                    "action_start",
                                    "执行动作",
                                    task_action,
                                    {"action": task_action},
                                )

                            # Run the task in a thread and capture the result.
                            task_result, task_touched = await asyncio.to_thread(
                                functools.partial(
                                    _execute_single_task,
                                    task,
                                    [],
                                    request,
                                    execution_root=execution_root,
                                    inferred_skill_root=_exec_inferred_root,
                                    skill_name=parent_skill_name,
                                    session_input_dir=_exec_session_dir,
                                )
                            )
                            _exec_all_results.append(task_result)
                            _exec_all_touched.extend(task_touched)

                            # Build safe result data for the thought (truncate stdout/stderr).
                            _safe_result = {
                                k: (v[:1000] if isinstance(v, str) else v)
                                for k, v in task_result.items()
                                if k not in {"content"}  # omit large resource content
                            }

                            success_flag = task_result.get("success", True)
                            if task_action == "run_command":
                                rc = task_result.get("returncode", 0)
                                yield _thought(
                                    "action_result",
                                    "执行结果",
                                    f"{'成功' if success_flag else '失败'} exit={rc}",
                                    _safe_result,
                                )
                            elif task_action == "read_resource":
                                yield _thought(
                                    "action_result",
                                    "读取结果",
                                    f"{'成功' if success_flag else '失败'}，"
                                    f"{len(task_result.get('content', ''))} 字符",
                                    _safe_result,
                                )
                            else:
                                yield _thought(
                                    "action_result",
                                    "操作结果",
                                    f"{'成功' if success_flag else '失败'}",
                                    _safe_result,
                                )

                        # Post-loop: validate any newly created Skill roots.
                        for root in _find_created_skill_roots(_exec_all_touched):
                            skill_md = root / "SKILL.md"
                            if skill_md.exists():
                                _validate_skill_md(skill_md)

                        # Assemble exec_result compatible with _generate_final_answer_from_observation.
                        _exec_all_output_files: list[dict] = []
                        for r in _exec_all_results:
                            _exec_all_output_files.extend(r.get("output_files") or [])

                        exec_result = {
                            "executed": True,
                            "reason": "已根据结构化 action plan 逐任务执行。",
                            "plan": runtime_plan,
                            "results": _exec_all_results,
                            "logs": [],
                            "output_files": _exec_all_output_files,
                        }

                        yield _sse({"status": {"phase": "generating", "message": "生成最终回答…"}})
                        final_answer = await _generate_final_answer_from_observation(
                            body_prompt=body_prompt,
                            request=request,
                            model=model,
                            plan=runtime_plan,
                            execution_result=exec_result,
                        )
                        yield _thought(
                            "final_answer",
                            "生成回答",
                            f"共 {len(final_answer)} 字符，包含 {len(_exec_all_output_files)} 个输出文件",
                            {
                                "answer_chars": len(final_answer),
                                "has_output_files": bool(_exec_all_output_files),
                                "output_file_count": len(_exec_all_output_files),
                            },
                        )

                        yield _sse({"status": None})

                        # Emit structured output_files event so the frontend can
                        # render download links without relying on LLM text parsing.
                        if _exec_all_output_files:
                            yield _sse({
                                "action_result": {
                                    "action": "output_files",
                                    "name": parent_skill_name,
                                    "success": True,
                                    "message": f"生成了 {len(_exec_all_output_files)} 个文件",
                                    "output_files": _exec_all_output_files,
                                }
                            })

                        yield _sse({"content": final_answer})
                        yield "data: [DONE]\n\n"
                        return

                    if mode == "ask_user":
                        yield _sse({"status": None})
                        missing = runtime_plan.get("missing") or []
                        errors = runtime_plan.get("errors") or []

                        if missing:
                            text = "缺少必要信息，无法执行 Skill：\n" + "\n".join(
                                f"- {item}" for item in missing
                            )
                        elif errors:
                            text = "运行时规划失败：\n" + "\n".join(
                                f"- {json.dumps(item, ensure_ascii=False)}" for item in errors
                            )
                        else:
                            text = "缺少必要信息，无法执行当前 Skill。"

                        yield _sse({"content": text})
                        yield "data: [DONE]\n\n"
                        return

                    if mode == "not_applicable":
                        yield _sse({"status": None})
                        yield _sse({"content": "当前用户请求与该 Skill 不匹配，请重新选择 Skill 或重新描述需求。"})
                        yield "data: [DONE]\n\n"
                        return

                    # mode == direct_answer 时继续走普通主模型回复。
                    yield _sse({"status": None})

                except Exception as exc:
                    logger.exception("runtime skill action planning/execution failed")
                    yield _sse({"status": None})
                    yield _sse({"error": f"错误：运行时规划或执行失败：{exc}"})
                    yield "data: [DONE]\n\n"
                    return

            final_messages = [
                {
                    "role": "system",
                    "content": body_prompt,
                }
            ]

            # Inject the per-state constraint immediately after the body prompt so that
            # small models see it before any other system instructions.  This is
            # important for state A: without this ordering, the artifact-consistency
            # prompt ("你正在创建一个可运行的 Skill 包") comes first and overrides the
            # "ask one question only" constraint, causing the model to skip requirement
            # collection and jump straight to blueprint generation.
            if skip_runtime_planner_before_confirmation:
                if creator_state_ctx is None:
                    raise RuntimeError(
                        "Internal error: creator_state_ctx must be initialized when "
                        "skip_runtime_planner_before_confirmation is enabled. "
                        "This indicates _detect_creator_state() was not called or its result was not stored."
                    )
                final_messages.append(
                        {
                            "role": "system",
                            "content": _compose_creator_state_injection(
                                creator_state,
                                blueprint_shown=creator_state_ctx.blueprint_shown,
                                requirement_analysis=creator_state_ctx.requirements,
                            ),
                        }
                    )

                # When the frontend drives file creation (plan C), state-C just needs a
                # brief acknowledgement — suppress code/file generation from the LLM.
                if use_frontend_driven_creation and creator_state == "C":
                    final_messages.append(
                        {
                            "role": "system",
                            "content": (
                                "【前端主控创建模式】用户已确认，前端创建面板将接管文件生成流程。\n"
                                "你的任务：只输出一句简短确认，例如：\n"
                                "  '好的，开始按蓝图创建 Skill 文件，请在下方面板中查看进度。'\n"
                                "严禁输出：fenced code block、SKILL.md 内容、脚本代码、文件列表、目录结构。\n"
                                "严禁说：'正在生成'、'以下是代码'、'下面是实现'、'已创建完成'。"
                            ),
                        }
                    )

            # Artifact-consistency prompt is only relevant during blueprint-revision
            # (state B) and file-generation (state C).  In state A the model is still
            # gathering requirements; injecting "you are in strict creation mode" here
            # contradicts the state-A constraint and causes weaker models to skip
            # requirement collection entirely.
            if strict_creator_generation and creator_state != "A":
                final_messages.append(
                    {
                        "role": "system",
                        "content": _compose_creator_artifact_consistency_prompt(),
                    }
                )

            if strict_skill_execution:
                final_messages.append(
                    {
                        "role": "system",
                        "content": (
                            "当前处于沙盒 Skill 严格执行模式。\n\n"
                            "你必须严格遵循已经加载的 Loaded SKILL.md，禁止把它当作普通参考资料。\n"
                            "你不得绕过 Loaded SKILL.md 自由回答用户请求。\n"
                            "你不得自行编造业务结果、执行结果、计划内容、文件内容或命令输出。\n\n"
                            "如果 Loaded SKILL.md 语义上要求通过某种动作完成任务，"
                            "例如运行程序、调用脚本、执行命令、写入文件、读取资源、生成配置、运行测试或调用工具，"
                            "你必须先按照 Loaded SKILL.md 的原始要求输出该动作的实际形式。\n"
                            "动作表达形式由 Loaded SKILL.md 决定，不能固定假设某种章节、某种语言、某种命令或某种格式。\n\n"
                            "如果动作中包含示例输入、占位输入、演示参数或模板参数，"
                            "只要语义上对应当前用户输入，就必须替换为当前用户的真实输入。\n"
                            "不能在应该替换时原样保留示例值或占位值。\n\n"
                            "如果缺少必要参数，必须明确指出缺少哪些信息；"
                            "不得猜测，不得保留占位符继续输出，不得直接编造最终结果。\n\n"
                            "只有当 Loaded SKILL.md 明确要求直接生成文本结果，"
                            "或者不存在任何外部动作要求时，才可以直接生成文本结果。\n"
                        ),
                    }
                )

            final_messages.extend(_request_messages_with_files(request))

            assistant_chunks: list[str] = []
            async for chunk in stream_chat(final_messages, model):
                assistant_chunks.append(chunk)
                yield _sse({"content": chunk})

            assistant_text = "".join(assistant_chunks)

            if enable_action_execution:
                # In frontend-driven creation mode (plan C), state C is handled entirely
                # by the frontend panel.  Skip the legacy block-planner path so it does
                # not try to execute file-write actions from the brief acknowledgement text.
                if use_frontend_driven_creation and creator_state == "C":
                    yield _sse({"status": None})
                    yield "data: [DONE]\n\n"
                    return

                try:
                    exec_result = await _plan_and_execute_generated_output(
                        assistant_text=assistant_text,
                        request=request,
                        model=model,
                        require_confirmation=require_action_confirmation,
                        execution_root=execution_root,
                        skill_name=parent_skill_name,
                    )

                    if exec_result.get("executed"):
                        yield _sse({"content": _format_execution_report(exec_result)})

                        # Emit structured output_files event for the fallback path too.
                        output_files = exec_result.get("output_files") or []
                        if output_files:
                            yield _sse({
                                "action_result": {
                                    "action": "output_files",
                                    "name": parent_skill_name,
                                    "success": True,
                                    "message": f"生成了 {len(output_files)} 个文件",
                                    "output_files": output_files,
                                }
                            })

                except Exception as exc:
                    logger.exception("legacy markdown action fallback failed")
                    yield _sse({"status": None})
                    yield _sse({"error": f"错误：后台规划或执行文件操作失败：{exc}"})
                    yield "data: [DONE]\n\n"
                    return
            yield _sse({"status": None})
            yield "data: [DONE]\n\n"

        except Exception as exc:
            logger.exception("LLM stream error")
            yield _sse({"status": None})
            yield _sse({"error": _friendly_error(exc)})
            yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")

def _compose_creator_artifact_consistency_prompt() -> str:
    """Creator-stage consistency contract.

    只用于 /creator 阶段。
    目标：
    - 不硬编码任何语言、章节名、命令格式或参数名；
    - 要求模型自己保证生成的 SKILL.md、脚本、配置、说明之间的调用接口一致；
    - 避免出现 SKILL.md 写一种调用方式，脚本实现却接收另一种输入方式。
    """
    return (
        "当前处于 Skill Creator 严格生成模式。\n\n"
        "你正在创建一个可运行的 Skill 包，而不是只写说明文档。"
        "你生成的所有文件必须形成一个自洽的整体，包括但不限于 SKILL.md、脚本、配置、参考文件和测试命令。\n\n"
        "一致性要求：\n"
        "1. 如果你生成了任何可执行入口、脚本、工具调用、配置入口或其他运行资源，"
        "同时又在说明文档中写出了调用方式、运行方式、示例命令或使用步骤，"
        "两者的输入接口必须严格一致。\n"
        "2. 说明文档中的调用方式必须由你生成的实际代码支持；"
        "实际代码接收输入的方式也必须能被说明文档中的调用方式触发。\n"
        "3. 如果代码通过命令行参数接收输入，说明文档中的调用方式必须使用对应的命令行参数。\n"
        "4. 如果代码通过标准输入、文件、环境变量、配置、HTTP 请求体、JSON 字段或其他方式接收输入，"
        "说明文档中的调用方式必须体现同一种输入通道。\n"
        "5. 如果说明文档要求某个参数、字段、文件、输入通道或调用步骤，实际代码必须实现它。\n"
        "6. 如果实际代码实现了某个输入通道，说明文档中的调用方式不得写成另一个不兼容的输入通道。\n"
        "7. 示例值、占位值和演示输入只能用于说明；当需要给出可执行调用示例时，"
        "必须保证该示例在当前生成的代码中真实可运行。\n\n"
        "禁止行为：\n"
        "1. 禁止只生成看起来合理但与代码入口不匹配的调用方式。\n"
        "2. 禁止文档写一种输入形式、代码实现另一种输入形式。\n"
        "3. 禁止依赖后台替你修正参数、命令或输入通道。\n"
        "4. 禁止假设宿主会自动把命令行参数转换成标准输入，或把标准输入转换成命令行参数。\n"
        "5. 禁止生成互相矛盾的 SKILL.md、脚本和测试命令。\n\n"
        "生成前自检：\n"
        "在输出写文件代码块之前，你必须在内部完成一致性检查：\n"
        "- 文档中的每个可执行调用是否被实际代码支持；\n"
        "- 实际代码需要的每个必要输入是否在文档调用方式中提供；\n"
        "- 示例调用是否能在当前 Skill 目录下直接运行；\n"
        "- 报错信息和输出要求是否与文档约束一致。\n\n"
        "输出要求：\n"
        "你仍然只输出普通 Markdown 和 fenced code block。"
        "不要输出自定义动作标签。"
        "需要写入文件时，仍应在代码块附近明确写出保存路径。"
    )

def build_kernel_skill_context() -> dict:
    kernel_metadata_prompt = load_kernel_metadata_prompt()

    return {
        "skill_name": "skill-creator",
        "metadata_prompt": kernel_metadata_prompt,
        "body_loader": load_kernel_body_prompt,
        "child_body_loader": lambda child_ref: load_child_skill_body_prompt("skill-creator", child_ref),
        "force_body": True,
        "enable_action_execution": True,
        "require_action_confirmation": True,
        "execution_root": None,
        "strict_creator_generation": True,

        "skip_runtime_planner_before_confirmation": True,
        "disable_runtime_planner": True,

        # creator 阶段按需读取 references/assets/scripts
        "enable_resource_preload": True,

        # 方案 C：state C 由前端面板主控文件生成，chat 端点只输出简短确认语
        "use_frontend_driven_creation": True,
    }


def build_skill_context(skill_name: str) -> dict:
    skill_root = _skill_root_for_name(skill_name)
    skill_metadata_prompt = load_skill_metadata_prompt(skill_name)

    return {
        "skill_name": skill_name,
        "metadata_prompt": skill_metadata_prompt,
        "body_loader": lambda: load_skill_body_prompt(skill_name),
        "child_body_loader": lambda child_ref: load_child_skill_body_prompt(skill_name, child_ref),
        "force_body": False,
        "enable_action_execution": True,
        "require_action_confirmation": False,
        "execution_root": skill_root,
        "strict_skill_execution": True,

        "enable_resource_preload": True,
    }
@router.post("/creator")
async def chat_with_creator(request: ChatRequest):
    """Multi-turn chat powered by the fixed kernel Skill Creator."""
    try:
        skill_context = build_kernel_skill_context()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return _make_stream(skill_context, request)


@router.post("/sandbox/{skill_name}")
async def chat_in_sandbox(skill_name: str, request: ChatRequest):
    """Multi-turn chat with a specific skill loaded in sandbox mode."""
    try:
        skill_context = build_skill_context(skill_name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return _make_stream(skill_context, request)
