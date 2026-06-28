import pytest

from backend.services.creator.contracts import _validate_script_contract_static


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

ALLOWED_KEYS = {"text", "style"}
REQUIRED_KEYS = {"text", "style"}
EXPECTED_TYPES = {"text": str, "style": str}

def parse_args():
    if len(sys.argv) != 2:
        raise ValueError("missing JSON argv")
    payload = json.loads(sys.argv[1])
    if not isinstance(payload, dict):
        raise ValueError("argv JSON must be an object")
    unknown = set(payload) - ALLOWED_KEYS
    if unknown:
        raise ValueError(f"unknown keys: {sorted(unknown)}")
    missing = REQUIRED_KEYS - set(payload)
    if missing:
        raise ValueError(f"missing required keys: {sorted(missing)}")
    validated = {}
    for key in REQUIRED_KEYS:
        value = payload[key]
        if value is None or value == "" or value == [] or value == {}:
            raise ValueError(f"empty required value: {key}")
        if not isinstance(value, EXPECTED_TYPES[key]):
            raise TypeError(f"invalid type: {key}")
        validated[key] = value
    return validated

def run(args):
    return {"result": args["text"] + args["style"]}

def main():
    print(json.dumps(run(parse_args()), ensure_ascii=False))

if __name__ == "__main__":
    main()
'''


def test_strict_argv_guard_accepts_explicit_schema():
    _validate(STRICT_OK)


def test_strict_argv_guard_rejects_payload_get_default():
    code = STRICT_OK.replace('return {"result": args["text"] + args["style"]}', 'return {"result": args.get("text", "fallback")}')
    with pytest.raises(ValueError, match="required argv keys"):
        _validate(code)


def test_strict_argv_guard_rejects_missing_unknown_key_guard():
    code = STRICT_OK.replace('''    unknown = set(payload) - ALLOWED_KEYS
    if unknown:
        raise ValueError(f"unknown keys: {sorted(unknown)}")
''', '')
    with pytest.raises(ValueError, match="unknown argv keys"):
        _validate(code)


DIRECT_GUARD_OK = r'''
import json
import sys

def parse_args():
    if len(sys.argv) != 2:
        raise ValueError("missing JSON argv")
    payload = json.loads(sys.argv[1])
    if not isinstance(payload, dict):
        raise ValueError("argv JSON must be an object")
    if set(payload) != {"text", "style"}:
        unknown = set(payload) - {"text", "style"}
        missing = {"text", "style"} - set(payload)
        if unknown:
            raise ValueError(f"unknown argv keys: {sorted(unknown)}")
        if missing:
            raise ValueError(f"missing required argv keys: {sorted(missing)}")
    text = payload["text"]
    style = payload["style"]
    if text is None or text == "" or text == [] or text == {}:
        raise ValueError("empty required argv value: text")
    if style is None or style == "" or style == [] or style == {}:
        raise ValueError("empty required argv value: style")
    if not isinstance(text, str):
        raise TypeError("invalid argv type for text")
    if not isinstance(style, str):
        raise TypeError("invalid argv type for style")
    return {"text": text, "style": style}

def run(args):
    return {"result": args["text"] + args["style"]}

def main():
    print(json.dumps(run(parse_args()), ensure_ascii=False))

if __name__ == "__main__":
    main()
