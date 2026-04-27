"""Clean, validate, and persist user input via state_machine."""
import re
from typing import Tuple

from . import state_machine

_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,62}[a-z0-9]$|^[a-z0-9]$")
_REQUIRED_FIELDS = {"name", "description"}
_MAX_DESCRIPTION_LEN = 1024
_MAX_FIELD_LEN = 2048


def _clean(value: str) -> str:
    """Strip whitespace and remove control characters."""
    value = value.strip()
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    return value


def validate_input(field_name: str, value: str) -> Tuple[bool, str]:
    """Validate a field value.
    
    Returns (is_valid, error_message). error_message is "" on success.
    """
    value = _clean(value)

    if field_name in _REQUIRED_FIELDS and not value:
        return False, f"字段 '{field_name}' 不能为空"

    if len(value) > _MAX_FIELD_LEN:
        return False, f"字段 '{field_name}' 超过最大长度 {_MAX_FIELD_LEN}"

    if field_name == "name":
        if not _SKILL_ID_RE.match(value):
            return False, ("技能名称只能包含小写字母、数字和连字符，"
                           "首尾不能是连字符，最长 64 字符")
        if len(value) > 64:
            return False, "技能名称最长 64 字符"

    if field_name == "description" and len(value) > _MAX_DESCRIPTION_LEN:
        return False, f"描述最长 {_MAX_DESCRIPTION_LEN} 字符"

    return True, ""


def save_user_input(session_id: str, field_name: str, value: str) -> Tuple[bool, str]:
    """Validate and persist input. Returns (success, error_message)."""
    value = _clean(value)
    ok, err = validate_input(field_name, value)
    if not ok:
        return False, err
    state_machine.save_field_data(session_id, field_name, value)
    return True, ""
