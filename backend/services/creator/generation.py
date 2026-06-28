"""Prompt construction, model calls, content normalization, and file-generation helpers."""

from .common import *  # noqa: F403
from .contracts import *  # noqa: F403
from .e2e import *  # noqa: F403
from .repair import *  # noqa: F403

def _is_valid_normalized_script_source(file_path: str, content: str) -> bool:
    """Return whether content is safe to accept as the requested raw script.

    This helper is intentionally narrower than the full script validator: it only
    checks that deterministic Markdown/bundle cleanup produced one raw source
    file.  Full fake-implementation, contract, dependency and trial-run checks
    still happen in the normal validation pipeline.
    """
    stripped = content.strip()
    if not stripped or "```" in stripped or "~~~" in stripped or _MULTI_FILE_MARKER_RE.search(stripped):
        return False

    return True


def _extract_single_wrapping_fence(content: str) -> str | None:
    """Extract a code block only when it wraps the entire model response.

    Models often wrap repaired scripts in ```text fences, sometimes with CRLF
    line endings or a closing fence that is longer than the opener.  This parser
    intentionally accepts only a whole-response fence: any prose before/after the
    block, or any non-fence trailing line, returns None and lets validation reject
    the ambiguous output.
    """
    stripped = content.strip().lstrip("\ufeff")
    lines = stripped.splitlines()
    if len(lines) < 2:
        return None

    opening = lines[0].strip()
    opening_match = re.match(r"^(`{3,}|~{3,})[^`~]*$", opening)
    if not opening_match:
        return None

    fence = opening_match.group(1)
    fence_char = fence[0]
    min_fence_len = len(fence)
    closing = lines[-1].strip()
    if not re.fullmatch(rf"{re.escape(fence_char)}{{{min_fence_len},}}", closing):
        return None

    return "\n".join(lines[1:-1]).strip()


def _extract_only_fenced_block(content: str) -> str | None:
    """Extract the body only when exactly one fenced block appears in content."""
    lines = content.strip().lstrip("\ufeff").splitlines()
    blocks: list[str] = []
    idx = 0
    while idx < len(lines):
        opening = lines[idx].strip()
        opening_match = re.match(r"^(`{3,}|~{3,})[^`~]*$", opening)
        if not opening_match:
            idx += 1
            continue

        fence = opening_match.group(1)
        fence_char = fence[0]
        min_fence_len = len(fence)
        body: list[str] = []
        idx += 1
        while idx < len(lines):
            closing = lines[idx].strip()
            if re.fullmatch(rf"{re.escape(fence_char)}{{{min_fence_len},}}", closing):
                blocks.append("\n".join(body).strip())
                break
            body.append(lines[idx])
            idx += 1
        else:
            return None

        if len(blocks) > 1:
            return None
        idx += 1

    if len(blocks) != 1:
        return None
    return blocks[0]


def _extract_first_fenced_block(content: str) -> str | None:
    """Extract the first fenced block body, or None if no complete block exists."""
    lines = content.strip().lstrip("\ufeff").splitlines()
    idx = 0
    while idx < len(lines):
        opening = lines[idx].strip()
        opening_match = re.match(r"^(`{3,}|~{3,})[^`~]*$", opening)
        if not opening_match:
            idx += 1
            continue
        fence = opening_match.group(1)
        fence_char = fence[0]
        min_fence_len = len(fence)
        body: list[str] = []
        idx += 1
        while idx < len(lines):
            closing = lines[idx].strip()
            if re.fullmatch(rf"{re.escape(fence_char)}{{{min_fence_len},}}", closing):
                return "\n".join(body).strip()
            body.append(lines[idx])
            idx += 1
        return None
    return None

def _count_fenced_blocks(content: str) -> int:
    """Count complete Markdown fenced code blocks using only fence structure."""
    lines = str(content or "").strip().lstrip("\ufeff").splitlines()
    count = 0
    idx = 0
    while idx < len(lines):
        opening_match = re.match(r"^\s*(`{3,}|~{3,})[^`~]*\s*$", lines[idx])
        if not opening_match:
            idx += 1
            continue
        fence = opening_match.group(1)
        fence_char = fence[0]
        min_fence_len = len(fence)
        idx += 1
        while idx < len(lines):
            if re.fullmatch(rf"\s*{re.escape(fence_char)}{{{min_fence_len},}}\s*", lines[idx]):
                count += 1
                break
            idx += 1
        idx += 1
    return count


def script_raw_source_candidate_error_id(content: str) -> str | None:
    """Return a structural scripts/* raw-source format error id, if any.

    This is intentionally language-agnostic: it only looks at Markdown fence
    structure and multi-file bundle markers.  It never picks one candidate from
    an ambiguous model response.
    """
    stripped = str(content or "").strip().lstrip("\ufeff")
    if not stripped:
        return "script.raw_source.single_file"
    fenced_count = _count_fenced_blocks(stripped)
    if fenced_count > 1:
        return "script.raw_source.ambiguous_multi_code_blocks"
    if _MULTI_FILE_MARKER_RE.search(stripped) or re.search(r"(?im)^\s*写入文件[:：]", stripped):
        return "script.raw_source.multi_file_bundle"
    if fenced_count == 1:
        body = _extract_only_fenced_block(stripped)
        if body is None:
            return "script.raw_source.ambiguous_script_candidate"
    return None


def is_canonical_script_source_candidate(content: str) -> bool:
    """Return whether content is already a single raw script source candidate."""
    return script_raw_source_candidate_error_id(content) is None and _is_valid_normalized_script_source("", content)


