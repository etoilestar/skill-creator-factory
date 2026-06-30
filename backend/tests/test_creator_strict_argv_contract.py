from types import SimpleNamespace

import pytest

from backend.services.creator import e2e
from backend.services.creator.common import E2EWorkflowCommand
from backend.services.creator.contracts import _validate_script_contract_static, extract_python_strict_argv_schema
from backend.services.creator_tool_registry import get_tool_capability, resolve_tools_for_skill_plan_entry
from backend.services.runtime_tools import strict_json_argv_guard
from backend.services.skill_plan import SkillPlanEntry


SKILL_MD = '''# Test Skill

```bash
python scripts/main.py '{"text":"hello","style":"plain"}'
```
'''


def _validate(code: str) -> None:
    _validate_script_contract_static(file_path="scripts/main.py", content=code, skill_md=SKILL_MD)


STRICT_OK = r'''
import json
import sys
from backend.services.runtime_tools import strict_json_argv_guard

def parse_args():
    if len(sys.argv) != 2:
        raise ValueError("missing JSON argv")
    payload = json.loads(sys.argv[1])
    return strict_json_argv_guard(payload, {
        "text": {"type": str, "required": True},
        "style": {"type": str, "required": True},
    })

def run(args):
    return {"result": args["text"] + args["style"]}

def main():
    print(json.dumps(run(parse_args()), ensure_ascii=False))

if __name__ == "__main__":
    main()
'''


def test_runtime_tools_exports_strict_json_argv_guard():
    assert strict_json_argv_guard({"text": "ok"}, {"text": {"type": str}}) == {"text": "ok"}


def test_strict_json_argv_guard_supports_string_type_aliases():
    payload = {"name": "Ada", "count": 2, "items": ["a"], "meta": {"ok": True}}
    spec = {
        "name": {"type": "string"},
        "count": {"type": "integer"},
        "items": {"type": "array"},
        "meta": {"type": "object"},
    }
    assert strict_json_argv_guard(payload, spec) == payload
    with pytest.raises(TypeError, match="unknown argv type alias"):
        strict_json_argv_guard({"name": "Ada"}, {"name": {"type": "unsupported"}})


def test_strict_json_argv_guard_failures_and_no_input():
    spec = {"text": {"type": str, "required": True}}
    with pytest.raises(ValueError, match="unknown argv keys"):
        strict_json_argv_guard({"text": "ok", "extra": 1}, spec)
    with pytest.raises(ValueError, match="missing required argv keys"):
        strict_json_argv_guard({}, spec)
    with pytest.raises(ValueError, match="empty required argv value"):
        strict_json_argv_guard({"text": ""}, spec)
    with pytest.raises(TypeError, match="invalid argv type"):
        strict_json_argv_guard({"text": 1}, spec)
    assert strict_json_argv_guard({}, {}) == {}
    with pytest.raises(ValueError, match="unknown argv keys"):
        strict_json_argv_guard({"extra": 1}, {})


def test_creator_tool_registry_has_mandatory_script_argv_guard():
    cap = get_tool_capability("script_argv_guard")
    assert cap is not None
    assert cap.usage_policy == "helper_required"
    assert "strict_json_argv_guard" in cap.helper_imports


def test_resolve_tools_injects_guard_for_python_scripts_without_affecting_business_tools():
    entry = SkillPlanEntry(path="scripts/main.py", role="generic_script", file_type="script", purpose="x", runtime="python", inputs=[], outputs=["result"])
    result = resolve_tools_for_skill_plan_entry(entry)
    assert "script_argv_guard" in result.allowed_tools
    assert "strict_json_argv_guard" in result.allowed_helper_imports
    assert any("strict_json_argv_guard" in card for card in result.tool_function_cards)
    assert any(item.get("tool") == "script_argv_guard" for item in result.tool_snippets)

    for path_attr in ("path", "file_path", "script_path"):
        data = {"role": "custom_role", "runtime": "python", path_attr: "scripts/alt.py", "outputs": ["result"]}
        result = resolve_tools_for_skill_plan_entry(data)
        assert "script_argv_guard" in result.allowed_tools
        assert "strict_json_argv_guard" in result.allowed_helper_imports


def test_strict_argv_guard_accepts_mandatory_helper_call():
    _validate(STRICT_OK)


def test_missing_guard_import_or_call_fails_first_round():
    with pytest.raises(ValueError, match="import strict_json_argv_guard"):
        _validate(STRICT_OK.replace("from backend.services.runtime_tools import strict_json_argv_guard\n", ""))
    with pytest.raises(ValueError, match="call strict_json_argv_guard"):
        _validate(STRICT_OK.replace("return strict_json_argv_guard(payload, {", "return ({"))


def test_run_reparse_or_direct_payload_use_fails_first_round():
    reparsing = STRICT_OK.replace('return {"result": args["text"] + args["style"]}', 'payload = json.loads(sys.argv[1])\n    return {"result": payload["text"]}')
    with pytest.raises(ValueError, match="re-parse"):
        _validate(reparsing)
    direct_payload = STRICT_OK.replace('def run(args):\n    return {"result": args["text"] + args["style"]}', 'def run(payload):\n    return {"result": payload["text"]}')
    with pytest.raises(ValueError, match="unvalidated payload"):
        _validate(direct_payload)


def test_spec_placeholders_fail_first_round():
    for bad_key in ["input_text", "example", "TODO"]:
        code = STRICT_OK.replace('"text": {"type": str, "required": True}', f'"{bad_key}": {{"type": str, "required": True}}')
        with pytest.raises(ValueError):
            _validate(code)


