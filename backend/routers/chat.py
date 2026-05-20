import asyncio
import functools
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import httpx
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ..config import settings
from ..services.llm_proxy import complete_chat_once, stream_chat
from ..services.output_validator import retry_with_validation
from .chat_models import ChatRequest, CreatorStateContext, MarkdownBlock
from .creator_chat import (
    _compose_creator_artifact_consistency_prompt,
    _compose_creator_state_injection,
    _compose_creator_validation_messages,
    _detect_creator_state,
    _has_creation_confirmation,
    _simple_sse_content_response,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

# 通用 fenced block 抽取器：用于解析所有 ```lang 代码块（执行与写文件统一入口）
_ALL_FENCE_RE = re.compile(
    r"(?P<fence>`{3,})(?P<info>[^\n`]*)\n(?P<code>[\s\S]*?)\n(?=\1)",
    re.IGNORECASE | re.DOTALL,
)

_PYTHON_HEREDOC_RE = re.compile(
    r"^\s*(?P<python>python3?|[\w./-]*python3?)\s+-\s+<<[ \t]*['\"]?(?P<tag>[A-Za-z_][A-Za-z0-9_]*)['\"]?[ \t]*\n"
    r"(?P<script>.*?)\n(?P=tag)[ \t]*;?\s*$",
    re.DOTALL,
)

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
    from .sandbox_chat import _allowed_skill_roots  # local import avoids circular dependency

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

def _make_stream(skill_context: dict, request: ChatRequest):
    """Staged Skill execution with creator-safe action planning.

    关键逻辑：
    - /creator：用户未明确确认前，只让主模型按 Creator SKILL.md 做需求收集；
      不运行 runtime planner，不执行动作。
    - /creator：用户确认后，允许主模型生成文件块，再由 block planner/executor 写入。
    - /sandbox/{skill_name}：可以使用 runtime planner 执行具体 Skill。
    """
    from .sandbox_chat import (
        _allowed_skill_roots,
        _compose_loaded_resources_prompt,
        _execute_single_task,
        _extract_runtime_resource_catalog,
        _format_execution_report,
        _generate_final_answer_from_observation,
        _infer_skill_root_from_tasks,
        _is_within_sandbox,
        _plan_and_execute_generated_output,
        _run_resource_selection_round,
        _run_skill_runtime_planner_round,
    )

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
                        "Ensure creator_state_ctx is assigned from _detect_creator_state() before this point."
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

            if skip_runtime_planner_before_confirmation and creator_state in {"A", "B"}:
                validator_messages = _compose_creator_validation_messages(
                    creator_state,
                    requirement_analysis=(
                        creator_state_ctx.requirements if creator_state_ctx else None
                    ),
                )
                validated_text, valid, attempts = await retry_with_validation(
                    final_messages,
                    model,
                    max_retries=_CREATOR_VALIDATION_MAX_RETRIES,
                    validator_messages=validator_messages,
                )
                if not valid:
                    logger.warning(
                        "creator output validation failed after retries: %s",
                        [a.feedback for a in attempts],
                    )
                for event in _simple_sse_content_response(validated_text):
                    yield event
                return

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