def _drop_common_non_code_lines(text: str) -> str:
    """Remove common chat/file-label prose that models place around scripts."""
    drop_patterns = [
        r"^\s*下面是",
        r"^\s*以下是",
        r"^\s*(?:文件|路径)[:：]",
        r"^\s*写入文件[:：]",
        r"^\s*#+\s*scripts/",
        r"^\s*`?scripts/[^`]+`?\s*$",
    ]
    cleaned: list[str] = []
    for line in text.strip().splitlines():
        if any(re.search(pattern, line, flags=re.IGNORECASE) for pattern in drop_patterns):
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def _looks_like_python_source(text: str) -> bool:
    """Return True for probable Python source without requiring valid syntax yet."""
    stripped = text.strip()
    if not stripped or "```" in stripped or "~~~" in stripped or _MULTI_FILE_MARKER_RE.search(stripped):
        return False
    return bool(re.search(
        r"(?m)^\s*(?:import\s+|from\s+|def\s+|class\s+|if __name__\s*==\s*['\"]__main__['\"]|#!/usr/bin/env python|#)",
        stripped,
    ))


def _extract_probable_python_source(content: str) -> str | None:
    """Extract a raw Python source candidate before syntax validation."""
    stripped = content.strip().lstrip("\ufeff")
    candidates: list[str] = []

    normalized = stripped
    for _ in range(3):
        wrapping = _extract_single_wrapping_fence(normalized)
        if wrapping is None:
            break
        normalized = wrapping.strip()
        candidates.append(normalized)

    only_block = _extract_only_fenced_block(stripped)
    if only_block is not None:
        candidates.append(only_block.strip())

    # Use the first block only when the response does not look like an explicit
    # multi-file bundle.  Multi-file bundles are rejected rather than guessed.
    if not _MULTI_FILE_MARKER_RE.search(stripped) and not re.search(r"(?im)^\s*写入文件[:：]", stripped):
        first_block = _extract_first_fenced_block(stripped)
        if first_block is not None:
            candidates.append(first_block.strip())

    candidates.append(stripped)

    seen: set[str] = set()
    for candidate in candidates:
        cleaned = _drop_common_non_code_lines(candidate)
        if cleaned in seen:
            continue
        seen.add(cleaned)
        if _looks_like_python_source(cleaned):
            return cleaned
    return None


def _normalize_generated_file_content(file_path: str, content: str) -> str:
    """Normalize model output into the requested single file content.

    scripts/**:
    - 保守抽取裸源码；
    - 不接受多文件包。

    SKILL.md:
    - 只剥掉包住整个文件的 ```markdown 外层 fence；
    - 保留正文内部正常 ```bash / ```json fenced block；
    - 不在这里校验 command block，可执行性留给第二轮 E2E。

    references/assets:
    - 继续按普通文件处理。
    """
    if file_path.startswith("scripts/"):
        stripped = content.strip()
        structural_error = script_raw_source_candidate_error_id(stripped)
        if structural_error in {
            "script.raw_source.ambiguous_multi_code_blocks",
            "script.raw_source.multi_file_bundle",
            "script.raw_source.ambiguous_script_candidate",
        }:
            return stripped

        normalized = stripped
        for _ in range(3):
            wrapping_fence = _extract_single_wrapping_fence(normalized)
            if wrapping_fence is None:
                break
            normalized = wrapping_fence.strip()
            if _is_valid_normalized_script_source(file_path, normalized):
                return normalized

        only_block = _extract_only_fenced_block(stripped)
        if only_block is not None:
            normalized = only_block.strip()
            if _is_valid_normalized_script_source(file_path, normalized):
                return normalized

        candidate = stripped if structural_error else _strip_orphan_trailing_fence(stripped)
        if _is_valid_normalized_script_source(file_path, candidate):
            return candidate

        return candidate

    extracted = _extract_target_file_from_bundle(content, file_path)
    candidate = extracted if extracted is not None else content

    if file_path == "SKILL.md":
        return _strip_outer_markdown_fence_for_skill_md(candidate).strip()

    return _strip_code_fence(candidate)



def _trim_source_to_runtime_entrypoint(file_path: str, content: str, skill_plan_entry: dict[str, Any] | None = None) -> str:
    """Drop leading/trailing prose around a probable runtime source entrypoint."""
    if not file_path.startswith("scripts/"):
        return content.strip()
    plan_entry = _skill_plan_entry_for_file(file_path=file_path, skill_plan_entry=skill_plan_entry)
    text = _strip_orphan_trailing_fence(content.strip().lstrip("\ufeff"))
    lines = text.splitlines()

    start_patterns: list[str]
    end_patterns: list[str]
    if plan_entry.runtime == "node":
        start_patterns = [r"^\s*(?:const|let|var)\s+", r"^\s*function\s+", r"^\s*#!/usr/bin/env\s+node"]
        end_patterns = [r"console\.log\s*\("]
    elif plan_entry.runtime in {"bash", "shell"}:
        start_patterns = [r"^\s*#!/", r"^\s*set\s+-", r"^\s*payload_json=", r"^\s*[A-Za-z_][A-Za-z0-9_]*="]
        end_patterns = [r"^\s*(?:echo|printf|python\s+-c)\b"]
    else:
        start_patterns = [r"^\s*(?:import\s+|from\s+|def\s+|class\s+|#!/usr/bin/env\s+python)"]
        end_patterns = [r"main\s*\(\s*\)"]

    start_idx = 0
    for idx, line in enumerate(lines):
        if any(re.search(pattern, line) for pattern in start_patterns):
            start_idx = idx
            break
    trimmed = lines[start_idx:]

    end_idx = len(trimmed)
    for idx in range(len(trimmed) - 1, -1, -1):
        line = trimmed[idx]
        if any(re.search(pattern, line) for pattern in end_patterns):
            end_idx = idx + 1
            break
    return "\n".join(trimmed[:end_idx]).strip()

_REFERENCE_EXECUTABLE_SCRIPT_CMD_RE = re.compile(
    r"(?m)^\s*(?:python|python3|node|bash|sh)\s+scripts/[A-Za-z0-9_./-]+\b"
)

_MARKDOWN_FENCED_BLOCK_RE = re.compile(
    r"```(?P<lang>[A-Za-z0-9_-]*)\s*\n(?P<body>.*?)```",
    re.DOTALL,
)