def test_required_get_default_still_fails_when_schema_declares_required_key():
    code = STRICT_OK.replace('return {"result": args["text"] + args["style"]}', 'return {"result": args.get("text", "fallback")}')
    # Compatibility AST extraction can still catch schema-like required declarations when present.
    schema_code = 'REQUIRED_KEYS = {"text"}\n' + code
    with pytest.raises(ValueError, match="required argv keys"):
        _validate(schema_code)


def test_extract_schema_supports_arg_schema_and_defaults_for_attribution():
    schema = extract_python_strict_argv_schema('ARG_SCHEMA = {"allowed_keys": {"a", "b"}, "required_keys": {"a"}, "expected_types": {"a": str}}\nOPTIONAL_KEYS={"b"}\nDEFAULT_VALUES={"b": 1}')
    assert schema["allowed_keys"] == ["a", "b"]
    assert schema["required_keys"] == ["a"]
    assert schema["optional_keys"] == ["b"]
    assert schema["defaulted_keys"] == ["b"]
    assert schema["expected_types"] == {"a": "str"}


def _argv_details(stderr, *, inputs, rendered, allowed='ALLOWED_KEYS = {"text"}', required='REQUIRED_KEYS = {"text"}', expected='EXPECTED_TYPES = {"text": str}', content=None):
    content = content or (STRICT_OK + "\n" + allowed + "\n" + required + "\n" + expected)
    return e2e._classify_argv_schema_failure(
        command=E2EWorkflowCommand(1, "SKILL.md", "scripts/main.py", "python scripts/main.py {}", "python", rendered),
        content=content,
        entry=SimpleNamespace(runtime="python", inputs=inputs),
        rendered_payload=rendered,
        stdout="",
        stderr=stderr,
    )


def test_argv_schema_attribution_targets():
    assert _argv_details("ValueError: unknown argv keys: ['extra']", inputs=["text"], rendered={"text": "ok", "extra": "x"})["primary_target"] == "SKILL.md"
    assert _argv_details("ValueError: unknown argv keys: ['text']", inputs=["text"], rendered={"text": "ok"}, allowed='ALLOWED_KEYS = set()')["primary_target"] == "scripts/main.py"
    missing = _argv_details("ValueError: missing required argv keys: ['extra']", inputs=["text"], rendered={"text": "ok"}, required='REQUIRED_KEYS = {"text", "extra"}')
    assert missing["primary_target"] == "scripts/main.py"
    assert missing["candidate_targets"] == ["scripts/main.py"]
    assert "not self-consistent" in missing["target_reason"]
    uncertain = _argv_details("ValueError: unknown argv schema error", inputs=[], rendered={"mystery": "ok"}, allowed='ALLOWED_KEYS = {"other"}')
    assert uncertain["primary_target"] == "SKILL.md"
    assert uncertain["candidate_targets"] == ["SKILL.md", "scripts/main.py"]
    assert "do not blindly modify" in uncertain["target_reason"]


def test_argv_schema_prefers_skill_md_when_script_interface_self_consistent():
    content = 'ALLOWED_KEYS = {"input_text"}\nREQUIRED_KEYS = {"input_text"}\ndef run(argv):\n    return {"text": argv.get("input_text")}\n'
    details = _argv_details(
        "ValueError: missing required argv keys: ['input_text']",
        inputs=["input_text"],
        rendered={"title": "wrong"},
        content=content,
    )
    assert details["primary_target"] == "SKILL.md"


def test_argv_schema_targets_script_when_guard_and_run_keys_disagree():
    content = 'ALLOWED_KEYS = {"input_text"}\nREQUIRED_KEYS = {"input_text"}\ndef run(argv):\n    return {"text": argv.get("title")}\n'
    details = _argv_details(
        "ValueError: missing required argv keys: ['input_text']",
        inputs=["input_text"],
        rendered={"title": "wrong"},
        content=content,
    )
    assert details["primary_target"] == "scripts/main.py"
    assert details["script_guard_run_mismatch"] is True

from backend.services.creator.generation import _script_generation_skeleton


def test_generation_skeleton_uses_mandatory_guard_import_call_not_inline_validate():
    skeleton = _script_generation_skeleton("scripts/main.py", "test", "", skill_plan_entry={"path": "scripts/main.py", "runtime": "python", "inputs": ["input_text"], "outputs": ["result"]})
    assert "from backend.services.runtime_tools import strict_json_argv_guard" in skeleton
    assert "strict_json_argv_guard(payload" in skeleton
    assert "def validate_payload" not in skeleton


def test_argv_schema_repair_instruction_treats_guard_as_probe():
    instruction = e2e._argv_schema_repair_instruction(
        "scripts/main.py",
        {"primary_target": "scripts/main.py", "candidate_targets": ["SKILL.md", "scripts/main.py"], "target_reason": "x"},
    )
    assert "strict_json_argv_guard 是接口不对齐探针" in instruction
    assert "禁止只改 guard schema" in instruction
    assert "不要只修 guard" in instruction
    assert "只修当前脚本 mandatory argv guard import/call 或 guard spec" not in instruction


def test_e2e_script_target_rule_contains_coverage_guardrail():
    source = e2e._repair_existing_file_for_e2e_failure.__code__.co_consts
    joined = "\n".join(str(item) for item in source if isinstance(item, str))
    assert "strict_json_argv_guard 是接口不对齐探针" in joined
    assert "不能通过删除参数降低功能覆盖面" in joined
