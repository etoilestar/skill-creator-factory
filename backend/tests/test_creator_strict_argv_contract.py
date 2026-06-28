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
    with pytest.raises(ValueError, match="payload.get"):
        _validate(code)


def test_strict_argv_guard_rejects_missing_unknown_key_guard():
    code = STRICT_OK.replace('''    unknown = set(payload) - ALLOWED_KEYS
    if unknown:
        raise ValueError(f"unknown keys: {sorted(unknown)}")
''', '')
    with pytest.raises(ValueError, match="unknown argv keys"):
        _validate(code)


def test_strict_argv_guard_rejects_missing_required_declaration():
    code = STRICT_OK.replace('REQUIRED_KEYS = {"text", "style"}', 'NEEDED_KEYS = {"text", "style"}')
    with pytest.raises(ValueError, match="required keys"):
        _validate(code)