def _sanitize_reference_markdown(content: str) -> str:
    """Keep reference examples non-executable.

    references/*.md may contain examples, including code examples.
    But executable shell blocks that call scripts/** must not be preserved as
    ```bash / ```sh / ```shell, because references are documentation resources,
    not workflow sources.
    """
    def repl(match: re.Match[str]) -> str:
        lang = (match.group("lang") or "").strip().lower()
        body = match.group("body") or ""

        if lang in {"bash", "sh", "shell"} and _REFERENCE_EXECUTABLE_SCRIPT_CMD_RE.search(body):
            return "```text\n" + body.strip() + "\n```"

        return match.group(0)

    return _MARKDOWN_FENCED_BLOCK_RE.sub(repl, content)

def _sanitize_generated_file_content(
    file_path: str,
    content: str,
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
) -> str:
    """Normalize model output into exactly the requested file content.

    这里只做 normalize/sanitize，不做合同校验。

    原因：
    - references/*.md 需要先 sanitize，再补/规范化 frontmatter，再进入 contract check；
    - 如果这里提前调用 _validate_generated_file_content，会在 frontmatter 修复前误杀；
    - write-file 阶段也不应再次校验，避免前端展示内容与落盘内容不一致。
    """
    if file_path.startswith("scripts/") and script_raw_source_candidate_error_id(content) in {
        "script.raw_source.ambiguous_multi_code_blocks",
        "script.raw_source.multi_file_bundle",
        "script.raw_source.ambiguous_script_candidate",
    }:
        sanitized = content.strip()
    else:
        sanitized = _normalize_generated_file_content(file_path, content)
        sanitized = _trim_source_to_runtime_entrypoint(
            file_path,
            sanitized,
            skill_plan_entry=skill_plan_entry,
        )

    if file_path.startswith("references/") or role == "reference":
        sanitized = _sanitize_reference_markdown(sanitized)

    return sanitized

def _strip_outer_markdown_fence_for_skill_md(content: str) -> str:
    """Strip only one outer markdown fence around a whole SKILL.md file.

    只处理模型把整个 SKILL.md 包成：

    ```markdown
    ---
    name: ...
    ---
    ...
    ```

    的情况。

    不处理正文内部的 ```bash / ```json block，
    避免误删 SKILL.md 内部正常 fenced code block。
    """
    text = (content or "").strip().lstrip("\ufeff")
    if not text.startswith(("```", "~~~")):
        return content

    lines = text.splitlines()
    if len(lines) < 3:
        return content

    opening = lines[0].strip()
    open_match = re.match(r"^(`{3,}|~{3,})(?:markdown|md)?\s*$", opening, flags=re.I)
    if not open_match:
        return content

    fence = open_match.group(1)
    fence_char = fence[0]
    fence_len = len(fence)

    closing = lines[-1].strip()
    if not re.fullmatch(rf"{re.escape(fence_char)}{{{fence_len},}}\s*", closing):
        return content

    body = "\n".join(lines[1:-1]).strip()
    return body + "\n"

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




def _script_generation_skeleton(
    file_path: str,
    purpose: str,
    blueprint_text: str,
    *,
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
) -> str:
    """Return protocol-only scaffolds; tool calls come from Tool Registry snippets."""
    plan_entry = _skill_plan_entry_for_file(
        file_path=file_path,
        purpose=purpose,
        blueprint_text=blueprint_text,
        role=role,
        skill_plan_entry=skill_plan_entry,
    )
    input_keys = list(plan_entry.inputs or ["payload"])
    output_keys = [key for key in (plan_entry.outputs or []) if isinstance(key, str) and key.strip()]
    if not output_keys:
        output_keys = ["text"]

    component_hint = getattr(plan_entry, "component_hint", "") or getattr(plan_entry, "role", "")
    helper_hint = f"# component_hint: {component_hint}\n"

    if plan_entry.runtime == "node":
        return (
            "协议骨架（只约束 argv/run/stdout；具体工具调用必须来自 Tool Registry snippets/function cards）：\n"
            + helper_hint +
            "const payload = process.argv[2] ? JSON.parse(process.argv[2]) : {};\n"
            "function run(payload) {\n"
            "  // TODO: implement the canonical contract using selected tools or real local logic.\n"
            "  // Return an object containing every required stdout field.\n"
            "  return {};\n"
            "}\n"
            "console.log(JSON.stringify(run(payload)));"
        )

    if plan_entry.runtime in {"bash", "shell"}:
        helper = "import json,sys; json.loads(sys.argv[1] or '{}'); print(json.dumps({}))"
        return (
            "协议骨架（只约束 $1 JSON argv 与 stdout JSON；具体工具调用必须来自 Tool Registry snippets/function cards）：\n"
            + helper_hint +
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "payload_json=${1:-'{}'}\n"
            f"python -c {shlex.quote(helper)} \"$payload_json\""
        )

    return (
        "协议骨架（只约束 parse_args/run/main/stdout JSON；具体工具调用必须来自 Tool Registry snippets/function cards）：\n"
        + helper_hint +
        "import json\n"
        "import sys\n\n"
        "# Replace input_text with this script's real argv keys; use empty sets only for a true no-input script.\n"
        "ALLOWED_KEYS = {\"input_text\"}\n"
        "REQUIRED_KEYS = {\"input_text\"}\n"
        "OPTIONAL_KEYS = set()\n"
        "DEFAULT_VALUES = {}\n"
        "EXPECTED_TYPES = {\"input_text\": str}\n\n"
        "def parse_args() -> dict:\n"
        "    if len(sys.argv) < 2:\n"
        "        raise ValueError('missing JSON argv')\n"
        "    data = json.loads(sys.argv[1])\n"
        "    if not isinstance(data, dict):\n"
        "        raise ValueError('argv JSON must be an object')\n"
        "    unknown = set(data) - (ALLOWED_KEYS | OPTIONAL_KEYS)\n"
        "    if unknown:\n"
        "        raise ValueError(f'unknown argv keys: {sorted(unknown)}')\n"
        "    missing = REQUIRED_KEYS - set(data)\n"
        "    if missing:\n"
        "        raise ValueError(f'missing required argv keys: {sorted(missing)}')\n"
        "    validated = {}\n"
        "    for key in REQUIRED_KEYS:\n"
        "        value = data[key]\n"
        "        if value is None or value == '' or value == [] or value == {}:\n"
        "            raise ValueError(f'empty required argv value: {key}')\n"
        "        if key in EXPECTED_TYPES and not isinstance(value, EXPECTED_TYPES[key]):\n"
        "            raise TypeError(f'invalid argv type for {key}')\n"
        "        validated[key] = value\n"
        "    for key in OPTIONAL_KEYS:\n"
        "        if key in data:\n"
        "            value = data[key]\n"
        "        elif key in DEFAULT_VALUES:\n"
        "            value = DEFAULT_VALUES[key]\n"
        "        else:\n"
        "            continue\n"
        "        if key in EXPECTED_TYPES and not isinstance(value, EXPECTED_TYPES[key]):\n"
        "            raise TypeError(f'invalid argv type for {key}')\n"
        "        validated[key] = value\n"
        "    return validated\n\n"
        "def run(args: dict) -> dict:\n"
        "    # TODO: implement the canonical contract using selected tools or real local logic.\n"
        "    # Only read values from args after parse_args validation; use args['required_key'] for required values.\n"
        "    # Return an object containing every required stdout field.\n"
        "    return {}\n\n"
        "def main() -> None:\n"
        "    print(json.dumps(run(parse_args()), ensure_ascii=False))\n\n"
        "if __name__ == '__main__':\n"
        "    main()"
    )


