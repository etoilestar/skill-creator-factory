"""命令准备与子进程执行。"""

import hashlib
import logging
import os
import shlex
import shutil
from pathlib import Path

from ..chat_utils import (
    _PYTHON_HEREDOC_RE,
    _SCRIPT_INTERPRETERS,
    _allowed_skill_roots,
    _get_skill_venv_python,
    _scan_and_install_python_deps,
    _scan_and_install_node_deps,
    _try_auto_install_interpreter,
)
from .path_resolution import _resolve_safe_path

logger = logging.getLogger(__name__)


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

        ext = exe_path.suffix.lower()
        if ext not in _SCRIPT_INTERPRETERS and os.access(exe_path, os.X_OK):
            argv[0] = str(exe_path)
        else:
            interpreter = _SCRIPT_INTERPRETERS.get(ext)
            if interpreter is not None:
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
    else:
        exe_name = Path(executable).name
        if exe_name in {"python", "python3"} and len(argv) >= 2 and base_dir is not None:
            script_arg = argv[1]
            script_path_candidate: Path | None = None
            if not script_arg.startswith("-") and (
                "/" in script_arg or script_arg.endswith(".py")
            ):
                candidate = Path(script_arg)
                if not candidate.is_absolute():
                    candidate = (base_dir / candidate).resolve()
                else:
                    for prefix in ("/app/scripts/", "/app/references/", "/app/assets/"):
                        if str(candidate).startswith(prefix):
                            rel_path = str(candidate)[len("/app/"):]
                            candidate = (base_dir / rel_path).resolve()
                            break
                try:
                    candidate.relative_to(base_dir.resolve())
                    if candidate.exists() and candidate.suffix.lower() == ".py":
                        script_path_candidate = candidate
                except ValueError:
                    pass
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
        elif exe_name in {"node", "nodejs"} and len(argv) >= 2 and base_dir is not None:
            script_arg = argv[1]
            if not script_arg.startswith("-"):
                candidate = Path(script_arg)
                if not candidate.is_absolute():
                    candidate = (base_dir / candidate).resolve()
                try:
                    candidate.relative_to(base_dir.resolve())
                    if candidate.exists() and candidate.suffix.lower() in {".js", ".mjs", ".cjs"}:
                        try:
                            _scan_and_install_node_deps(candidate, base_dir)
                        except Exception as node_exc:
                            logger.warning("skill-env: node dep scan failed: %s", node_exc)
                except ValueError:
                    pass
            if shutil.which(executable) is None:
                _try_auto_install_interpreter(executable)
        else:
            if shutil.which(executable) is None:
                _try_auto_install_interpreter(executable)

        if shutil.which(executable) is None and not Path(argv[0]).exists():
            raise ValueError(
                f"命令不可执行：{executable} 不在 PATH 中，也不是当前 Skill 内的可执行文件。"
                "如果这是函数名或伪代码，请不要规划 run_command。"
            )

    _validate_skill_local_command_paths(argv, base_dir=base_dir)
    return argv

def _safe_command_argv(command: str, *, base_dir: Path | None = None) -> list[str]:
    """通用命令参数解析器。"""
    if not command or not command.strip():
        raise ValueError("命令为空")

    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"命令解析失败: {command}") from exc

    if not argv:
        raise ValueError("命令为空")

    return argv