'''


def test_strict_argv_guard_accepts_direct_core_bound_validation_without_schema_constants():
    _validate(DIRECT_GUARD_OK)

from types import SimpleNamespace
from backend.services.creator import e2e
from backend.services.creator.contracts import extract_python_strict_argv_schema
from backend.services.creator.common import E2EWorkflowCommand


def test_strict_argv_guard_rejects_schema_placeholders():
    bad_cases = [
        'ALLOWED_KEYS = set(...)',
        'REQUIRED_KEYS = set(...)',
        'EXPECTED_TYPES = {...}',
        'ALLOWED_KEYS = "TODO schema"',
        'ALLOWED_KEYS = {"input_text"}',
    ]
    for replacement in bad_cases:
        code = STRICT_OK.replace('ALLOWED_KEYS = {"text", "style"}', replacement)
        if replacement.startswith('REQUIRED'):
            code = STRICT_OK.replace('REQUIRED_KEYS = {"text", "style"}', replacement)
        if replacement.startswith('EXPECTED'):
            code = STRICT_OK.replace('EXPECTED_TYPES = {"text": str, "style": str}', replacement)
        with pytest.raises(ValueError):
            _validate(code)


def test_strict_argv_guard_rejects_get_default_for_required_keys_only():
    for expr in ['payload.get("text", "fallback")', 'args.get("text", "fallback")', 'data.get("text") or "fallback"']:
        code = STRICT_OK.replace('return {"result": args["text"] + args["style"]}', f'return {{"result": {expr}}}')
        with pytest.raises(ValueError, match="required argv keys"):
            _validate(code)


def test_strict_argv_guard_allows_schema_declared_optional_default_get():
    code = STRICT_OK.replace('REQUIRED_KEYS = {"text", "style"}', 'REQUIRED_KEYS = {"text"}\nOPTIONAL_KEYS = {"style"}\nDEFAULT_VALUES = {"style": "plain"}')
    code = code.replace('return {"result": args["text"] + args["style"]}', 'return {"result": args["text"] + args.get("style", "plain")}')
    _validate(code)


def _argv_details(stderr, *, inputs, rendered, allowed='ALLOWED_KEYS = {"text"}', required='REQUIRED_KEYS = {"text"}', expected='EXPECTED_TYPES = {"text": str}'):
    code = STRICT_OK.replace('ALLOWED_KEYS = {"text", "style"}', allowed).replace('REQUIRED_KEYS = {"text", "style"}', required).replace('EXPECTED_TYPES = {"text": str, "style": str}', expected)
    return e2e._classify_argv_schema_failure(
        command=E2EWorkflowCommand(1, 'SKILL.md', 'scripts/main.py', 'python scripts/main.py {}', 'python', rendered),
        content=code,
        entry=SimpleNamespace(runtime='python', inputs=inputs),
        rendered_payload=rendered,
        stdout='',
        stderr=stderr,
    )


def test_argv_schema_attribution_skill_md_extra_key():
    details = _argv_details("ValueError: unknown keys: ['extra']", inputs=['text'], rendered={'text': 'ok', 'extra': 'x'})
    assert details['primary_target'] == 'SKILL.md'
    assert details['argv_schema_error_kind'] == 'unknown_key'


def test_argv_schema_attribution_script_allowed_underdeclared_semantic_key():
    details = _argv_details("ValueError: unknown keys: ['text']", inputs=['text'], rendered={'text': 'ok'}, allowed='ALLOWED_KEYS = set()')
    assert details['primary_target'] == 'scripts/main.py'


def test_argv_schema_attribution_script_required_too_broad():
    details = _argv_details("ValueError: missing required keys: ['extra']", inputs=['text'], rendered={'text': 'ok'}, required='REQUIRED_KEYS = {"text", "extra"}')
    assert details['primary_target'] == 'scripts/main.py'


def test_argv_schema_attribution_uncertain_has_candidate_targets():
    details = _argv_details("ValueError: unknown argv schema error", inputs=[], rendered={'mystery': 'ok'}, allowed='ALLOWED_KEYS = {"other"}')
    assert details['candidate_targets'] == ['SKILL.md', 'scripts/main.py']
    assert details['primary_target'] == 'scripts/main.py'


def test_extract_schema_supports_arg_schema_dict():
    schema = extract_python_strict_argv_schema('ARG_SCHEMA = {"allowed_keys": {"a", "b"}, "required_keys": {"a"}, "expected_types": {"a": str}}')
    assert schema['allowed_keys'] == ['a', 'b']
    assert schema['required_keys'] == ['a']
    assert schema['expected_types'] == {'a': 'str'}


def test_extract_schema_supports_optional_and_defaulted_keys():
    schema = extract_python_strict_argv_schema('OPTIONAL_KEYS = {"style"}\nDEFAULT_VALUES = {"style": "plain"}')
    assert schema['optional_keys'] == ['style']
    assert schema['defaulted_keys'] == ['style']