def _creator_kernel_reference_context() -> str:
    """Load small Creator prompt context from kernel references.

    These references are advisory generation context only; SKILL.md protocol and
    generated file contracts remain unchanged.
    """
    kernel_dir = Path(__file__).resolve().parents[2] / "kernel"
    candidates = [
        kernel_dir / "references" / "best-practices.md",
        kernel_dir / "references" / "workflows.md",
        kernel_dir / "references" / "output-patterns.md",
        kernel_dir / "SKILL.md",
    ]
    chunks: list[str] = []
    for path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            rel = path.relative_to(kernel_dir.parent)
            chunks.append(f"### INTERNAL-ONLY {rel}\n{text[:1800]}")
    return "\n\n".join(chunks)


def _script_local_contract_payload(
    *,
    file_path: str,
    purpose: str,
    plan_entry: SkillPlanEntry,
    stdout_schema: dict[str, Any],
) -> dict[str, Any]:
    """Return the only business contract a script-generation prompt should need.

    通用原则：
    - available_tools 只是基础能力候选，不是完整业务方案枚举；
    - 没有召回到专用工具，不代表脚本不能实现；
    - script 可以通过标准库、本地 helper、一个或多个基础工具组合完成职责。
    """
    canonical_contract = compile_canonical_file_contract(plan_entry, stdout_schema)
    implementation_resolution = resolve_implementation(plan_entry, canonical_contract)

    available_tools: list[dict[str, Any]] = []
    tool_function_cards: list[str] = []
    selected_tool_names: list[str] = []

    for tool in (implementation_resolution.available_tools or implementation_resolution.selected_tools or []):
        capability_name = str(tool.tool_id).split(".")[0]
        selected_tool_names.append(capability_name)
        cap = get_tool_capability(capability_name)
        if cap is not None:
            tool_function_cards.extend(function_cards_for_tool(cap))

        available_tools.append({
            "tool_id": tool.tool_id,
            "description": tool.description,
            "call_template": call_template_for_tool(tool),
            "signature": tool.signature,
            "input_schema": tool.input_schema,
            "output_schema": tool.output_schema,
            "return_contract": tool.return_contract,
            "example_return": tool.example_return,
            "example_stdout": tool.example_stdout,
            "common_mistakes": tool.common_mistakes,
            "snippets": tool.snippets,
            "usage_policy": tool.usage_policy,
            "required_env": tool.required_env,
            "required_secrets": tool.required_secrets,
            "artifact_outputs": tool.artifact_outputs,
        })

    tool_snippets = resolve_tool_snippets_for_context(
        role=plan_entry.role or "",
        capabilities=list(dict.fromkeys([
            req.capability_id
            for req in canonical_contract.capability_requirements
            if req.capability_id
        ])),
        tool_names=list(dict.fromkeys(selected_tool_names)),
        file_path=file_path,
        max_snippets=8,
    )

    return {
        "file_path": file_path,
        "runtime": plan_entry.runtime,
        "language": plan_entry.language,
        "script_goal": purpose,
        "inputs": canonical_contract.inputs,
        "outputs": canonical_contract.outputs,
        "available_tools": available_tools,
        "tool_function_cards": tool_function_cards,
        "tool_snippets": tool_snippets,
        "tool_snippet_prompt": tool_snippet_prompt(tool_snippets),
        "resource_refs": canonical_contract.resource_refs,
        "output_contract": {
            "stdout_schema": stdout_schema,
            "artifact_contract": canonical_contract.artifact_contract,
        },
        "runtime_envelope": {
            "description": (
                "Creator/Skill runtime may provide a generic JSON argv envelope. "
                "Scripts should read explicit contract fields when present, and may also use "
                "generic envelope fields such as payload, user_request, fields, options, "
                "input_files, files, and resources when they need external user inputs or files."
            ),
            "generic_fields": ["payload", "user_request", "fields", "options", "input_files", "files", "resources"],
            "smoke_note": (
                "Smoke test may provide real sample files through input_files/files/resources. "
                "Do not hardcode sample paths; read them from argv if needed."
            ),
        },
        "rules": [
            "Use script_composition: combine argv inputs, local logic, standard library, and any useful available_tools to satisfy this single script goal.",
            "available_tools are recalled base capabilities, not an exhaustive list of business solutions; the script may implement composition/adaptation locally instead of waiting for a specialized tool.",
            "Only import a platform tool using its exact call_template import path; never guess backend imports.",
            "Standard-library imports and already-available runtime libraries may be used for local composition.",
            "References/assets are resource_refs only, not pip/install/import dependencies.",
            "First round fixes only the current script; end-to-end chain repair happens later.",
        ],
        "implementation_resolution": {
            "mode": implementation_resolution.mode,
            "available_tools": available_tools,
            "tool_function_cards": tool_function_cards,
            "tool_snippets": tool_snippets,
            "tool_snippet_prompt": tool_snippet_prompt(tool_snippets),
            "allowed_imports": implementation_resolution.allowed_imports,
            "declared_dependencies": implementation_resolution.declared_dependencies,
            "required_evidence": implementation_resolution.required_evidence,
            "reason": implementation_resolution.reason,
        },
    }


def _creator_file_generation_messages(task_content: str, *, system_rule: str | None = None) -> list[dict]:
    """Return Creator generation messages with the concrete task in user role."""
    rule = system_rule or "你是 Creator 文件内容生成器。只遵守用户消息中的当前文件生成任务；只输出目标文件内容。"
    return [
        {"role": "system", "content": rule},
        {"role": "user", "content": task_content},
    ]


def _ensure_user_visible_task_message(messages: list[dict]) -> list[dict]:
    """Guarantee same-model retries always include a user-visible task message."""
    if any(isinstance(message, dict) and message.get("role") == "user" and str(message.get("content") or "").strip() for message in messages):
        return messages
    task_content = "\n\n".join(
        str(message.get("content") or "")
        for message in messages
        if isinstance(message, dict) and str(message.get("content") or "").strip()
    )
    return _creator_file_generation_messages(task_content)


def _message_role_counts(messages: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "unknown")
        counts[role] = counts.get(role, 0) + 1
    return counts


def _message_role_chars(messages: list[dict], role: str) -> int:
    return sum(
        len(str(message.get("content") or ""))
        for message in messages
        if isinstance(message, dict) and message.get("role") == role
    )


def _build_script_generate_file_prompt_variant(
    *,
    file_path: str,
    skill_name: str,
    purpose: str,
    blueprint_text: str,
    role: str | None,
    skill_plan_entry: dict[str, Any] | None,
    variant: str,
) -> list[dict]:
    """Build script-only prompts using progressively smaller local contracts.

    Scripts must not receive the full blueprint, creator UI copy, global kernel
    docs, or E2E/platform workflow text. The platform owns invocation/stdout
    parsing; the model only implements this one file's internals.
    """
    plan_entry = _skill_plan_entry_for_file(
        file_path=file_path,
        purpose=purpose,
        blueprint_text=blueprint_text,
        role=role,
        skill_plan_entry=skill_plan_entry,
    )
    stdout_schema = _script_stdout_schema_for_entry(plan_entry)
    local_contract = _script_local_contract_payload(
        file_path=file_path,
        purpose=purpose,
        plan_entry=plan_entry,
        stdout_schema=stdout_schema,
    )

    implementation_payload = (
        local_contract.get("implementation_resolution")
        if isinstance(local_contract.get("implementation_resolution"), dict)
        else {}
    )
    implementation_mode = str(implementation_payload.get("mode") or "script_composition")

    if implementation_mode == "unresolved":
        # unresolved 只能说明“没有召回到完整专用工具方案”，不能说明脚本无法实现。
        # Creator 的定位是：平台提供基础工具与运行环境，script 通过标准库、本地 helper、
        # 一个或多个基础工具组合完成业务职责。
        logger.warning(
            "[Creator][script_generation_contract][implementation_unresolved_downgraded] file_path=%s reason=%s",
            file_path,
            str(implementation_payload.get("reason") or ""),
        )
        implementation_mode = "script_composition"
        implementation_payload["mode"] = "script_composition"
        implementation_payload["unresolved_advisory"] = (
            implementation_payload.get("reason")
            or "no specialized tool resolution; use script composition"
        )
        local_contract["implementation_resolution"] = implementation_payload

    script_skeleton_text = ""
    if variant in {"standard", "simplified"}:
        script_skeleton_text = _script_generation_skeleton(
            file_path,
            purpose,
            "",
            role=plan_entry.role,
            skill_plan_entry=skill_plan_entry,
        )

    resolution_payload = (
        local_contract.get("implementation_resolution")
        if isinstance(local_contract.get("implementation_resolution"), dict)
        else {}
    )
    selected_tools_payload = (
        resolution_payload.get("available_tools")
        if isinstance(resolution_payload, dict)
        else []
    )
    tool_function_cards = (
        local_contract.get("tool_function_cards")
        if isinstance(local_contract.get("tool_function_cards"), list)
        else []
    )
    tool_snippets = (
        local_contract.get("tool_snippets")
        if isinstance(local_contract.get("tool_snippets"), list)
        else []
    )

    logger.info(
        "[Creator][script_generation_contract] file_path=%s inputs=%s outputs=%s output_contract=%s resource_refs=%s mode=%s available_tools=%s tool_function_cards_count=%d tool_snippets_count=%d required_evidence=%s allowed_imports=%s declared_dependencies=%s reason=%s",
        file_path,
        json.dumps(local_contract.get("inputs") or [], ensure_ascii=False),
        json.dumps(local_contract.get("outputs") or [], ensure_ascii=False),
        json.dumps(local_contract.get("output_contract") or {}, ensure_ascii=False, sort_keys=True),
        json.dumps(local_contract.get("resource_refs") or [], ensure_ascii=False, sort_keys=True),
        implementation_mode,
        json.dumps(
            [tool.get("tool_id") for tool in selected_tools_payload if isinstance(tool, dict)],
            ensure_ascii=False,
        ),
        len(tool_function_cards),
        len(tool_snippets),
        json.dumps(resolution_payload.get("required_evidence") or [], ensure_ascii=False),
        json.dumps(resolution_payload.get("allowed_imports") or [], ensure_ascii=False),
        json.dumps(resolution_payload.get("declared_dependencies") or [], ensure_ascii=False),
        str(resolution_payload.get("reason") or ""),
    )

    instruction = [
        f'你正在为 Skill 包 "{skill_name}" 生成单个脚本文件：{file_path}。',
        "必须满足以下脚本文件合同（局部合同）：",
        "只实现当前文件；不要重新规划整个 Skill；不要输出 Markdown fence、解释、文件名标题或多文件包。",
        "scripts/ 生成不会追加聊天历史，也不会注入完整蓝图。",
        "外层调用、参数传递和 stdout 解析由 Creator 的确定性规则处理；你不要自由改协议，只实现内部逻辑。",
        "脚本必须读取一个 JSON object argv（Python: 读取 sys.argv[1] 并 json.loads 解析；Node: process.argv[2]；Bash: $1），并向 stdout 输出结构化 JSON object。",
        "硬性 argv schema 规则：当前脚本必须在源码内声明自己实际接受的 allowed_keys 和 required_keys（或等价显式结构），parse_args/validate_payload 必须 fail-fast 校验 JSON argv object。",
        "硬性 argv schema 规则：unknown key、missing required key、required 空字符串/空列表/空对象/None、required 类型错误都必须 raise 或非零退出；参数错误时不得输出成功 JSON。",
        "硬性 argv schema 规则：不要一刀切禁止默认值；如果某个参数确实可选或可由脚本默认，必须在 schema 中显式标为 OPTIONAL_KEYS/DEFAULT_VALUES 或等价结构；required 参数不得靠默认值补齐。",
        "硬性 argv schema 规则：禁止忽略未知参数，禁止多传参数静默通过；生成脚本时 parse_args 必须返回已经校验过的参数对象，后续 run() 只能使用该对象；required 参数推荐用 validated['key'] 读取。",
        "硬性 argv schema 规则：骨架中的 input_text 只是示例，必须替换为当前脚本真实参数；禁止保留 input_text/example placeholder、ellipsis、set(...)、{...}、TODO schema 或 placeholder schema；确实无输入时 allowed_keys/required_keys 可为空且 SKILL.md command 传 {}。",
        "stdout JSON 不得包含 error 字段；必须至少包含 stdout_schema.required 中的字段且值非空。",
        "必须读取输入并输出符合 stdout_schema.required 的非空字段；不要通过 error 字段、{}、空文件或空路径绕过运行和产物校验。",
        "只根据轻量上下文实现：script_goal、inputs、outputs、available_tools、tool_function_cards、tool_snippets、tool_snippet_prompt、resource_refs、output_contract、runtime_envelope、rules。",
        "raw role/capability 只能作为 hint，不能当硬合同。",
        "统一按 script_composition 生成脚本：代码模型根据功能目标自行决定如何组合 argv 输入、本地逻辑、标准库和 available_tools。",
        "available_tools 是基础能力候选，不是完整业务方案枚举；不要因为缺少某个专用工具就放弃实现当前脚本职责。",
        "当前脚本可以定义局部 helper，使用标准库或运行环境中已有通用库做字段适配、内容组织、格式转换、文件处理和产物组装。",
        "可以调用一个或多个 available_tools，也可以完全用本地确定性逻辑实现；关键是核心输入必须影响核心输出或产物内容。",
        "工具/helper/标准库如何组合不作为第一轮 hard gate；如 import/dependency、调用、stdout 或 artifact 失败，再修当前脚本。",
        "如果当前脚本需要外部文件或用户上传资源，应从 JSON argv 的显式合同字段或通用 envelope 字段读取，例如 input_files/files/resources；不要在源码中写死 smoke 样例路径。",
        "Creator smoke 可能会在 input_files/files/resources 中提供真实样例文件，用于验证脚本是否能处理外部文件；这只是试运行输入，不是业务逻辑常量。",
        "第一轮只修当前脚本；不要修改或重规划上下游链路，第二轮 E2E 才修整链路。",
        f"prompt_variant: {variant}",
        "当前文件结构化合同：",
        json.dumps(local_contract, ensure_ascii=False, indent=2),
        "动态工具函数卡片（从 registry/manifest 读取，不硬编码工具名）：",
        "\n\n---\n\n".join(tool_function_cards) if tool_function_cards else "无",
        "动态工具 Snippet 指南（从 registry/manifest 读取，不硬编码工具名）：",
        str(local_contract.get("tool_snippet_prompt") or "当前脚本可用工具 Snippets: 无"),
    ]

    if variant == "standard":
        instruction.append("不注入完整蓝图、kernel 文档或额外工具清单；只使用上面的轻量上下文。")

    if script_skeleton_text:
        instruction.extend([
            "固定脚本骨架 / 动态协议骨架（根据当前 outputs 生成；输出时应补全为可运行源码）：",
            script_skeleton_text,
        ])

    if variant == "minimal":
        instruction.append("极简要求：返回可运行脚本源码，使用 strict JSON argv schema 校验 allowed/required/type/non-empty/unknown keys，真实处理输入，成功时打印满足 stdout_schema 的 JSON object。")

    return _creator_file_generation_messages(
        "\n\n".join(instruction),
        system_rule="你是 Creator 脚本文件生成器。只输出单个目标脚本源码；禁止解释、Markdown fence 或多文件包。",
    )

def _build_generate_file_prompt(
    file_path: str,
    skill_name: str,
    purpose: str,
    blueprint_text: str,
    conversation_history: list[dict],
    role: str | None = None,
    skill_plan_entry: dict[str, Any] | None = None,
) -> list[dict]:
    """Build a minimal generation prompt for a single Skill file.

    The model is asked to output *only* raw file content — no fences, no JSON,
    no explanations.  This maximises reliability for small or unstable models.
    """
    ext = Path(file_path).suffix.lower()
    lang = _LANG_LABELS.get(ext, "文本")

    if file_path.startswith("scripts/"):
        return _build_script_generate_file_prompt_variant(
            file_path=file_path,
            skill_name=skill_name,
            purpose=purpose,
            blueprint_text=blueprint_text,
            role=role,
            skill_plan_entry=skill_plan_entry,
            variant="standard",
        )

    clean_blueprint_text = _clean_blueprint_for_file_prompt(blueprint_text)
    declared_paths = _extract_declared_skill_paths(blueprint_text)
    declared_paths_text = "\n".join(f"- {path}" for path in declared_paths) or "- （蓝图未显式列出资源文件）"
    plan_entry = _skill_plan_entry_for_file(
        file_path=file_path, purpose=purpose, blueprint_text=blueprint_text, role=role, skill_plan_entry=skill_plan_entry
    )
    generated_file_contract_text = _build_generated_file_contract_text(
        file_path, blueprint_text, purpose, role=role, skill_plan_entry=skill_plan_entry
    )
    skill_md_contract_text = generated_file_contract_text if file_path == "SKILL.md" else ""
    skill_md_e2e_authoring_guide = (
        _build_skill_md_e2e_authoring_guide(blueprint_text)
        if file_path == "SKILL.md"
        else ""
    )
    tool_usage_prompt = ""
    script_skeleton_text = _script_generation_skeleton(
        file_path,
        purpose,
        blueprint_text,
        role=plan_entry.role,
        skill_plan_entry=skill_plan_entry,
    ) if file_path.startswith("scripts/") else ""
    kernel_reference_context = _creator_kernel_reference_context()
    plan_summary = (
        f"SkillPlan role：{plan_entry.role}；"
        f"inputs：{', '.join(plan_entry.inputs)}；"
        f"outputs：{', '.join(plan_entry.outputs)}；"
        f"language：{plan_entry.language}；"
        f"runtime：{plan_entry.runtime}；"
        f"command_template：{_script_command_template(file_path, blueprint_text, plan_entry)}；"
        f"forbidden_capabilities：{', '.join(plan_entry.forbidden_capabilities)}"
    )

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
            "4. SKILL.md 第一轮只需生成静态可解析的使用说明和命令块；内部脚本流转由第二轮 E2E 真实执行验证。\n"
            "5. 如果蓝图包含 scripts/ 资源，SKILL.md 正文必须为每个 scripts/ 路径提供一个标准、独立、无缩进的 ```bash fenced code block。\n"
            "6. 每个 bash fenced code block 内只能有一条脚本命令；命令必须直接调用 scripts/ 路径，并在脚本路径后传入一个 JSON object argv。\n"
            "7. 第一条脚本命令只能引用 external envelope 中确定存在的通用字段：user_request、input、text、input_files、files、fields、options，或显式结构化来源提供的字段。\n"
            "8. 如果 Skill 需要业务字段，命令可把 user_request/input/text 或 fields 传给脚本，由脚本自行解析；第一轮不固定中间 stdout 字段名。\n"
            "9. 第一轮只要求命令 JSON argv 静态可解析，并优先引用 external envelope 或显式结构化来源；不要要求证明后续 placeholder 来自前序 stdout。\n"
            "10. JSON argv 必须是 json.loads 可解析的模板；所有动态 placeholder 必须是字符串值；禁止裸写动态数值 placeholder。\n"
            "11. 若需要数值默认值，直接写固定 JSON 数字；不要把动态数值 placeholder 裸露在 JSON 中。\n"
            "12. 批量处理、列表处理或多文件处理应由对应脚本内部完成，SKILL.md 静态说明中不展开自然语言循环。\n"
            "13. 列表或对象字段必须通过整值占位符传递；不要写成由无来源拆分字段拼接的列表。\n"
            "14. 如果蓝图包含 references/ 资源，SKILL.md 正文必须在“参考资料/资源”小节明确引用每个 references/ 路径，并说明何时读取。\n"
            "15. 不要在输出内容的外侧套 ``` 代码块，但 SKILL.md 正文内部必须按需包含标准 ```bash fenced code block。\n"
            "16. 禁止只写‘立即调用 `scripts/...`’这种隐式执行描述；必须写明 assistant 应输出可执行 fenced block。\n"
            "17. 禁止复制 Creator 界面流程、确认清单、‘点击开始创建/开始生成’、系统将自动创建文件等平台创建流程文案。\n"
            "18. 以下宿主 Markdown 执行说明是内部写作约束，只能转化为面向使用者的 Skill 说明，不要逐字复制这些约束或标题。\n"
            "19. 命令中 JSON key 是当前脚本读取的 argv 字段；{{placeholder}} 优先来自 external envelope 或显式 fields/defaults/input binding。内部上游 stdout 字段闭环只在第二轮 E2E 验证。\n"
            "20. 不要在第一轮为下游脚本固定无来源中间字段名；placeholder 来源和修复交给第二轮 E2E。\n"
            "21. 第一轮不要求声明最终 stdout 字段闭环；脚本 stdout 与平台标准输出字段由第二轮 E2E 真实执行验证。\n"
            "22. SKILL.md 必须覆盖蓝图真实规划的任务、真实脚本路径、资源使用、脚本调用顺序（如有）和最终产物类型；不要固定特定中间字段。\n"
            "23. 真实文件计划需要结合蓝图语境判断：目录结构、SkillPlan path、dependencies、references 字段通常是真实文件计划。\n"
            "24. 如果蓝图在“禁止隐式执行/示例/反例/例如/比如”语境中提到某个 scripts/*.py、references/*.md 或 assets/*，它只是解释性示例，不应进入最终 SKILL.md，除非它同时出现在目录结构或 SkillPlan path 中。\n"
            "25. 不要为了满足格式而新增蓝图外脚本；只为蓝图真实规划脚本提供命令块。\n"
            f"{_SKILL_MD_MARKDOWN_EXECUTION_GUIDE}\n\n"
            "以下 SKILL.md first-round static authoring guide 只约束静态格式和平台边界；内部脚本流转交给第二轮 E2E 验证：\n"
            f"{skill_md_e2e_authoring_guide}\n\n"
            "生成前请先隐式检查以下合同，最终输出必须逐项满足；如果合同要求内部 ```bash block，必须在 SKILL.md 正文中写出该 block：\n"
            f"{skill_md_contract_text}\n\n"
            f"蓝图声明的文件路径（必须覆盖对应 scripts/references 要求）：\n{declared_paths_text}\n\n"
            f"以下是已确认的蓝图（已移除 Creator UI 确认文案），你的内容必须与此一致：\n\n{clean_blueprint_text}"
        )
    elif file_path.startswith("scripts/"):
        local_contract = _script_local_contract_payload(
            file_path=file_path,
            purpose=purpose,
            plan_entry=plan_entry,
            stdout_schema=_script_stdout_schema_for_entry(plan_entry),
        )
        instruction = (
            f'你正在为 Skill 包 "{skill_name}" 生成单个脚本文件：{file_path}。\n\n'
            "只输出完整可运行源码本身；禁止 Markdown fence、解释、文件名标题或多文件输出。\n"
            "第一轮只修当前脚本；不要修改或重规划上下游链路，第二轮 E2E 才修整链路。\n"
            "生成前只使用以下轻量上下文：script_goal、inputs、outputs、available_tools、resource_refs、output_contract、rules。\n"
            "统一按 script_composition 理解：根据功能目标组合 argv 输入、本地逻辑和 available_tools；available_tools 只做候选召回，不是最终裁决。\n"
            "工具/helper 如何组合不作为第一轮 hard gate；如 import/dependency、调用、stdout 或 artifact 失败，再修当前脚本。\n"
            "references/assets 只能作为 resource_refs/asset_refs 读取，不能作为 dependencies、allowed_imports 或 pip install 依赖。\n"
            "生成后第一轮只校验协议 + 运行 + 产物：argv JSON、入口、stdout JSON object、required outputs、artifact_created、import/dependency 和危险系统操作。\n\n"
            "轻量脚本上下文：\n"
            f"{json.dumps(local_contract, ensure_ascii=False, indent=2)}\n\n"
            f"固定脚本骨架（仅约束入口/JSON stdout；输出时补全为可运行源码）：\n{script_skeleton_text}"
        )
    elif file_path.startswith("references/"):
        instruction = (
            f'你正在为 Skill 包 "{skill_name}" 生成 {file_path} 参考资料文件。\n\n'
            f"职责说明：{purpose}\n\n"
            "要求：\n"
            "1. 只输出 Markdown 文档内容，不要额外的说明文字。\n"
            "2. 不要在文档外套 ``` 代码块。\n"
            "3. 内容应是有实际指导价值的参考资料，不是对参考资料的再描述。\n"
            "生成前请先隐式检查以下 reference 合同，最终输出必须逐项满足：\n"
            f"{generated_file_contract_text}\n\n"
            f"蓝图声明的文件路径：\n{declared_paths_text}\n\n"
            f"以下是已确认的蓝图（参考资料职责说明见 references/ 部分）：\n\n{clean_blueprint_text}"
        )
    elif file_path.startswith("assets/"):
        instruction = (
            f'你正在为 Skill 包 "{skill_name}" 生成 {file_path} 资源文件。\n\n'
            f"职责说明：{purpose}\n\n"
            "要求：\n"
            f"1. 只输出 {lang} 格式的文件内容，不要任何说明文字。\n"
            "2. 不要用 ``` 代码块包裹输出。\n"
            "生成前请先隐式检查以下 asset 合同，最终输出必须逐项满足：\n"
            f"{generated_file_contract_text}\n\n"
            f"蓝图声明的文件路径：\n{declared_paths_text}\n\n"
            f"以下是已确认的蓝图：\n\n{clean_blueprint_text}"
        )
    else:
        instruction = (
            f'你正在为 Skill 包 "{skill_name}" 生成 {file_path} 文件。\n\n'
            f"职责说明：{purpose}\n\n"
            "要求：直接输出文件内容，不要任何解释，不要 Markdown 代码块包裹。\n\n"
            f"蓝图声明的文件路径：\n{declared_paths_text}\n\n"
            f"蓝图：\n\n{clean_blueprint_text}"
        )

    messages: list[dict] = _creator_file_generation_messages(instruction)

    if file_path.startswith("scripts/"):
        # Scripts are generated from a short system rule plus a user-visible concrete task.
        # Do not append conversation history: recent Creator UI
        # copy (file-list previews, confirmation instructions, panel messages)
        # has repeatedly polluted first-pass script output.
        return messages

    # Include recent user context but skip Creator UI confirmation text. Assistant
    # blueprint confirmations often contain "click Start" operational prose that
    # must never be copied into generated files.
    for msg in conversation_history[-_MAX_HISTORY_TURNS:]:
        if not isinstance(msg, dict) or msg.get("role") not in {"user", "assistant"}:
            continue
        content = str(msg.get("content") or "")
        if msg.get("role") == "assistant" and _CREATOR_FLOW_LEAK_RE.search(content):
            continue
        messages.append({**msg, "content": _clean_blueprint_for_file_prompt(content)})

    return messages



def _local_blueprint_text_for_path(path: str, blueprint_text: str, *, window: int = 600) -> str:
    text = blueprint_text or ""
    idx = text.find(path)
    if idx < 0:
        return ""
    return text[max(0, idx - window): min(len(text), idx + len(path) + window)]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

__all__ = [name for name in globals() if not name.startswith("__")]
