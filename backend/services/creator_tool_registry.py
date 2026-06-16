"""Creator tool capability registry.

This module is the single source of truth for Creator-facing tool metadata
and default role mappings.  The sandbox/runtime execution path is kept separate:
this registry describes what Creator may plan, prompt and expose through
management APIs.  Runtime helpers and deep source validators are intentionally
implemented in follow-up modules; tool status reports whether registered helper
names are exported by ``backend.services.skill_runtime``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields as dataclasses_fields, replace
from datetime import datetime, timezone
import subprocess
import asyncio
import ast
import base64
import importlib
import importlib.util
import json
import os, sys
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Literal


UsagePolicy = Literal["helper_required", "helper_preferred", "self_implementation_allowed"]



_TOOL_AUTHORING_CONFIG_STORE: dict[str, dict[str, Any]] = {}
_TOOL_AUTHORING_CONFIG_LOADED = False


def _tool_authoring_config_store_path() -> Path:
    raw = os.environ.get("TOOL_AUTHORING_CONFIG_STORE_PATH", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path(__file__).resolve().parents[1] / "config" / "tool_authoring_configs.json").resolve()


def _load_tool_authoring_config_store_from_disk() -> None:
    """Load saved authoring configs and restore env values.

    This intentionally persists plaintext local dev secrets because custom tools
    need to keep working after container restart. For production, replace this
    with KMS/Vault/DB encrypted secret storage.
    """
    global _TOOL_AUTHORING_CONFIG_LOADED
    if _TOOL_AUTHORING_CONFIG_LOADED:
        return

    _TOOL_AUTHORING_CONFIG_LOADED = True
    path = _tool_authoring_config_store_path()
    if not path.exists():
        return

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return

    sessions = payload.get("sessions") if isinstance(payload, dict) else {}
    if not isinstance(sessions, dict):
        return

    for session_id, record in sessions.items():
        if not isinstance(record, dict):
            continue

        key = _tool_config_session_id({"session_id": session_id})
        _TOOL_AUTHORING_CONFIG_STORE[key] = record

        raw_values = record.get("raw_values") if isinstance(record.get("raw_values"), dict) else {}
        for env_name, value in raw_values.items():
            if env_name and value not in (None, ""):
                os.environ[str(env_name)] = str(value)


def _persist_tool_authoring_config_store_to_disk() -> None:
    path = _tool_authoring_config_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "sessions": _TOOL_AUTHORING_CONFIG_STORE,
    }

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)

    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

def _infer_wrapper_family(request: dict[str, Any]) -> str:
    """Infer wrapper family from explicit runtime contract only.

    This intentionally does not use tool_kind or needs_external_network to select
    a wrapper. Those are semantic hints. The planner/validator/repair loop must
    convert them into required_capabilities first.
    """
    request = request if isinstance(request, dict) else {}

    explicit = str(
        request.get("wrapper_family")
        or request.get("adapter_family")
        or request.get("runtime_family")
        or ""
    ).strip()

    if explicit:
        return _canonical_wrapper_family(explicit)

    manifest = request.get("manifest") if isinstance(request.get("manifest"), dict) else {}

    manifest_family = str(
        manifest.get("wrapper_family")
        or manifest.get("adapter_family")
        or manifest.get("runtime_family")
        or ""
    ).strip()

    if manifest_family:
        return _canonical_wrapper_family(manifest_family)

    required_capabilities: list[str] = []
    for source in (request, manifest):
        raw = source.get("required_capabilities") if isinstance(source, dict) else None
        if isinstance(raw, list):
            required_capabilities.extend(str(item).strip() for item in raw if str(item).strip())

    for capability in required_capabilities:
        wrapper = CAPABILITY_TO_WRAPPER.get(capability)
        if wrapper:
            return _canonical_wrapper_family(wrapper)

    config = request.get("config") if isinstance(request.get("config"), dict) else {}

    if any(config.get(key) not in (None, "", {}, []) for key in ("base_url", "endpoint")):
        return "http_api"

    if any(manifest.get(key) not in (None, "", {}, []) for key in ("base_url", "endpoint")):
        return "http_api"

    if any(config.get(key) not in (None, "", {}, []) for key in ("database_url", "dsn", "connection_string")):
        return "database_query"

    if any(manifest.get(key) not in (None, "", {}, []) for key in ("database_url", "dsn", "connection_string")):
        return "database_query"

    if request.get("generates_file") or manifest.get("generates_file"):
        return "file_io"

    code = str(request.get("code_block") or request.get("adapter_code") or "")
    facts = extract_runtime_facts(code, manifest) if code.strip() or manifest else {}

    if facts.get("has_configurable_endpoint"):
        return "http_api"

    if code.strip():
        return "python_compute"

    # Do not use needs_external_network here. External network is a capability,
    # not a wrapper selector.
    return "python_compute"

def _restore_env_from_authoring_record(record: dict[str, Any] | None) -> None:
    if not isinstance(record, dict):
        return
    raw_values = record.get("raw_values") if isinstance(record.get("raw_values"), dict) else {}
    for env_name, value in raw_values.items():
        if env_name and value not in (None, ""):
            os.environ[str(env_name)] = str(value)
_SENSITIVE_CONFIG_KEYS = ("api_key", "apikey", "token", "password", "secret", "authorization", "credential")


def _tool_config_session_id(payload: dict[str, Any] | None = None) -> str:
    payload = payload or {}
    seed = str(payload.get("session_id") or payload.get("tool_name") or payload.get("operation") or payload.get("description") or "default")
    slug = _slug(seed) if "_slug" in globals() else re.sub(r"[^a-z0-9]+", "_", seed.lower()).strip("_")
    return slug or "default"

def _tool_platform_env_prefix(tool_name: str) -> str:
    prefix = re.sub(r"[^A-Za-z0-9]+", "_", (tool_name or "TOOL")).strip("_").upper()
    return f"TOOLCFG_{prefix or 'TOOL'}"


def _tool_platform_envs(tool_name: str) -> dict[str, str]:
    prefix = _tool_platform_env_prefix(tool_name)
    return {
        "prefix": prefix,
        "base_url": f"{prefix}_BASE_URL",
        "method": f"{prefix}_METHOD",
        "secret": f"{prefix}_SECRET",
        "auth_header": f"{prefix}_AUTH_HEADER",
        "auth_query_param": f"{prefix}_AUTH_QUERY_PARAM",
        "body_template": f"{prefix}_BODY_TEMPLATE_JSON",
        "query_template": f"{prefix}_QUERY_TEMPLATE_JSON",
        "headers_template": f"{prefix}_HEADERS_TEMPLATE_JSON",
    }

def _tool_platform_envs_from_prefix(prefix: str) -> dict[str, str]:
    prefix = str(prefix or "").strip()
    if not prefix:
        prefix = "TOOLCFG_TOOL"
    return {
        "prefix": prefix,
        "base_url": f"{prefix}_BASE_URL",
        "method": f"{prefix}_METHOD",
        "secret": f"{prefix}_SECRET",
        "auth_header": f"{prefix}_AUTH_HEADER",
        "auth_query_param": f"{prefix}_AUTH_QUERY_PARAM",
        "body_template": f"{prefix}_BODY_TEMPLATE_JSON",
        "query_template": f"{prefix}_QUERY_TEMPLATE_JSON",
        "headers_template": f"{prefix}_HEADERS_TEMPLATE_JSON",
    }


def _env_ref_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    match = _ENV_REF_RE.fullmatch(value.strip())
    return match.group(1) if match else ""


def _resolve_env_ref_or_value(value: Any, default: str = "") -> str:
    ref = _env_ref_name(value)
    if ref:
        return os.environ.get(ref, default)
    if value in (None, "", {}, []):
        return default
    return str(value)


def _find_saved_authoring_record_for_request(
    request: dict[str, Any],
    manifest: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Best-effort single-user lookup for saved authoring config.

    This fixes prefix drift between save-time tool_name and generate-time manifest.name.
    """
    _load_tool_authoring_config_store_from_disk()

    manifest = manifest or {}
    plan = plan or {}

    candidates: list[str] = []
    for value in (
        request.get("session_id"),
        request.get("tool_name"),
        request.get("operation"),
        manifest.get("name"),
        manifest.get("display_name"),
        plan.get("operation"),
    ):
        if value:
            candidates.append(_tool_config_session_id({"session_id": str(value)}))
            candidates.append(_tool_config_session_id({"tool_name": str(value)}))

    for key in candidates:
        record = _TOOL_AUTHORING_CONFIG_STORE.get(key)
        if isinstance(record, dict):
            _restore_env_from_authoring_record(record)
            return record

    names = {
        str(item or "").strip()
        for item in (
            request.get("tool_name"),
            request.get("operation"),
            manifest.get("name"),
            manifest.get("display_name"),
            plan.get("operation"),
        )
        if str(item or "").strip()
    }

    for record in _TOOL_AUTHORING_CONFIG_STORE.values():
        if not isinstance(record, dict):
            continue
        stored_tool_name = str(record.get("tool_name") or "").strip()
        if stored_tool_name and stored_tool_name in names:
            _restore_env_from_authoring_record(record)
            return record

    # 单用户最小策略：如果只有一个已保存配置，直接用它，避免 tool_name/manifest.name 漂移。
    records = [item for item in _TOOL_AUTHORING_CONFIG_STORE.values() if isinstance(item, dict)]
    if len(records) == 1:
        _restore_env_from_authoring_record(records[0])
        return records[0]

    return {}


def _json_env(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _load_json_env(env_name: str, default: Any = None) -> Any:
    raw = os.environ.get(env_name, "")
    if not raw:
        return {} if default is None else default
    try:
        return json.loads(raw)
    except Exception:
        return {} if default is None else default

def _is_sensitive_config_key(key: str) -> bool:
    lowered = (key or "").lower()
    return any(token in lowered for token in _SENSITIVE_CONFIG_KEYS)


def _build_saved_auth_config(data: dict[str, Any], raw_config: dict[str, Any], auth_type: str, secret_env: str) -> dict[str, Any]:
    if auth_type in {"", "none", "no_auth", "anonymous"} or not secret_env:
        return {}
    existing = raw_config.get("auth") if isinstance(raw_config.get("auth"), dict) else {}
    normalized_type = "token" if auth_type in {"token", "bearer"} else "api_key" if auth_type in {"api_key", "key"} else auth_type
    placement = str(data.get("auth_placement") or raw_config.get("auth_placement") or existing.get("placement") or "").strip().lower()
    if normalized_type == "token":
        placement = placement or "bearer"
    elif normalized_type == "api_key":
        placement = placement or "header"
    elif normalized_type == "basic":
        placement = placement or "header"
    auth: dict[str, Any] = {"type": normalized_type, "env": secret_env, "placement": placement}
    header_name = str(data.get("auth_header_name") or raw_config.get("auth_header_name") or existing.get("header_name") or "").strip()
    query_param = str(data.get("auth_query_param") or raw_config.get("auth_query_param") or existing.get("query_param") or "").strip()
    if normalized_type == "api_key":
        if placement == "query":
            auth["query_param"] = query_param or "api_key"
        elif placement == "bearer":
            auth["header_name"] = "Authorization"
            auth["scheme"] = "Bearer"
        else:
            auth["placement"] = "header"
            auth["header_name"] = header_name or "X-API-KEY"
    elif normalized_type == "token":
        auth["placement"] = "header"
        auth["header_name"] = header_name or "Authorization"
        auth["scheme"] = str(existing.get("scheme") or raw_config.get("auth_scheme") or "Bearer").strip() or "Bearer"
    elif normalized_type == "basic":
        auth["placement"] = "header"
        auth["header_name"] = "Authorization"
        auth["scheme"] = "Basic"
    elif normalized_type == "custom":
        auth["placement"] = placement or "custom"
    return auth


def save_tool_authoring_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Save authoring configuration and persist it.

    Single-user minimal version:
    - user-provided secret_env is treated only as source alias;
    - generated adapters read platform env names like TOOLCFG_<TOOL>_SECRET;
    - key/base_url/method/templates are persisted and restored into os.environ;
    - auth_override is persisted as user intent metadata, not as a secret.
    """
    _load_tool_authoring_config_store_from_disk()

    data = dict(payload or {})
    session_id = _tool_config_session_id(data)
    existing = _TOOL_AUTHORING_CONFIG_STORE.get(session_id) or {}

    tool_name = str(
        data.get("tool_name")
        or data.get("operation")
        or existing.get("tool_name")
        or session_id
        or "tool"
    )

    raw_config = data.get("config") if isinstance(data.get("config"), dict) else {}
    existing_raw_values = (
        existing.get("raw_values")
        if isinstance(existing.get("raw_values"), dict)
        else {}
    )
    raw_values: dict[str, str] = dict(existing_raw_values)

    auth_override = _auth_override_from_request(data)

    envs = _tool_platform_envs(tool_name)

    config_refs: dict[str, str] = {}
    configured_env: list[str] = []
    configured_secrets: list[str] = []

    def put_env(
        env_name: str,
        value: Any,
        *,
        secret: bool = False,
        keep_old_if_empty: bool = True,
    ) -> str | None:
        name = str(env_name or "").strip()
        if not name:
            return None

        if value in (None, "", {}, []):
            if keep_old_if_empty and raw_values.get(name) not in (None, ""):
                os.environ[name] = str(raw_values[name])
                (configured_secrets if secret else configured_env).append(name)
                return name
            return None

        raw = str(value).strip() if isinstance(value, str) else str(value)
        os.environ[name] = raw
        raw_values[name] = raw
        (configured_secrets if secret else configured_env).append(name)
        return name

    base_url_value = (
        data.get("base_url")
        or raw_config.get("base_url")
        or raw_config.get("endpoint")
        or raw_config.get("url")
    )
    if put_env(envs["base_url"], base_url_value):
        config_refs["base_url"] = f"${{ENV:{envs['base_url']}}}"

    method_value = (
        str(data.get("method") or raw_config.get("method") or "GET")
        .strip()
        .upper()
        or "GET"
    )
    if put_env(envs["method"], method_value):
        config_refs["method"] = f"${{ENV:{envs['method']}}}"

    auth_type = (
        str(
            data.get("auth_type")
            or raw_config.get("auth_type")
            or raw_config.get("authentication")
            or "none"
        )
        .strip()
        .lower()
        or "none"
    )

    if auth_type in {"noauth", "no_auth", "anonymous", "public"}:
        auth_type = "none"

    auth_header_name = (
        str(
            data.get("auth_header_name")
            or raw_config.get("auth_header_name")
            or (
                (raw_config.get("auth") or {}).get("header_name")
                if isinstance(raw_config.get("auth"), dict)
                else ""
            )
            or "X-API-KEY"
        )
        .strip()
        or "X-API-KEY"
    )

    put_env(envs["auth_header"], auth_header_name)
    config_refs["auth_header_name"] = f"${{ENV:{envs['auth_header']}}}"

    auth_query_param = str(
        data.get("auth_query_param")
        or raw_config.get("auth_query_param")
        or ""
    ).strip()

    if auth_query_param:
        put_env(envs["auth_query_param"], auth_query_param)
        config_refs["auth_query_param"] = f"${{ENV:{envs['auth_query_param']}}}"

    sample_input = data.get("sample_input") if isinstance(data.get("sample_input"), dict) else {}

    body_template = (
        raw_config.get("json_body_template")
        if "json_body_template" in raw_config
        else raw_config.get("body_template", {})
    )
    if not body_template and method_value not in {"GET", "HEAD"} and sample_input:
        body_template = sample_input

    put_env(envs["body_template"], _json_env(body_template or {}))
    config_refs["json_body_template"] = f"${{ENV:{envs['body_template']}}}"

    query_template = raw_config.get("query_template") or raw_config.get("params_template") or {}
    if not query_template and method_value in {"GET", "DELETE"} and sample_input:
        query_template = sample_input

    put_env(envs["query_template"], _json_env(query_template or {}))
    config_refs["query_template"] = f"${{ENV:{envs['query_template']}}}"

    headers_template = raw_config.get("headers_template") or raw_config.get("headers") or {}
    put_env(envs["headers_template"], _json_env(headers_template or {}))
    config_refs["headers_template"] = f"${{ENV:{envs['headers_template']}}}"

    original_secret_env = str(
        data.get("secret_env")
        or raw_config.get("secret_env")
        or ""
    ).strip()

    secret_value = (
        data.get("secret_value")
        or raw_config.get("secret_value")
        or raw_config.get("api_key")
        or raw_config.get("token")
        or raw_config.get("password")
    )

    auth_requires_secret = auth_type not in {
        "",
        "none",
        "no_auth",
        "noauth",
        "anonymous",
        "public",
        "unknown",
    }

    if auth_requires_secret:
        if secret_value in (None, "") and original_secret_env:
            secret_value = os.environ.get(original_secret_env, "")

        if put_env(envs["secret"], secret_value, secret=True):
            config_refs["secret"] = f"${{ENV:{envs['secret']}}}"

    sanitized_config = dict(raw_config)
    sanitized_config.update(
        {
            "base_url": f"${{ENV:{envs['base_url']}}}",
            "method": f"${{ENV:{envs['method']}}}",
            "auth_type": auth_type,
            "secret_env": envs["secret"] if auth_requires_secret else "",
            "original_secret_env": original_secret_env,
            "platform_env_prefix": envs["prefix"],
            "auth_header_name": f"${{ENV:{envs['auth_header']}}}",
            "json_body_template_env": envs["body_template"],
            "query_template_env": envs["query_template"],
            "headers_template_env": envs["headers_template"],
            "auth_override": auth_override,
        }
    )

    if auth_query_param:
        sanitized_config["auth_query_param"] = f"${{ENV:{envs['auth_query_param']}}}"

    if auth_requires_secret:
        saved_auth = _build_saved_auth_config(
            {
                **data,
                "secret_env": envs["secret"],
                "auth_header_name": f"${{ENV:{envs['auth_header']}}}",
                "auth_query_param": (
                    f"${{ENV:{envs['auth_query_param']}}}"
                    if auth_query_param
                    else ""
                ),
            },
            sanitized_config,
            auth_type,
            envs["secret"],
        )
        if saved_auth:
            sanitized_config["auth"] = saved_auth

    sanitized_config, inferred_refs = _sanitize_authoring_config(sanitized_config)

    for ref in inferred_refs:
        if ref in raw_values:
            os.environ[ref] = str(raw_values[ref])

        if ref.endswith("_SECRET"):
            configured_secrets.append(ref)
        else:
            configured_env.append(ref)

    record = {
        "tool_name": tool_name,
        "platform_env_prefix": envs["prefix"],
        "config": sanitized_config,
        "sample_input": sample_input,
        "configured_env": sorted(set(configured_env)),
        "configured_secrets": sorted(set(configured_secrets)),
        "config_refs": config_refs,
        "raw_values": raw_values,
        "auth_override": auth_override,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    _TOOL_AUTHORING_CONFIG_STORE[session_id] = record
    _persist_tool_authoring_config_store_to_disk()

    return {
        "success": True,
        "session_id": session_id,
        "platform_env_prefix": envs["prefix"],
        "configured_env": sorted(set(configured_env)),
        "configured_secrets": sorted(set(configured_secrets)),
        "config_refs": config_refs,
        "config": sanitized_config,
        "auth_override": auth_override,
        "persisted": True,
        "store_path": str(_tool_authoring_config_store_path()),
    }


def tool_authoring_config_status(session_id: str = "default") -> dict[str, Any]:
    _load_tool_authoring_config_store_from_disk()

    key = _tool_config_session_id({"session_id": session_id})
    stored = _TOOL_AUTHORING_CONFIG_STORE.get(key)

    if not stored:
        return {
            "success": True,
            "configured": False,
            "session_id": key,
            "configured_env": [],
            "configured_secrets": [],
            "config_refs": {},
            "config": {},
            "sample_input": {},
            "auth_override": {
                "mode": "auto",
                "reason": "",
                "source": "user",
                "updated_at": "",
            },
            "persisted": False,
            "store_path": str(_tool_authoring_config_store_path()),
        }

    _restore_env_from_authoring_record(stored)

    configured_env = [
        name
        for name in stored.get("configured_env", [])
        if os.environ.get(name) is not None
    ]
    configured_secrets = [
        name
        for name in stored.get("configured_secrets", [])
        if os.environ.get(name) is not None
    ]

    auth_override = (
        stored.get("auth_override")
        if isinstance(stored.get("auth_override"), dict)
        else {}
    )

    if not auth_override:
        config = stored.get("config") if isinstance(stored.get("config"), dict) else {}
        auth_override = (
            config.get("auth_override")
            if isinstance(config.get("auth_override"), dict)
            else {}
        )

    if not auth_override:
        auth_override = {
            "mode": "auto",
            "reason": "",
            "source": "user",
            "updated_at": "",
        }

    return {
        "success": True,
        "configured": bool(configured_env or configured_secrets),
        "session_id": key,
        "configured_env": configured_env,
        "configured_secrets": configured_secrets,
        "config_refs": stored.get("config_refs") or {},
        "config": stored.get("config") or {},
        "sample_input": stored.get("sample_input") or {},
        "auth_override": auth_override,
        "updated_at": stored.get("updated_at"),
        "persisted": True,
        "store_path": str(_tool_authoring_config_store_path()),
    }

SnippetKind = Literal[
    "minimal_usage",
    "multi_input_usage",
    "file_output_usage",
    "batch_usage",
    "error_repair_usage",
    "anti_pattern",
    "trial_run_usage",
]


@dataclass(frozen=True)
class ToolSnippet:
    id: str
    title: str
    kind: SnippetKind = "minimal_usage"
    applies_to: dict[str, list[str]] = field(default_factory=dict)
    description: str = ""
    code: str = ""
    expected_input_shape: dict[str, Any] = field(default_factory=dict)
    expected_output_shape: dict[str, Any] = field(default_factory=dict)
    return_rule: str = ""
    anti_patterns: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    usage_policy: UsagePolicy = "self_implementation_allowed"
    priority: int = 0

@dataclass(frozen=True)
class ToolFunctionManifest:
    function_name: str
    import_path: str
    short_description: str
    when_to_use: str
    signature: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    return_contract: str = "Returns a dict that conforms to output_schema."
    example_call: str = ""
    example_return: str = ""
    example_stdout: str = ""
    common_mistakes: list[str] = field(default_factory=list)
    trial_mode_behavior: str = ""
    safety_notes: list[str] = field(default_factory=list)
    required_env: list[str] = field(default_factory=list)
    required_secrets: list[str] = field(default_factory=list)
    usage_policy: UsagePolicy = "self_implementation_allowed"
    allowed_roles: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    forbidden_imports: list[str] = field(default_factory=list)
    forbidden_side_effects: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ToolCapability:
    name: str
    display_name: str
    category: str
    roles: list[str] = field(default_factory=list)

    enabled_by_default: bool = True
    allow_creator_use: bool = True
    allow_external_side_effect: bool = False

    helper_imports: list[str] = field(default_factory=list)
    allowed_roles: list[str] = field(default_factory=list)
    required_capabilities: list[str] = field(default_factory=list)
    optional_capabilities: list[str] = field(default_factory=list)
    forbidden_capabilities: list[str] = field(default_factory=list)
    usage_policy: UsagePolicy = "self_implementation_allowed"
    required_env: list[str] = field(default_factory=list)
    required_secrets: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    helper_module: str = "backend.services.skill_runtime"
    forbidden_direct_imports: list[str] = field(default_factory=list)
    safety_level: str = "standard"

    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)

    trial_mode: Literal["none", "mock", "minimal_file"] = "mock"
    validator_kind: str = "generic"
    prompt_guidance: str = ""
    tool_type: str = "python_helper"
    functions: list[ToolFunctionManifest] = field(default_factory=list)
    snippets: list[ToolSnippet] = field(default_factory=list)
    adapter_path: str = ""
    version: str = "1.0.0"
    approval_status: str = "approved"
    test_status: str = "unknown"
    last_validation_result: dict[str, Any] = field(default_factory=dict)
    created_by: str = "system"
    created_at: str = ""
    updated_at: str = ""


_ROLE_FORBIDDEN_CAPABILITIES: dict[str, list[str]] = {
    "text_generator": ["image_generation", "pdf_generation"],
    "image_generator": ["text_generation", "pdf_generation"],
    "composite_generator": ["pdf_generation"],
    "generic_script": ["text_generation", "image_generation", "pdf_generation"],
    "reference": ["runtime_execution", "image_generation"],
    "asset": ["runtime_execution", "image_generation"],
    "skill_overview": ["hidden_runtime_protocol"],
}


BUILTIN_TOOL_CAPABILITIES: dict[str, ToolCapability] = {
    "text_generation": ToolCapability(
        name="text_generation",
        display_name="文本生成",
        category="generation",
        roles=["text_generator", "composite_generator"],
        helper_imports=["generate_text_with_llm"],
        input_schema={"type": "object", "properties": {"prompt": {"type": "string"}}},
        output_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        validator_kind="helper_import",
        usage_policy="helper_preferred",
        prompt_guidance="需要文本生成时，可优先使用 backend.services.skill_runtime.generate_text_with_llm；也可在声明能力边界内自实现。",
    ),
    "image_generation": ToolCapability(
        name="image_generation",
        display_name="图片生成",
        category="generation",
        roles=["image_generator", "composite_generator"],
        helper_imports=["generate_stable_diffusion_image"],
        output_schema={"type": "object", "properties": {"image_path": {"type": "string"}}},
        trial_mode="minimal_file",
        validator_kind="helper_import",
        usage_policy="helper_preferred",
        prompt_guidance="需要图片生成时，可优先使用 generate_stable_diffusion_image；也可自实现，但最终 stdout/artifact 必须通过 E2E 校验。",
    ),
    "pdf_generation": ToolCapability(
        name="pdf_generation",
        display_name="PDF 生成",
        category="document",
        roles=["pdf_builder"],
        helper_imports=["create_pdf", "build_pdf_report", "images_to_pdf", "merge_pdfs"],
        dependencies=["reportlab"],
        output_schema={"type": "object", "properties": {"pdf_path": {"type": "string"}}},
        trial_mode="minimal_file",
        validator_kind="file_output",
        usage_policy="helper_preferred",
        prompt_guidance=(
            "PDF 生成可优先使用平台 helper create_pdf/build_pdf_report/images_to_pdf/merge_pdfs；"
            "也可自实现，但最终 PDF 文件、路径和 stdout 字段必须通过 E2E 校验。"
        ),
    ),
    "docx_generation": ToolCapability(
        name="docx_generation",
        display_name="Word 生成",
        category="document",
        roles=["docx_builder"],
        helper_imports=["create_docx"],
        dependencies=["python-docx"],
        output_schema={"type": "object", "properties": {"docx_path": {"type": "string"}}},
        trial_mode="minimal_file",
        validator_kind="file_output",
        usage_policy="helper_preferred",
        prompt_guidance="Word 生成可优先使用平台 helper create_docx；也可自实现，并返回合法 docx_path/file_outputs。",
    ),
    "pptx_generation": ToolCapability(
        name="pptx_generation",
        display_name="PPT 生成",
        category="document",
        roles=["pptx_builder"],
        helper_imports=["create_pptx"],
        dependencies=["python-pptx"],
        output_schema={"type": "object", "properties": {"pptx_path": {"type": "string"}}},
        trial_mode="minimal_file",
        validator_kind="file_output",
        usage_policy="helper_preferred",
        prompt_guidance="PPT 生成可优先使用平台 helper create_pptx；也可自实现，并返回合法 pptx_path/file_outputs。",
    ),
    "html_asset_generation": ToolCapability(
        name="html_asset_generation",
        display_name="HTML 素材生成",
        category="document",
        roles=["html_asset_builder"],
        output_schema={"type": "object", "properties": {"html_path": {"type": "string"}}},
        validator_kind="file_output",
        prompt_guidance="HTML 资产生成必须输出确定性 HTML 文件路径，并在 stdout JSON 中声明文件输出。",
    ),
    "asset_generation": ToolCapability(
        name="asset_generation",
        display_name="静态素材生成",
        category="asset",
        roles=["asset_builder"],
        output_schema={"type": "object", "properties": {"asset_path": {"type": "string"}}},
        validator_kind="file_output",
        prompt_guidance="静态素材生成只能创建本地文件，不得调用外部服务。",
    ),
    "file_output": ToolCapability(
        name="file_output",
        display_name="文件输出",
        category="common",
        roles=["pdf_builder", "docx_builder", "pptx_builder", "html_asset_builder", "asset_builder"],
        trial_mode="minimal_file",
        validator_kind="file_output",
        prompt_guidance="写文件时必须输出可校验的本地路径，并在 stdout JSON 中包含 path 或 file_outputs。",
    ),
    "pdf_parsing": ToolCapability(
        name="pdf_parsing",
        display_name="PDF 解析",
        category="parsing",
        roles=["pdf_parser"],
        helper_imports=["extract_pdf_text"],
        dependencies=["pypdf"],
        validator_kind="helper_import",
        usage_policy="helper_preferred",
        prompt_guidance="PDF 解析可优先使用 extract_pdf_text；也可自实现。",
    ),
    "docx_parsing": ToolCapability(
        name="docx_parsing",
        display_name="Word 解析",
        category="parsing",
        roles=["docx_parser"],
        helper_imports=["read_docx_text"],
        dependencies=["python-docx"],
        validator_kind="helper_import",
        usage_policy="helper_preferred",
        prompt_guidance="Word 解析可优先使用 read_docx_text；也可自实现。",
    ),
    "pptx_parsing": ToolCapability(
        name="pptx_parsing",
        display_name="PPT 解析",
        category="parsing",
        roles=["pptx_parser"],
        helper_imports=["read_pptx_text"],
        dependencies=["python-pptx"],
        validator_kind="helper_import",
        usage_policy="helper_preferred",
        prompt_guidance="PPT 解析可优先使用 read_pptx_text；也可自实现。",
    ),
    "spreadsheet_read": ToolCapability(
        name="spreadsheet_read",
        display_name="表格读取",
        category="parsing",
        roles=["spreadsheet_reader"],
        helper_imports=["read_spreadsheet"],
        dependencies=["openpyxl"],
        validator_kind="helper_import",
        prompt_guidance="表格读取只能读取本地文件，避免写入或修改原始表格。",
    ),
    "vision_understanding": ToolCapability(
        name="vision_understanding",
        display_name="视觉理解",
        category="ai",
        roles=["vision_analyzer"],
        helper_imports=["analyze_image_with_vision", "ocr_image"],
        required_env=["VISION_MODEL"],
        validator_kind="helper_import",
        usage_policy="helper_preferred",
        prompt_guidance="视觉理解可优先使用 analyze_image_with_vision 或 ocr_image；试运行时可返回 mock 结果。",
    ),
    "http_request": ToolCapability(
        name="http_request",
        display_name="HTTP/API 请求",
        category="retrieval",
        roles=["search_reader", "generic_script"],
        helper_imports=[],
        validator_kind="external_http",
        usage_policy="self_implementation_allowed",
        prompt_guidance=(
            "Use this capability for fixed HTTP/API calls where the request is described by a stable "
            "endpoint/base_url, method, headers/query/body templates, and optional auth config. "
            "It must be implemented by the http_api wrapper. The code_model must write "
            "execute_task(payload, context), not a full requests/httpx adapter. "
            "Inside execute_task, the model may build task-specific params/body/headers and call "
            "context['http_request'](...). The platform owns endpoint/env/auth/secrets and the actual "
            "network execution. The model must not import requests/httpx/urllib or read secrets directly."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "payload": {
                    "type": "object",
                    "description": "Runtime payload used by execute_task to construct the provider request.",
                }
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "result": {"type": "object"},
                "raw": {"type": "object"},
                "error": {"type": "string"},
                "status_code": {"type": "integer"},
            },
        },
    ),
    "network_read": ToolCapability(
        name="network_read",
        display_name="网络资源读取",
        category="retrieval",
        roles=["search_reader", "generic_script"],
        helper_imports=["fetch_url_text"],
        validator_kind="helper_import",
        usage_policy="helper_required",
        prompt_guidance=(
            "Use this capability when the tool needs to read external network resources through "
            "a platform-managed helper rather than a fixed provider API. It must be implemented by "
            "the managed_helper wrapper with helper_contract.helper_name chosen from helper_imports."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "A runtime-provided network resource identifier such as a URL.",
                }
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "source": {"type": "string"},
                "success": {"type": "boolean"},
            },
        },
    ),
    "web_search": ToolCapability(
        name="web_search",
        display_name="网页搜索",
        category="retrieval",
        roles=["search_reader"],
        helper_imports=["web_search", "fetch_url_text"],
        required_env=["SEARCHXNG_BASE_URL"],
        validator_kind="helper_import",
        usage_policy="helper_preferred",
        prompt_guidance="网页搜索可优先使用 web_search/fetch_url_text；也可在能力声明边界内自实现。",
    ),
    "database_read": ToolCapability(
        name="database_read",
        display_name="数据库只读查询",
        category="retrieval",
        roles=["database_reader"],
        helper_imports=["query_database_readonly", "list_database_tables", "describe_database_table"],
        usage_policy="helper_required",
        required_secrets=["DATABASE_URL"],
        validator_kind="database_readonly",
        prompt_guidance="数据库能力只允许 SELECT/WITH 只读查询，必须通过 query_database_readonly，禁止 INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE/CREATE。",
    ),
    "wechat_draft": ToolCapability(
        name="wechat_draft",
        display_name="微信公众号草稿",
        category="publisher",
        roles=["wechat_draft_creator"],
        helper_imports=["create_wechat_draft", "upload_wechat_media"],
        required_secrets=["WECHAT_APP_ID", "WECHAT_APP_SECRET"],
        validator_kind="helper_import",
        usage_policy="helper_required",
        prompt_guidance="默认只能创建微信公众号草稿，不得自动发布。使用 create_wechat_draft 并返回 draft_id。",
    ),
    "wechat_publish": ToolCapability(
        name="wechat_publish",
        display_name="微信公众号发布",
        category="publisher",
        roles=["wechat_publisher"],
        enabled_by_default=False,
        allow_external_side_effect=True,
        helper_imports=["publish_wechat_draft"],
        required_secrets=["WECHAT_APP_ID", "WECHAT_APP_SECRET"],
        validator_kind="external_side_effect",
        usage_policy="helper_required",
        prompt_guidance="除非用户明确要求直接发布，否则只能创建草稿；发布必须通过 publish_wechat_draft。",
    ),
    "deterministic_execution": ToolCapability(
        name="deterministic_execution",
        display_name="确定性脚本执行",
        category="common",
        roles=["generic_script"],
        trial_mode="none",
        validator_kind="generic",
        prompt_guidance="通用脚本不得调用模型、搜索、数据库或外部发布能力，除非 SkillPlan 显式声明对应 capability。",
    ),
    "reference_guidance": ToolCapability(
        name="reference_guidance",
        display_name="参考文档指导",
        category="resource",
        roles=["reference"],
        allow_creator_use=False,
        trial_mode="none",
        validator_kind="resource",
    ),
    "static_resource": ToolCapability(
        name="static_resource",
        display_name="静态资源",
        category="resource",
        roles=["asset"],
        allow_creator_use=False,
        trial_mode="none",
        validator_kind="resource",
    ),
    "workflow_overview": ToolCapability(
        name="workflow_overview",
        display_name="工作流概览",
        category="resource",
        roles=["skill_overview"],
        allow_creator_use=False,
        trial_mode="none",
        validator_kind="resource",
    ),
    "authoring_config_collector": ToolCapability(
        name="authoring_config_collector",
        display_name="Authoring 通用配置收集",
        category="authoring",
        roles=["tool_authoring"],
        allow_creator_use=False,
        tool_type="internal_authoring_tool",
        validator_kind="internal_authoring_tool",
        usage_policy="helper_required",
        prompt_guidance="仅 Tool Authoring 流程可调用；收集 endpoint/base_url、认证、env/secret 引用、headers/query/body 模板和 sample input，不保存明文 secret。",
    ),
    "authoring_schema_infer": ToolCapability(
        name="authoring_schema_infer",
        display_name="Authoring Schema 推断",
        category="authoring",
        roles=["tool_authoring"],
        allow_creator_use=False,
        tool_type="internal_authoring_tool",
        validator_kind="internal_authoring_tool",
        usage_policy="helper_required",
        prompt_guidance="仅 Tool Authoring 流程可调用；根据需求、配置和 sample input 推断输入/输出 schema 草案。",
    ),
    "authoring_live_test": ToolCapability(
        name="authoring_live_test",
        display_name="Authoring Live Test",
        category="authoring",
        roles=["tool_authoring"],
        allow_creator_use=False,
        allow_external_side_effect=True,
        tool_type="internal_authoring_tool",
        validator_kind="internal_authoring_tool",
        usage_policy="helper_required",
        prompt_guidance="仅 Tool Authoring 流程可调用；在用户确认外部网络后执行一次通用 live_test。",
    ),
    "authoring_dependency_check": ToolCapability(
        name="authoring_dependency_check",
        display_name="Authoring 依赖检测",
        category="authoring",
        roles=["tool_authoring"],
        allow_creator_use=False,
        tool_type="internal_authoring_tool",
        validator_kind="internal_authoring_tool",
        usage_policy="helper_required",
        prompt_guidance="仅 Tool Authoring 流程可调用；检测 adapter 计划所需 Python 依赖是否可 import。",
    ),
    "authoring_code_protocol_check": ToolCapability(
        name="authoring_code_protocol_check",
        display_name="Authoring 代码协议检查",
        category="authoring",
        roles=["tool_authoring"],
        allow_creator_use=False,
        tool_type="internal_authoring_tool",
        validator_kind="internal_authoring_tool",
        usage_policy="helper_required",
        prompt_guidance="仅 Tool Authoring 流程可调用；静态检查 adapter 是否暴露 run/manifest function、env 读取和危险调用。",
    ),
    "authoring_file_output_check": ToolCapability(
        name="authoring_file_output_check",
        display_name="Authoring 文件输出协议检查",
        category="authoring",
        roles=["tool_authoring"],
        allow_creator_use=False,
        tool_type="internal_authoring_tool",
        validator_kind="internal_authoring_tool",
        usage_policy="helper_required",
        prompt_guidance="仅 Tool Authoring 流程可调用；检查文件输出 schema 是否声明 file_paths/file_outputs 或 OUTPUT_DIR 约束。",
    ),
}

RESOURCE_ROLES: frozenset[str] = frozenset({"skill_overview", "reference", "asset", "tool_authoring"})
TOOL_OVERRIDE_PERSISTENCE = "process_memory"
CUSTOM_TOOL_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "config" / "tool_registry.custom.json"
CUSTOM_TOOL_ADAPTER_DIR = Path(__file__).resolve().parent / "runtime_tools" / "custom_tools"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
_REGISTERED_TOOL_CAPABILITIES: dict[str, ToolCapability] = {}
_TOOL_OVERRIDES: dict[str, dict[str, bool]] = {}
_RUNTIME_HELPERS_CACHE: set[str] | None = None

_ALLOWED_USAGE_POLICIES = {"helper_required", "helper_preferred", "self_implementation_allowed"}
_ALLOWED_SNIPPET_KINDS = {"minimal_usage", "multi_input_usage", "file_output_usage", "batch_usage", "error_repair_usage", "anti_pattern", "trial_run_usage"}
_ALLOWED_TOOL_TYPES = {
    "python_helper", "http_api", "local_command", "database_query",
    "file_converter", "document_generator", "image_generator", "custom_adapter",
    "internal_authoring_tool",
}
_DANGEROUS_IMPORTS = {"subprocess", "shutil", "socket", "paramiko", "ftplib", "telnetlib"}
_DANGEROUS_CALLS = {"eval", "exec", "compile", "open"}
_HIGH_RISK_CAPABILITIES = {
    "database_write", "external_http", "wechat_publish", "file_delete",
    "shell_command", "network_access", "secret_access",
}

_DEPENDENCY_IMPORT_NAMES = {
    "python-docx": "docx",
    "python-pptx": "pptx",
}


def _normalize_dependency_record(dep: Any) -> dict[str, Any]:
    if isinstance(dep, str):
        package = dep.strip()
        return {
            "package": package,
            "imports": [package.replace("-", "_")] if package else [],
            "version": "",
        }

    if isinstance(dep, dict):
        package = str(
            dep.get("package")
            or dep.get("name")
            or dep.get("pip")
            or ""
        ).strip()

        imports = dep.get("imports") or dep.get("import_names") or dep.get("modules") or []
        if isinstance(imports, str):
            imports = [imports]
        if not isinstance(imports, list):
            imports = []

        imports = [
            str(item).strip()
            for item in imports
            if str(item).strip()
        ]

        # 如果模型没给 imports，保底用 package 名的简单转换。
        # 这不是业务词表，只是兜底；正确路径是 planner 给 imports。
        if not imports and package:
            imports = [package.replace("-", "_")]

        return {
            "package": package,
            "imports": imports,
            "version": str(dep.get("version") or "").strip(),
        }

    return {"package": "", "imports": [], "version": ""}


def _manifest_dependency_records(manifest: dict[str, Any] | None) -> list[dict[str, Any]]:
    manifest = manifest if isinstance(manifest, dict) else {}
    deps = manifest.get("dependencies")
    if not isinstance(deps, list):
        return []

    records: list[dict[str, Any]] = []
    for dep in deps:
        record = _normalize_dependency_record(dep)
        if record.get("package") or record.get("imports"):
            records.append(record)

    return records


def _dependency_available(dependency: Any) -> bool:
    record = _normalize_dependency_record(dependency)

    imports = record.get("imports")
    if not isinstance(imports, list) or not imports:
        return True

    for module_name in imports:
        if importlib.util.find_spec(str(module_name)) is None:
            return False

    return True


def _with_overrides(capability: ToolCapability) -> ToolCapability:
    override = _TOOL_OVERRIDES.get(capability.name, {})
    return replace(
        capability,
        enabled_by_default=override.get("enabled", capability.enabled_by_default),
        allow_creator_use=override.get("allow_creator_use", capability.allow_creator_use),
    )



def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str, fallback: str = "custom_tool") -> str:
    text = re.sub(r"[^a-zA-Z0-9_]+", "_", (value or "").strip().lower()).strip("_")
    if not text:
        text = fallback
    if text[0].isdigit():
        text = f"tool_{text}"
    return text[:64]


def _function_from_dict(data: dict[str, Any]) -> ToolFunctionManifest:
    known = {field.name for field in dataclasses_fields(ToolFunctionManifest)}
    payload = {key: value for key, value in (data or {}).items() if key in known}
    return ToolFunctionManifest(**payload)


def _snippet_from_dict(data: dict[str, Any]) -> ToolSnippet:
    known = {field.name for field in dataclasses_fields(ToolSnippet)}
    payload = {key: value for key, value in (data or {}).items() if key in known}
    if not payload.get("id"):
        payload["id"] = _slug(str(payload.get("title") or "snippet"), fallback="snippet")
    if not payload.get("title"):
        payload["title"] = str(payload["id"]).replace("_", " ").title()
    if payload.get("kind") not in _ALLOWED_SNIPPET_KINDS:
        payload["kind"] = "minimal_usage"
    if not isinstance(payload.get("applies_to"), dict):
        payload["applies_to"] = {}
    for key in ("roles", "capabilities", "failure_layers"):
        values = payload["applies_to"].get(key, [])
        payload["applies_to"][key] = [str(item) for item in values if item] if isinstance(values, list) else []
    for key in ("expected_input_shape", "expected_output_shape"):
        if not isinstance(payload.get(key), dict):
            payload[key] = {}
    for key in ("anti_patterns", "requires"):
        payload[key] = [str(item) for item in payload.get(key, []) if item] if isinstance(payload.get(key), list) else []
    if payload.get("usage_policy") not in _ALLOWED_USAGE_POLICIES:
        payload["usage_policy"] = "self_implementation_allowed"
    try:
        payload["priority"] = int(payload.get("priority") or 0)
    except (TypeError, ValueError):
        payload["priority"] = 0
    return ToolSnippet(**payload)


def _capability_from_dict(data: dict[str, Any]) -> ToolCapability:
    payload = dict(data or {})
    functions = payload.pop("functions", []) or []
    snippets = payload.pop("snippets", []) or []
    if "enabled" in payload and "enabled_by_default" not in payload:
        payload["enabled_by_default"] = bool(payload.pop("enabled"))
    if "allowed_roles" in payload and "roles" not in payload:
        payload["roles"] = list(payload.get("allowed_roles") or [])
    known = {field.name for field in dataclasses_fields(ToolCapability)}
    payload = {key: value for key, value in payload.items() if key in known}
    payload["functions"] = [_function_from_dict(item) for item in functions if isinstance(item, dict)]
    payload["snippets"] = [_snippet_from_dict(item) for item in snippets if isinstance(item, dict)]
    return ToolCapability(**payload)


def _capability_to_registry_record(capability: ToolCapability) -> dict[str, Any]:
    data = asdict(capability)
    data["enabled"] = data.pop("enabled_by_default")
    data["allowed_roles"] = capability.allowed_roles or capability.roles
    return data


def _load_registered_tools_from_disk() -> None:
    if not CUSTOM_TOOL_REGISTRY_PATH.exists():
        return
    try:
        payload = json.loads(CUSTOM_TOOL_REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    records = payload.get("tools") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        return
    for record in records:
        if not isinstance(record, dict):
            continue
        try:
            cap = _capability_from_dict(record)
        except Exception:
            continue
        if cap.name:
            _REGISTERED_TOOL_CAPABILITIES[cap.name] = cap


def persist_registered_tools() -> None:
    CUSTOM_TOOL_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    records = [_capability_to_registry_record(cap) for cap in _REGISTERED_TOOL_CAPABILITIES.values()]
    CUSTOM_TOOL_REGISTRY_PATH.write_text(
        json.dumps({"tools": records}, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _runtime_helper_names() -> set[str]:
    global _RUNTIME_HELPERS_CACHE
    if _RUNTIME_HELPERS_CACHE is not None:
        return set(_RUNTIME_HELPERS_CACHE)

    try:
        runtime_module = importlib.import_module("backend.services.skill_runtime")
    except Exception:
        runtime_module = None

    if runtime_module is not None:
        _RUNTIME_HELPERS_CACHE = {
            helper
            for capability in [*BUILTIN_TOOL_CAPABILITIES.values(), *_REGISTERED_TOOL_CAPABILITIES.values()]
            for helper in capability.helper_imports
            if hasattr(runtime_module, helper)
        }
        return set(_RUNTIME_HELPERS_CACHE)

    # Fallback for damaged import environments: keep the old static scan, but
    # include imported/re-exported helper aliases as well as local definitions.
    runtime_path = Path(__file__).with_name("skill_runtime.py")
    try:
        tree = ast.parse(runtime_path.read_text(encoding="utf-8"))
    except OSError:
        _RUNTIME_HELPERS_CACHE = set()
        return set()

    helper_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            helper_names.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            helper_names.update(alias.asname or alias.name for alias in node.names)
    _RUNTIME_HELPERS_CACHE = helper_names
    return set(_RUNTIME_HELPERS_CACHE)



@dataclass(frozen=True)
class ToolResolveResult:
    allowed_tools: list[str] = field(default_factory=list)
    allowed_helper_imports: list[str] = field(default_factory=list)
    required_dependencies: list[str] = field(default_factory=list)
    forbidden_imports: list[str] = field(default_factory=list)
    tool_function_cards: list[str] = field(default_factory=list)
    tool_snippets: list[dict[str, Any]] = field(default_factory=list)
    tool_usage_prompt: str = ""
    warnings: list[str] = field(default_factory=list)



def _schema_summary(schema: dict[str, Any]) -> str:
    if not schema:
        return "{}"
    return json.dumps(schema, ensure_ascii=False, sort_keys=True)

def _default_core_logic_code(wrapper_family: str = "python_compute") -> str:
    wrapper_family = _canonical_wrapper_family(wrapper_family)

    if wrapper_family == "http_api":
        return '''
def execute_task(payload: dict, context: dict) -> dict:
    """Core tool logic for a fixed HTTP/API tool.

    Use context["http_request"](...) to perform the platform-controlled HTTP call.
    The platform owns endpoint, auth, secrets and actual network execution.
    """
    response = context["http_request"](
        params=dict(payload or {}),
        json={},
    )
    if isinstance(response, dict):
        response.setdefault("success", True)
        return response
    return {"success": True, "result": response}
'''.strip()

    if wrapper_family == "managed_helper":
        return '''
def execute_task(payload: dict, context: dict) -> dict:
    """Core tool logic for a managed helper tool.

    Use context["call_helper"](...) to call an allowed platform helper.
    """
    helper_name = context.get("helper_name") or ""
    value = context["call_helper"](helper_name, **dict(payload or {}))
    if isinstance(value, dict):
        value.setdefault("success", True)
        return value
    return {"success": True, "result": value}
'''.strip()

    if wrapper_family == "file_io":
        return '''
def execute_task(payload: dict, context: dict) -> dict:
    """Core file tool logic.

    Use context["safe_output_path"](filename) for every output path.
    """
    filename = str(payload.get("filename") or "output.txt").strip() or "output.txt"
    path = context["safe_output_path"](filename)
    content = str(payload.get("content") or payload.get("text") or "")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    return {
        "success": True,
        "path": path,
        "file_path": path,
        "file_paths": [path],
        "file_outputs": [{"path": path}],
    }
'''.strip()

    if wrapper_family == "database_query":
        return '''
def execute_task(payload: dict, context: dict) -> dict:
    """Core readonly database logic.

    Use context["query_readonly"](sql, params, limit) for database reads.
    """
    sql = str(payload.get("sql") or "SELECT 1 AS value")
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    limit = int(payload.get("limit") or 100)
    rows = context["query_readonly"](sql, params=params, limit=limit)

    return {
        "success": True,
        "rows": rows,
        "results": rows,
        "total": len(rows),
    }
'''.strip()

    if wrapper_family == "local_command":
        return '''
def execute_task(payload: dict, context: dict) -> dict:
    """Core local command planning logic.

    Return {"argv": [...]} only. The platform wrapper validates approval and executes.
    """
    return {
        "argv": context["render_command"](payload),
    }
'''.strip()

    return '''
def execute_task(payload: dict, context: dict) -> dict:
    """Core local deterministic tool logic."""
    return {
        "success": True,
        "result": dict(payload or {}),
    }
'''.strip()

async def _author_sample_input_with_model(
    *,
    request: dict[str, Any],
    plan: dict[str, Any],
    manifest: dict[str, Any],
    wrapper_family: str,
    model_notes: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    existing = request.get("sample_input")
    if isinstance(existing, dict) and existing:
        return existing

    planned = plan.get("sample_input")
    if isinstance(planned, dict) and planned:
        return planned

    input_schema = manifest.get("input_schema") if isinstance(manifest.get("input_schema"), dict) else {}

    messages = [
        {
            "role": "system",
            "content": (
                "You are sample_input_model for a generic tool authoring system. "
                "Return strict JSON only: {\"sample_input\": {...}, \"notes\": []}. "
                "Generate one realistic, safe, runnable test input for the current tool. "
                "It must conform to manifest.input_schema and should exercise the core functionality. "
                "Do not use secrets. Do not use private URLs. "
                "For HTTP/API tools, use the user's confirmed sample/query if available. "
                "For file/plot tools, include small synthetic data. "
                "For ML tools such as SVM/decision tree, include a tiny synthetic dataset and labels. "
                "For document/PDF parsing, use a placeholder local file path only if the input schema requires a file path."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "wrapper_family": wrapper_family,
                    "user_request": {
                        "description": request.get("description"),
                        "operation": request.get("operation"),
                        "tool_name": request.get("tool_name"),
                        "input_description": request.get("input_description"),
                        "output_description": request.get("output_description"),
                    },
                    "manifest": manifest,
                    "input_schema": input_schema,
                    "tool_kind": plan.get("tool_kind") or manifest.get("tool_kind"),
                    "code_policy": manifest.get("code_policy") or {},
                    "dependencies": manifest.get("dependencies") or [],
                },
                ensure_ascii=False,
            ),
        },
    ]

    result, ack, err = await _complete_author_model(
        "planner",
        messages,
        reason="creator_tool_author_sample_input",
    )

    if ack:
        model_notes.append(f"sample_input_model={ack['model']}")
    if err:
        warnings.append(f"sample_input_model unavailable, used schema fallback: {err}")

    sample = result.get("sample_input") if isinstance(result, dict) else None
    if isinstance(sample, dict) and sample:
        return sample

    props = _schema_properties(input_schema)
    fallback: dict[str, Any] = {}
    for key, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        if "default" in spec:
            fallback[key] = spec["default"]
            continue

        typ = str(spec.get("type") or "").lower()
        if typ in {"number", "integer"}:
            fallback[key] = 1
        elif typ == "boolean":
            fallback[key] = True
        elif typ == "array":
            fallback[key] = []
        elif typ == "object":
            fallback[key] = {}
        else:
            fallback[key] = "demo"

    return fallback

def _core_logic_policy(
    wrapper_family: str,
    contract: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return generic policy for model-written execute_task code.

    This is capability/wrapper policy, not business hardcoding.
    """
    wrapper_family = _canonical_wrapper_family(wrapper_family)
    contract = contract if isinstance(contract, dict) else {}
    manifest = manifest if isinstance(manifest, dict) else {}

    safe_stdlib_imports = {
        "base64",
        "collections",
        "csv",
        "datetime",
        "decimal",
        "fractions",
        "functools",
        "hashlib",
        "html",
        "io",
        "itertools",
        "json",
        "math",
        "operator",
        "random",
        "re",
        "statistics",
        "string",
        "textwrap",
        "typing",
        "uuid",
    }

    manifest_policy = manifest.get("code_policy") if isinstance(manifest.get("code_policy"), dict) else {}
    contract_policy = contract.get("code_policy") if isinstance(contract.get("code_policy"), dict) else {}

    allowed_imports: set[str] = set(safe_stdlib_imports)

    # dependencies 也可以作为允许 import 的来源。
    for dep in _manifest_dependency_records(manifest):
        imports = dep.get("imports") if isinstance(dep.get("imports"), list) else []
        for import_name in imports:
            text = str(import_name or "").strip()
            if text:
                allowed_imports.add(text.replace("-", "_").split(".")[0])

    for source in (manifest_policy, contract_policy):
        raw = source.get("allowed_imports")
        if isinstance(raw, list):
            allowed_imports.update(
                str(item).replace("-", "_").split(".")[0]
                for item in raw
                if str(item).strip()
            )

    context_apis_by_wrapper = {
        "http_api": {
            "http_request",
            "manifest",
            "endpoint",
            "method",
            "base_headers",
            "base_params",
            "base_json",
            "wrapper_family",
        },
        "managed_helper": {
            "call_helper",
            "helpers",
            "helper_name",
            "helper_contract",
            "manifest",
            "wrapper_family",
        },
        "python_compute": {
            "manifest",
            "wrapper_family",
        },
        "file_io": {
            "safe_output_path",
            "output_dir",
            "manifest",
            "wrapper_family",
        },
        "database_query": {
            "query_readonly",
            "manifest",
            "wrapper_family",
        },
        "local_command": {
            "render_command",
            "manifest",
            "wrapper_family",
        },
    }

    return {
        "wrapper_family": wrapper_family,
        "required_function": "execute_task",
        "allowed_functions": {"execute_task"},
        "allowed_imports": allowed_imports,
        "allowed_context_keys": context_apis_by_wrapper.get(
            wrapper_family,
            {"manifest", "wrapper_family"},
        ),
        "forbidden_builtin_calls": {
            "__import__",
            "breakpoint",
            "compile",
            "eval",
            "exec",
            "globals",
            "input",
            "locals",
            "vars",
        },
        "allow_open": wrapper_family == "file_io",
    }


def _core_logic_code_errors(
    wrapper_family: str,
    code: str,
    contract: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
) -> list[str]:
    policy = _core_logic_policy(wrapper_family, contract=contract, manifest=manifest)

    errors: list[str] = []

    try:
        tree = ast.parse(code or "")
    except SyntaxError as exc:
        return [f"core logic code syntax error: {exc}"]

    wrapper_family = str(policy["wrapper_family"])
    required_function = str(policy["required_function"])
    allowed_functions: set[str] = set(policy["allowed_functions"])
    allowed_imports: set[str] = set(policy["allowed_imports"])
    forbidden_builtin_calls: set[str] = set(policy["forbidden_builtin_calls"])
    allow_open = bool(policy["allow_open"])

    function_names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = str(alias.name or "").split(".")[0]
                if root not in allowed_imports:
                    errors.append(
                        f"core logic import {root!r} is not allowed for wrapper_family={wrapper_family}; "
                        "declare it in manifest.code_policy.allowed_imports/dependencies, or use platform context APIs"
                    )

        elif isinstance(node, ast.ImportFrom):
            root = str(node.module or "").split(".")[0]
            if root not in allowed_imports:
                errors.append(
                    f"core logic import {root!r} is not allowed for wrapper_family={wrapper_family}; "
                    "declare it in manifest.code_policy.allowed_imports/dependencies, or use platform context APIs"
                )

        elif isinstance(node, ast.FunctionDef):
            function_names.add(node.name)

            if node.name not in allowed_functions:
                errors.append(
                    f"core logic may only define {required_function}(payload, context), got {node.name}"
                )

            arg_names = [arg.arg for arg in node.args.args]
            if node.name == required_function and arg_names[:2] != ["payload", "context"]:
                errors.append(
                    f"{required_function} must have signature {required_function}(payload, context)"
                )

        elif isinstance(node, ast.AsyncFunctionDef):
            errors.append("core logic must not define async functions")

        elif isinstance(node, ast.ClassDef):
            errors.append("core logic must not define classes")

        elif isinstance(node, ast.Global):
            errors.append("core logic must not use global statements")

        elif isinstance(node, ast.Nonlocal):
            errors.append("core logic must not use nonlocal statements")

        elif isinstance(node, ast.Call):
            func = node.func

            if isinstance(func, ast.Name):
                called = func.id

                if called in forbidden_builtin_calls:
                    errors.append(f"core logic must not call {called}")

                if called == "open" and not allow_open:
                    errors.append(
                        f"core logic must not call open for wrapper_family={wrapper_family}; "
                        "use wrapper context APIs instead"
                    )

            elif isinstance(func, ast.Attribute):
                attr = func.attr

                # 这里不是业务词表，而是 Python 逃逸/副作用边界。
                if attr in {
                    "system",
                    "popen",
                    "spawn",
                    "fork",
                    "execv",
                    "execve",
                    "remove",
                    "unlink",
                    "rmdir",
                    "rmtree",
                    "chmod",
                    "chown",
                    "connect",
                }:
                    errors.append(
                        f"core logic must not call unsafe method {attr!r}; "
                        "use platform context APIs instead"
                    )

    if required_function not in function_names:
        errors.append(f"core logic code must define {required_function}(payload, context)")

    if wrapper_family == "file_io":
        text = code or ""
        if "open(" in text and "safe_output_path" not in text:
            errors.append(
                "file_io core logic may call open only with paths produced by context['safe_output_path']"
            )

    return sorted(set(errors))

def _default_snippet_for_function(capability: ToolCapability, fn: ToolFunctionManifest) -> ToolSnippet:
    import_stmt = f"from {fn.import_path} import {fn.function_name}" if fn.import_path else f"import {fn.function_name}"
    return ToolSnippet(
        id=f"{fn.function_name}.minimal_usage",
        title=f"Use {fn.function_name}",
        kind="minimal_usage",
        applies_to={
            "roles": fn.allowed_roles or capability.allowed_roles or capability.roles,
            "capabilities": fn.required_capabilities or capability.required_capabilities or [capability.name],
            "failure_layers": ["helper_call_failed", "final_platform_output_value_invalid", "artifact_missing"],
        },
        description=fn.when_to_use or fn.short_description,
        code=f"{import_stmt}\n\nresult = {fn.function_name}(... )\nreturn result",
        expected_input_shape=fn.input_schema or capability.input_schema,
        expected_output_shape=fn.output_schema or capability.output_schema,
        return_rule=fn.return_contract or "Return the helper result directly if it is already a platform stdout dict.",
        anti_patterns=fn.common_mistakes or ["Do not guess parameters.", "Do not wrap a platform stdout dict inside the wrong field."],
        requires=fn.required_capabilities or capability.required_capabilities or [capability.name],
        usage_policy=fn.usage_policy or capability.usage_policy,
        priority=10,
    )


def snippets_for_tool(capability: ToolCapability) -> list[ToolSnippet]:
    snippets = list(capability.snippets)
    if snippets:
        return snippets
    functions = list(capability.functions)
    if not functions:
        functions = [
            ToolFunctionManifest(
                function_name=helper,
                import_path=capability.helper_module,
                short_description=capability.prompt_guidance or capability.display_name,
                when_to_use=f"Use for capability {capability.name} when role/capability resolution allows it.",
                signature=f"{helper}(...) -> dict",
                input_schema=capability.input_schema,
                output_schema=capability.output_schema,
                return_contract="Return the helper result directly when it already contains platform stdout fields; do not wrap helper result in the wrong key.",
                usage_policy=capability.usage_policy,
                allowed_roles=capability.allowed_roles or capability.roles,
                required_capabilities=capability.required_capabilities or [capability.name],
            )
            for helper in capability.helper_imports
        ]
    return [_default_snippet_for_function(capability, fn) for fn in functions]


def format_tool_snippet(capability: ToolCapability, snippet: ToolSnippet) -> str:
    anti = "\n".join(f"- {item}" for item in snippet.anti_patterns) or "- Follow the helper contract; do not guess parameters or return shape."
    return "\n".join([
        "[Tool Snippet]",
        f"Tool: {capability.name}",
        f"Snippet: {snippet.id} ({snippet.kind}, priority={snippet.priority})",
        f"Use when: {snippet.description or snippet.title}",
        "Correct usage:",
        snippet.code.strip(),
        "Expected input shape:",
        _schema_summary(snippet.expected_input_shape),
        "Expected return:",
        _schema_summary(snippet.expected_output_shape),
        f"Return rule: {snippet.return_rule or 'Return a JSON-serializable dict that matches the expected return.'}",
        f"Usage policy: {snippet.usage_policy}",
        "Do not:",
        anti,
    ])


def validate_tool_snippet(capability: ToolCapability, snippet: ToolSnippet) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []

    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", snippet.id or ""):
        errors.append("snippet id must be 1-128 chars of letters, numbers, '_', '.', ':', or '-'")

    if snippet.kind not in _ALLOWED_SNIPPET_KINDS:
        errors.append(f"snippet kind must be one of {sorted(_ALLOWED_SNIPPET_KINDS)}")

    if not (snippet.code or "").strip():
        errors.append("snippet code is required")

    if snippet.usage_policy not in _ALLOWED_USAGE_POLICIES:
        errors.append("snippet usage_policy is invalid")

    code = snippet.code or ""
    imported_names: set[str] = set()

    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported_names.update(alias.asname or alias.name for alias in node.names)
                root = (node.module or "").split(".")[0]
                if root in _DANGEROUS_IMPORTS:
                    errors.append(f"dangerous import is forbidden in snippet: {root}")

            elif isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    imported_names.add(alias.asname or alias.name.split(".")[-1])
                    if root in _DANGEROUS_IMPORTS:
                        errors.append(f"dangerous import is forbidden in snippet: {root}")

    except SyntaxError as exc:
        warnings.append(f"snippet is not a complete executable Python block: {exc}")

    helpers = set(capability.helper_imports) | {fn.function_name for fn in capability.functions}

    if helpers and not any(helper in code for helper in helpers):
        errors.append("snippet code must reference at least one manifest helper/function name")

    if imported_names and helpers and not (imported_names & helpers):
        warnings.append("snippet imports do not include a declared manifest helper/function")

    if re.search(r"(?:/tmp|/var|/etc|~[/\\]|[A-Za-z]:\\\\)", code):
        errors.append("snippet must not write or direct outputs to dangerous absolute paths")

    if re.search(r"(?:sk-|AKIA|-----BEGIN [A-Z ]*PRIVATE KEY-----)[A-Za-z0-9_\-+/=]{8,}", code):
        errors.append("snippet appears to contain a hard-coded secret")

    declared_outputs = _declared_output_field_names(capability.output_schema, required_only=False)
    declared_required_outputs = _declared_output_field_names(capability.output_schema, required_only=True)

    for fn in capability.functions:
        declared_outputs.update(_declared_output_field_names(fn.output_schema, required_only=False))
        declared_required_outputs.update(_declared_output_field_names(fn.output_schema, required_only=True))

    snippet_outputs = _declared_output_field_names(snippet.expected_output_shape, required_only=False)
    snippet_required_outputs = _declared_output_field_names(snippet.expected_output_shape, required_only=True)

    if declared_required_outputs and snippet_outputs:
        missing = sorted(declared_required_outputs - snippet_outputs)
        if missing:
            errors.append(
                "snippet expected_output_shape is missing required manifest output fields: "
                + ", ".join(missing)
            )

    if declared_outputs and snippet_outputs and not (declared_outputs & snippet_outputs):
        warnings.append("snippet expected_output_shape has no overlap with manifest output_schema")

    declared_inputs: set[str] = set()
    declared_required_inputs: set[str] = set()
    if capability.input_schema:
        props = capability.input_schema.get("properties") if isinstance(capability.input_schema, dict) else {}
        req = capability.input_schema.get("required") if isinstance(capability.input_schema, dict) else []
        if isinstance(props, dict):
            declared_inputs.update(str(key) for key in props.keys())
        if isinstance(req, list):
            declared_required_inputs.update(str(item) for item in req if isinstance(item, str))

    for fn in capability.functions:
        schema = fn.input_schema or {}
        props = schema.get("properties") if isinstance(schema, dict) else {}
        req = schema.get("required") if isinstance(schema, dict) else []
        if isinstance(props, dict):
            declared_inputs.update(str(key) for key in props.keys())
        if isinstance(req, list):
            declared_required_inputs.update(str(item) for item in req if isinstance(item, str))

    snippet_inputs: set[str] = set()
    schema = snippet.expected_input_shape or {}
    props = schema.get("properties") if isinstance(schema, dict) else {}
    if isinstance(props, dict):
        snippet_inputs.update(str(key) for key in props.keys())

    if declared_required_inputs and snippet_inputs:
        missing_inputs = sorted(declared_required_inputs - snippet_inputs)
        if missing_inputs:
            errors.append(
                "snippet expected_input_shape is missing required manifest input fields: "
                + ", ".join(missing_inputs)
            )

    if snippet_required_outputs - snippet_outputs:
        warnings.append("snippet required output fields are not present in snippet properties")

    return {
        "success": not errors,
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
    }

def _snippet_score(*, capability: ToolCapability, snippet: ToolSnippet, role: str, capabilities: list[str], tool_names: list[str], failure_layer: str | None, error_text: str | None) -> tuple[int, int, int, int, int, int]:
    applies = snippet.applies_to or {}
    snippet_caps = set(applies.get("capabilities") or snippet.requires or [])
    snippet_roles = set(applies.get("roles") or [])
    snippet_failures = set(applies.get("failure_layers") or [])
    haystack = (error_text or "").lower()
    helper_names = set(capability.helper_imports) | {fn.function_name for fn in capability.functions} | {capability.name}
    capability_match = len(snippet_caps & set(capabilities))
    role_match = 1 if role and role in snippet_roles else 0
    failure_match = 1 if failure_layer and failure_layer in snippet_failures else 0
    error_match = 1 if any(name.lower() in haystack for name in helper_names) else 0
    minimal = 1 if snippet.kind in {"minimal_usage", "error_repair_usage"} else 0
    tool_match = 1 if capability.name in tool_names or any(name in tool_names for name in helper_names) else 0
    return (capability_match + tool_match, role_match, failure_match, error_match, int(snippet.priority or 0), minimal)


def resolve_tool_snippets_for_context(
    *,
    role: str,
    capabilities: list[str],
    tool_names: list[str],
    file_path: str,
    failure_layer: str | None = None,
    error_text: str | None = None,
    max_snippets: int = 5,
) -> list[dict[str, Any]]:
    max_snippets = max(1, min(int(max_snippets or 5), 10))
    candidates: list[tuple[tuple[int, int, int, int, int, int], ToolCapability, ToolSnippet]] = []
    requested_tools = set(tool_names or [])
    requested_caps = set(capabilities or [])
    for cap in list_tool_capabilities():
        status = tool_status(cap)
        if not status.get("creator_available"):
            continue
        helper_names = set(cap.helper_imports) | {fn.function_name for fn in cap.functions}
        if requested_tools and cap.name not in requested_tools and not (helper_names & requested_tools):
            continue
        if not requested_tools and requested_caps and cap.name not in requested_caps and not (set(cap.required_capabilities or [cap.name]) & requested_caps):
            continue
        if cap.roles and role and role not in cap.roles and cap.name not in {"file_output", "deterministic_execution"}:
            # Still allow explicit error-text matches during repair.
            haystack = (error_text or "").lower()
            if not any(name.lower() in haystack for name in helper_names | {cap.name}):
                continue
        for snippet in snippets_for_tool(cap):
            score = _snippet_score(
                capability=cap,
                snippet=snippet,
                role=role,
                capabilities=capabilities or [],
                tool_names=tool_names or [],
                failure_layer=failure_layer,
                error_text=error_text,
            )
            if any(score[:4]) or not requested_tools:
                candidates.append((score, cap, snippet))
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [
        {"tool": cap.name, **asdict(snippet), "formatted": format_tool_snippet(cap, snippet)}
        for _, cap, snippet in candidates[:max_snippets]
    ]


def tool_snippet_prompt(snippets: list[dict[str, Any]]) -> str:
    if not snippets:
        return "当前脚本可用工具 Snippets: 无"
    return "当前脚本可用工具 Snippets（调用任何工具前必须优先参考；不要根据函数名猜参数或返回值；若 snippet 与猜测冲突，以 snippet 为准）：\n\n" + "\n\n---\n\n".join(str(item.get("formatted") or "") for item in snippets)


def set_tool_snippets(name: str, snippets: list[ToolSnippet]) -> ToolCapability | None:
    global BUILTIN_TOOL_CAPABILITIES
    current = BUILTIN_TOOL_CAPABILITIES.get(name) or _REGISTERED_TOOL_CAPABILITIES.get(name)
    if current is None:
        return None
    updated = replace(current, snippets=snippets, updated_at=_utc_now())
    if name in BUILTIN_TOOL_CAPABILITIES:
        BUILTIN_TOOL_CAPABILITIES[name] = updated
    else:
        _REGISTERED_TOOL_CAPABILITIES[name] = updated
    return get_tool_capability(name)


def function_cards_for_tool(capability: ToolCapability) -> list[str]:
    """Return Creator prompt cards with function-level I/O and call contracts."""
    functions = list(capability.functions)
    if not functions:
        functions = [
            ToolFunctionManifest(
                function_name=helper,
                import_path=capability.helper_module,
                short_description=capability.prompt_guidance or capability.display_name,
                when_to_use=f"Use for capability {capability.name} when role/capability resolution allows it.",
                signature=f"{helper}(...) -> dict",
                input_schema=capability.input_schema,
                output_schema=capability.output_schema,
                return_contract="Return the helper result directly when it already contains platform stdout fields; do not wrap helper result in the wrong key.",
                example_call=f"from {capability.helper_module} import {helper}\nresult = {helper}(...)\nreturn result",
                example_stdout="return result",
                common_mistakes=[
                    "Do not guess parameters; inspect the signature or follow the generated adapter contract.",
                    "Do not wrap a platform stdout dict inside another unrelated field.",
                ],
                trial_mode_behavior=str(capability.trial_mode),
                safety_notes=[capability.prompt_guidance] if capability.prompt_guidance else [],
                required_env=capability.required_env,
                required_secrets=capability.required_secrets,
                usage_policy=capability.usage_policy,
                allowed_roles=capability.allowed_roles or capability.roles,
                required_capabilities=capability.required_capabilities or [capability.name],
                forbidden_imports=capability.forbidden_direct_imports,
            )
            for helper in capability.helper_imports
        ]
    cards: list[str] = []
    for fn in functions:
        mistakes = "\n".join(f"  - {item}" for item in fn.common_mistakes) or "  - None declared"
        safety = "\n".join(f"  - {item}" for item in fn.safety_notes) or "  - Follow platform sandbox and OUTPUT_DIR rules."
        import_stmt = f"from {fn.import_path} import {fn.function_name}" if fn.import_path else f"import {fn.function_name}"
        cards.append(
            "\n".join([
                f"Tool: {capability.name}.{fn.function_name}",
                f"Purpose: {fn.short_description}",
                f"When to use: {fn.when_to_use}",
                f"Import: {import_stmt}",
                f"Signature: {fn.signature}",
                f"Input schema: {_schema_summary(fn.input_schema)}",
                f"Output schema: {_schema_summary(fn.output_schema)}",
                f"Return contract: {fn.return_contract}",
                f"Example call: {fn.example_call}",
                f"Example stdout/return: {fn.example_stdout or fn.example_return}",
                "Common mistakes:",
                mistakes,
                f"Trial mode behavior: {fn.trial_mode_behavior}",
                "Safety notes:",
                safety,
                f"Usage policy: {fn.usage_policy or capability.usage_policy}",
                f"Allowed roles: {', '.join(fn.allowed_roles or capability.allowed_roles or capability.roles) if (fn.allowed_roles or capability.allowed_roles or capability.roles) else 'all'}",
                f"Required capabilities: {', '.join(fn.required_capabilities or capability.required_capabilities or [capability.name])}",
                f"Required env: {', '.join(fn.required_env or capability.required_env) if (fn.required_env or capability.required_env) else 'none'}",
                f"Required secrets: {', '.join(fn.required_secrets or capability.required_secrets) if (fn.required_secrets or capability.required_secrets) else 'none'}",
                f"Forbidden imports: {', '.join(fn.forbidden_imports or capability.forbidden_direct_imports) if (fn.forbidden_imports or capability.forbidden_direct_imports) else 'none'}",
                f"Forbidden side effects: {', '.join(fn.forbidden_side_effects) if fn.forbidden_side_effects else 'none'}",
            ])
        )
    return cards


def _entry_capabilities(entry: Any) -> list[str]:
    values: list[str] = []
    for attr in ("required_capabilities", "optional_capabilities", "allowed_capabilities"):
        raw = getattr(entry, attr, None) if not isinstance(entry, dict) else entry.get(attr)
        if isinstance(raw, list):
            values.extend(str(item) for item in raw if item)
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _entry_role(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("role") or "")
    return str(getattr(entry, "role", "") or "")


def resolve_tools_for_skill_plan_entry(entry: Any) -> ToolResolveResult:
    """Resolve Creator-usable helpers for a SkillPlan entry.

    This is the pre-generation Tool Resolve step.  It is the only place that
    converts role/capability declarations into helper names exposed to the
    model.  Disabled tools, disallowed Creator tools, missing env/secret, and
    missing runtime helpers are excluded before prompt construction.
    """
    role = _entry_role(entry)
    capabilities = _entry_capabilities(entry)
    allowed_tools: list[str] = []
    allowed_helper_imports: list[str] = []
    required_dependencies: list[str] = []
    forbidden_imports: list[str] = []
    guidance: list[str] = []
    tool_function_cards: list[str] = []
    warnings: list[str] = []

    for capability in capabilities:
        cap = get_tool_capability(capability)
        if not cap:
            warnings.append(f"unknown capability {capability!r} has no registered tool")
            continue
        status = tool_status(cap)
        if not status["enabled"]:
            warnings.append(f"tool {cap.name} is disabled")
            continue
        if not status["creator_available"]:
            warnings.append(f"tool {cap.name} is not allowed for Creator use")
            continue
        if cap.roles and role and role not in cap.roles and capability not in {"file_output", "deterministic_execution"}:
            warnings.append(f"tool {cap.name} is not allowed for role {role}")
            continue
        if status["missing_env"] or status["missing_secrets"]:
            warnings.append(f"tool {cap.name} is not configured: missing env/secret")
            continue
        if cap.helper_imports and status["missing_runtime_helpers"] and cap.usage_policy == "helper_required":
            warnings.append(f"tool {cap.name} missing required runtime helpers: {', '.join(status['missing_runtime_helpers'])}")
            continue
        allowed_tools.append(cap.name)
        allowed_helper_imports.extend(status["runtime_helpers_available"] or cap.helper_imports)
        required_dependencies.extend(cap.dependencies)
        if cap.usage_policy == "helper_required":
            forbidden_imports.extend(cap.forbidden_direct_imports)
        tool_function_cards.extend(function_cards_for_tool(cap))
        if cap.prompt_guidance:
            guidance.append(f"- {cap.name}: {cap.prompt_guidance}")

    # de-duplicate preserving order
    def dedupe(items: list[str]) -> list[str]:
        seen: set[str] = set(); out: list[str] = []
        for item in items:
            if item and item not in seen:
                seen.add(item); out.append(item)
        return out

    allowed_helper_imports = dedupe(allowed_helper_imports)
    allowed_tools = dedupe(allowed_tools)
    required_dependencies = dedupe(required_dependencies)
    forbidden_imports = dedupe(forbidden_imports)
    helper_line = (
        "可优先使用的 backend.services.skill_runtime helper: "
        + (", ".join(allowed_helper_imports) if allowed_helper_imports else "无")
        + "。"
    )
    policy_lines = [
        f"- {cap.name}: usage_policy={cap.usage_policy}; allowed_roles={', '.join(cap.roles) if cap.roles else 'all'}; "
        f"required_env={', '.join(cap.required_env) if cap.required_env else '无'}; "
        f"required_secrets={', '.join(cap.required_secrets) if cap.required_secrets else '无'}; "
        f"dependencies={', '.join(cap.dependencies) if cap.dependencies else '无'}; "
        f"required_capabilities={', '.join(cap.required_capabilities or [cap.name])}; "
        f"optional_capabilities={', '.join(cap.optional_capabilities) if cap.optional_capabilities else '无'}; "
        f"forbidden_capabilities={', '.join(cap.forbidden_capabilities or _ROLE_FORBIDDEN_CAPABILITIES.get(role, [])) if (cap.forbidden_capabilities or _ROLE_FORBIDDEN_CAPABILITIES.get(role, [])) else '无'}"
        for cap_name in allowed_tools
        for cap in [get_tool_capability(cap_name)]
        if cap is not None
    ]
    forbid_line = (
        "helper_required 工具禁止直接 import/调用底层库或绕过 helper: " + ", ".join(forbidden_imports) + "。"
        if forbidden_imports else
        "除 usage_policy=helper_required 的能力外，helper 是可用/推荐工具，不强制实现方式；最终以 E2E stdout/artifact 合同为准。"
    )
    resolved_snippets = resolve_tool_snippets_for_context(
        role=role,
        capabilities=capabilities,
        tool_names=allowed_tools + allowed_helper_imports,
        file_path=str(getattr(entry, "path", "") if not isinstance(entry, dict) else entry.get("path", "")),
        max_snippets=5,
    ) if allowed_tools or allowed_helper_imports else []
    snippet_prompt = tool_snippet_prompt(resolved_snippets)
    cards_text = "\n\n".join(tool_function_cards)
    card_header = "当前脚本可用工具 Function Cards（作为 schema 补充；真实调用优先模仿 Tool Snippets，不要只凭函数名猜参数/返回值）:" if tool_function_cards else "当前脚本可用工具 Function Cards: 无"
    tool_usage_prompt = "\n".join([helper_line, forbid_line, *policy_lines, *guidance, snippet_prompt, card_header, cards_text])
    return ToolResolveResult(
        allowed_tools=allowed_tools,
        allowed_helper_imports=allowed_helper_imports,
        required_dependencies=required_dependencies,
        forbidden_imports=forbidden_imports,
        tool_function_cards=tool_function_cards,
        tool_snippets=resolved_snippets,
        tool_usage_prompt=tool_usage_prompt,
        warnings=warnings,
    )

def list_tool_capabilities() -> list[ToolCapability]:
    return [_with_overrides(cap) for cap in [*BUILTIN_TOOL_CAPABILITIES.values(), *_REGISTERED_TOOL_CAPABILITIES.values()]]


def get_tool_capability(name: str) -> ToolCapability | None:
    key = (name or "").strip()
    cap = BUILTIN_TOOL_CAPABILITIES.get(key) or _REGISTERED_TOOL_CAPABILITIES.get(key)
    return _with_overrides(cap) if cap else None


def register_tool_capability(capability: ToolCapability) -> ToolCapability:
    """Register a user/admin-provided Creator tool capability in process memory."""
    if not capability.name:
        raise ValueError("registered tool capability name is required")
    _REGISTERED_TOOL_CAPABILITIES[capability.name] = capability
    global _RUNTIME_HELPERS_CACHE
    _RUNTIME_HELPERS_CACHE = None
    return capability


def clear_registered_tool_capabilities() -> None:
    _REGISTERED_TOOL_CAPABILITIES.clear()
    global _RUNTIME_HELPERS_CACHE
    _RUNTIME_HELPERS_CACHE = None


def set_tool_capability_override(name: str, *, enabled: bool | None = None, allow_creator_use: bool | None = None) -> ToolCapability | None:
    if name not in BUILTIN_TOOL_CAPABILITIES and name not in _REGISTERED_TOOL_CAPABILITIES:
        return None
    current = dict(_TOOL_OVERRIDES.get(name, {}))
    if enabled is not None:
        current["enabled"] = bool(enabled)
    if allow_creator_use is not None:
        current["allow_creator_use"] = bool(allow_creator_use)
    _TOOL_OVERRIDES[name] = current
    return get_tool_capability(name)


def capabilities_for_role(role: str, *, only_creator_enabled: bool = True) -> tuple[list[str], list[str]]:
    role = (role or "").strip()
    capabilities = list_tool_capabilities()
    if only_creator_enabled:
        capabilities = [
            cap
            for cap in capabilities
            if cap.enabled_by_default and cap.allow_creator_use
        ]
    required = [cap.name for cap in capabilities if role in cap.roles]
    return required, list(_ROLE_FORBIDDEN_CAPABILITIES.get(role, []))


def roles() -> list[str]:
    values = {role for cap in [*BUILTIN_TOOL_CAPABILITIES.values(), *_REGISTERED_TOOL_CAPABILITIES.values()] for role in cap.roles}
    return sorted(values)


def get_script_roles() -> list[str]:
    return [role for role in roles() if role not in RESOURCE_ROLES]


def is_resource_role(role: str) -> bool:
    return (role or "").strip() in RESOURCE_ROLES


def is_script_role(role: str) -> bool:
    return (role or "").strip() in set(get_script_roles())


def role_regex() -> str:
    return "|".join(re.escape(role) for role in sorted(roles(), key=len, reverse=True))


def get_role_pattern() -> str:
    return role_regex()


def validate_capability_names(names: list[str]) -> list[str]:
    known = set(BUILTIN_TOOL_CAPABILITIES) | set(_REGISTERED_TOOL_CAPABILITIES)
    return [name for name in names if name not in known]



def build_tool_manifest_draft(description: dict[str, Any]) -> dict[str, Any]:
    '''Deterministically draft a complete function-level manifest from NL form fields.'''
    name = _slug(str(description.get("tool_name") or description.get("name") or description.get("display_name") or "custom_tool"))
    display_name = str(description.get("display_name") or description.get("tool_name") or name.replace("_", " ").title())
    tool_type = str(description.get("tool_type") or "python_helper")
    output_generates_file = bool(description.get("generates_file"))
    safety_level = "high" if description.get("high_risk") else ("medium" if description.get("needs_external_network") or description.get("needs_secret") else "low")
    usage_policy = "helper_required" if safety_level == "high" else "helper_preferred"
    roles = [str(item) for item in description.get("allowed_roles") or [] if item] or ["generic_script", "composite_generator"]
    capability = _slug(str(description.get("capability") or name))
    input_schema = description.get("input_schema") if isinstance(description.get("input_schema"), dict) else {
        "payload": {"type": "object", "required": True, "description": str(description.get("input_description") or "Tool input payload.")}
    }
    output_schema = description.get("output_schema") if isinstance(description.get("output_schema"), dict) else {
        "result": {"type": "object", "description": str(description.get("output_description") or "Tool result.")}
    }
    if output_generates_file:
        output_schema.setdefault("file_paths", {"type": "array[string]", "description": "Generated files under OUTPUT_DIR."})
        output_schema.setdefault("file_outputs", {"type": "array[object]", "description": "Platform downloadable file metadata."})
    adapter_import = f"backend.services.runtime_tools.custom_tools.{name}"
    return {
        "name": name, "display_name": display_name, "category": str(description.get("category") or ("document" if output_generates_file else "custom")),
        "capability": capability, "tool_type": tool_type if tool_type in _ALLOWED_TOOL_TYPES else "custom_adapter", "usage_policy": usage_policy,
        "allowed_roles": roles, "roles": roles, "required_capabilities": [capability],
        "required_env": [str(item) for item in description.get("required_env") or [] if item],
        "required_secrets": [str(item) for item in description.get("required_secrets") or [] if item] + (["TOOL_API_KEY"] if description.get("needs_secret") else []),
        "dependencies": [str(item) for item in description.get("dependencies") or [] if item], "safety_level": safety_level,
        "enabled": False, "approval_status": "draft", "test_status": "untested", "adapter_path": f"backend/services/runtime_tools/custom_tools/{name}.py",
        "version": "0.1.0",
        "functions": [{
            "function_name": name, "import_path": adapter_import,
            "short_description": str(description.get("short_description") or description.get("purpose") or description.get("description") or display_name),
            "when_to_use": str(description.get("when_to_use") or f"Use when a Creator script needs {display_name}."),
            "signature": f"{name}(payload: dict) -> dict", "input_schema": input_schema, "output_schema": output_schema,
            "return_contract": "Returns a dict conforming to output_schema. If file_outputs/file_paths are returned, paths must exist under OUTPUT_DIR.",
            "example_call": f"from {adapter_import} import {name}\nresult = {name}(payload)\nreturn result",
            "example_stdout": "return result", "example_return": "{...output_schema fields...}",
            "common_mistakes": ["Do not guess parameter names; follow the signature and input_schema.", "Do not print or return secret values.", "Do not write files outside OUTPUT_DIR."],
            "trial_mode_behavior": str(description.get("trial_mode_behavior") or "When SKILL_TRIAL_RUN=1, return a minimal deterministic mock that still satisfies output_schema."),
            "safety_notes": ["Validate paths before reading or writing.", "Declare every env var, secret, network host, and side effect in the manifest."],
            "required_env": [str(item) for item in description.get("required_env") or [] if item], "required_secrets": [str(item) for item in description.get("required_secrets") or [] if item],
            "usage_policy": usage_policy, "allowed_roles": roles, "required_capabilities": [capability],
            "forbidden_imports": sorted(_DANGEROUS_IMPORTS), "forbidden_side_effects": ["write outside OUTPUT_DIR", "leak secrets", "undeclared network access"],
        }],
        "snippets": [{
            "id": f"{name}.minimal_usage", "title": f"Use {display_name}", "kind": "minimal_usage",
            "applies_to": {"roles": roles, "capabilities": [capability], "failure_layers": ["helper_call_failed", "final_platform_output_value_invalid", "artifact_missing"]},
            "description": str(description.get("when_to_use") or f"Use when a Creator script needs {display_name}."),
            "code": f"from {adapter_import} import {name}\n\npayload = {{...}}\nresult = {name}(payload)\nreturn result",
            "expected_input_shape": input_schema, "expected_output_shape": output_schema,
            "return_rule": "Return the adapter result directly when it already matches output_schema; do not wrap it in another field.",
            "anti_patterns": ["Do not guess parameter names; pass a payload dict unless the signature says otherwise.", "Do not print or return secret values.", "Do not write files outside OUTPUT_DIR."],
            "requires": [capability], "usage_policy": usage_policy, "priority": 80,
        }],
    }


def generate_adapter_code(manifest: dict[str, Any]) -> str:
    cap = _capability_from_dict(manifest)
    fn = cap.functions[0] if cap.functions else _function_from_dict(build_tool_manifest_draft(manifest)["functions"][0])
    output_keys = list((fn.output_schema or {}).keys()) or ["result"]
    input_keys = list((fn.input_schema or {}).keys())
    return f"""# Generated adapter for registered Creator tool: {cap.name}.

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


OUTPUT_KEYS = {output_keys!r}
INPUT_KEYS = {input_keys!r}


def _output_dir() -> Path:
    root = Path(os.environ.get(\"OUTPUT_DIR\", \"outputs\")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_filename(value: str, default: str = \"result\") -> str:
    cleaned = \"\".join(ch if ch.isalnum() or ch in (\"-\", \"_\", \".\") else \"_\" for ch in value.strip())
    return (cleaned or default)[:120]


def _build_file(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    output_dir = _output_dir()
    title = str(payload.get(\"title\") or payload.get(\"name\") or \"result\")
    suffix = str(payload.get(\"extension\") or payload.get(\"suffix\") or \"txt\").lstrip(\".\") or \"txt\"
    if suffix not in {{\"txt\", \"md\", \"json\", \"csv\", \"html\"}}:
        suffix = \"txt\"
    path = (output_dir / f\"{{_safe_filename(title)}}.{{suffix}}\").resolve()
    if output_dir not in path.parents and path != output_dir:
        raise ValueError(\"generated file path must stay under OUTPUT_DIR\")
    content = payload.get(\"content\") or payload.get(\"markdown\") or payload.get(\"text\") or json.dumps(payload, ensure_ascii=False, indent=2)
    path.write_text(str(content), encoding=\"utf-8\")
    return str(path), {{\"path\": str(path), \"mime_type\": \"text/plain\", \"label\": title}}


def run(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    \"\"\"{fn.short_description}\"\"\"
    payload = dict(payload or {{}})
    if os.getenv(\"SKILL_TRIAL_RUN\") == \"1\":
        mock_result: dict[str, Any] = {{\"result\": {{\"ok\": True, \"trial_run\": True, \"payload_keys\": sorted(payload.keys())}}}}
        for key in OUTPUT_KEYS:
            mock_result.setdefault(key, [] if key.endswith(\"s\") else {{\"ok\": True, \"trial_run\": True}})
        return mock_result
    result: dict[str, Any] = {{\"result\": {{\"ok\": True, \"payload_keys\": sorted(payload.keys())}}}}
    wants_file = any(key in OUTPUT_KEYS for key in (\"file_paths\", \"file_outputs\", \"path\", \"output_path\"))
    if wants_file:
        path, file_output = _build_file(payload)
        result.update({{\"path\": path, \"output_path\": path, \"file_paths\": [path], \"file_outputs\": [file_output]}})
    for key in OUTPUT_KEYS:
        if key in result:
            continue
        if \"path\" in key:
            path, _file_output = _build_file(payload)
            result[key] = path
        elif key.endswith(\"s\"):
            result[key] = []
        else:
            result[key] = {{\"ok\": True}}
    return result


def {fn.function_name}(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return run(payload)


def main() -> None:
    raw = sys.stdin.read().strip() or \"{{}}\"
    payload = json.loads(raw)
    print(json.dumps(run(payload), ensure_ascii=False))


if __name__ == \"__main__\":
    main()
"""

def _manifest_errors(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    try:
        cap = _capability_from_dict(manifest)
    except Exception as exc:
        return [f"manifest cannot be parsed: {exc}"]
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", cap.name or ""):
        errors.append("name must be a valid Python identifier-like slug")
    if cap.tool_type not in _ALLOWED_TOOL_TYPES:
        errors.append(f"tool_type must be one of {sorted(_ALLOWED_TOOL_TYPES)}")
    if cap.usage_policy not in _ALLOWED_USAGE_POLICIES:
        errors.append("usage_policy is invalid")
    if cap.safety_level not in {"low", "medium", "high", "standard"}:
        errors.append("safety_level must be low/medium/high")
    if not cap.functions:
        errors.append("at least one function manifest is required")
    for fn in cap.functions:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", fn.function_name or ""):
            errors.append(f"invalid function_name: {fn.function_name!r}")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", fn.import_path or ""):
            errors.append(f"invalid import_path for {fn.function_name}")
        if not fn.signature or "(" not in fn.signature or ")" not in fn.signature:
            errors.append(f"signature is not parseable for {fn.function_name}")
        if not isinstance(fn.input_schema, dict) or not fn.input_schema:
            errors.append(f"input_schema is required for {fn.function_name}")
        if not isinstance(fn.output_schema, dict) or not fn.output_schema:
            errors.append(f"output_schema is required for {fn.function_name}")
    for snippet in snippets_for_tool(cap):
        result = validate_tool_snippet(cap, snippet)
        errors.extend(f"snippet {snippet.id}: {err}" for err in result["errors"])
    if set(cap.required_capabilities) & _HIGH_RISK_CAPABILITIES and cap.approval_status not in {"approved", "validated"}:
        errors.append("high-risk tools must be validated and admin-approved before enabling")
    return errors


def _code_security_errors(code: str, manifest: dict[str, Any] | None = None) -> list[str]:
    """Adapter-level security checks.

    This checks full generated adapter code, so it must distinguish platform wrapper
    code from model-written core logic. Wrapper-owned operations such as file_io.open
    or local_command.subprocess are allowed only for their wrapper_family.
    """
    errors: list[str] = []
    manifest = manifest if isinstance(manifest, dict) else {}
    wrapper_family = _canonical_wrapper_family(str(manifest.get("wrapper_family") or ""))

    try:
        tree = ast.parse(code or "")
    except SyntaxError as exc:
        return [f"adapter code syntax error: {exc}"]

    allowed_wrapper_imports = {
        "json",
        "os",
        "sys",
        "re",
        "sqlite3",
        "inspect",
        "requests",
        "pathlib",
        "typing",
        "datetime",
        "backend",
    }

    if wrapper_family == "local_command":
        allowed_wrapper_imports.add("subprocess")

    if wrapper_family != "database_query":
        # sqlite3 只允许 database_query wrapper 平台代码使用。
        allowed_wrapper_imports.discard("sqlite3")

    if wrapper_family != "http_api":
        # requests 只允许 http_api wrapper 平台代码使用。
        allowed_wrapper_imports.discard("requests")

    dangerous_imports = {"shutil", "socket", "paramiko", "ftplib", "telnetlib"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = str(alias.name or "").split(".")[0]

                if root in dangerous_imports:
                    errors.append(f"dangerous import is forbidden: {root}")

                if root == "subprocess" and wrapper_family != "local_command":
                    errors.append("subprocess import is only allowed inside local_command wrapper")

                if root == "sqlite3" and wrapper_family != "database_query":
                    errors.append("sqlite3 import is only allowed inside database_query wrapper")

                if root == "requests" and wrapper_family != "http_api":
                    errors.append("requests import is only allowed inside http_api wrapper")

        elif isinstance(node, ast.ImportFrom):
            root = str(node.module or "").split(".")[0]

            if root in dangerous_imports:
                errors.append(f"dangerous import is forbidden: {root}")

            if root == "subprocess" and wrapper_family != "local_command":
                errors.append("subprocess import is only allowed inside local_command wrapper")

            if root == "sqlite3" and wrapper_family != "database_query":
                errors.append("sqlite3 import is only allowed inside database_query wrapper")

            if root == "requests" and wrapper_family != "http_api":
                errors.append("requests import is only allowed inside http_api wrapper")

        elif isinstance(node, ast.Call):
            func = node.func
            called = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""

            if called in {"eval", "exec", "compile", "__import__"}:
                errors.append(f"dangerous call requires a controlled helper: {called}")

            if called == "open" and wrapper_family != "file_io":
                errors.append("open is only allowed inside file_io wrapper/core logic")

            if called in {"system", "popen"}:
                errors.append(f"dangerous process call is forbidden: {called}")

    if re.search(r"(?:sk-|AKIA|-----BEGIN [A-Z ]*PRIVATE KEY-----)[A-Za-z0-9_\-+/=]{8,}", code or ""):
        errors.append("adapter appears to contain a hard-coded secret")

    return sorted(set(errors))


def _safe_adapter_module_path(cap: ToolCapability) -> Path:
    """Return the only allowed persistent adapter path for a custom tool."""
    return (CUSTOM_TOOL_ADAPTER_DIR / f"{_slug(cap.name)}.py").resolve()


def _adapter_module_path(cap: ToolCapability) -> Path:
    """Resolve persisted adapters without trusting arbitrary manifest paths."""
    return _safe_adapter_module_path(cap)


def safe_adapter_path_for_manifest(manifest: dict[str, Any]) -> str:
    """Return the normalized repository-relative adapter path for a manifest."""
    cap = _capability_from_dict(manifest)
    path = _safe_adapter_module_path(cap)
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def normalize_tool_manifest_adapter_path(manifest: dict[str, Any]) -> dict[str, Any]:
    """Force registered custom adapters into CUSTOM_TOOL_ADAPTER_DIR."""
    payload = dict(manifest or {})
    cap = _capability_from_dict(payload)
    payload["adapter_path"] = safe_adapter_path_for_manifest(payload)
    adapter_import = f"backend.services.runtime_tools.custom_tools.{_slug(cap.name)}"
    functions = []
    for item in payload.get("functions") or []:
        if isinstance(item, dict):
            fn = dict(item)
            fn["import_path"] = adapter_import
            functions.append(fn)
    if functions:
        payload["functions"] = functions
    return payload


def write_registered_adapter(manifest: dict[str, Any], adapter_code: str | None) -> dict[str, Any]:
    """Persist confirmed adapter code and return a path-normalized manifest."""
    payload = normalize_tool_manifest_adapter_path(manifest)
    if adapter_code:
        cap = _capability_from_dict(payload)
        path = _safe_adapter_module_path(cap)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(adapter_code, encoding="utf-8")
    return payload

def _run_adapter_once(
    *,
    cap: ToolCapability,
    path: Path,
    sample_input: dict[str, Any],
    trial: bool,
    extra_sys_path: list[str] | None = None,
) -> dict[str, Any]:
    """Import and run a generated adapter once.

    extra_sys_path is used for temporary dependency installation directories
    created during authoring validation.
    """
    if not path.exists():
        raise FileNotFoundError(f"adapter file does not exist: {path}")

    function_name = ""
    if cap.functions:
        function_name = str(cap.functions[0].function_name or "").strip()

    payload = dict(sample_input or {})

    old_trial = os.environ.get("SKILL_TRIAL_RUN")
    old_output_dir = os.environ.get("OUTPUT_DIR")
    old_sys_path = list(sys.path)

    temp_output_dir = tempfile.TemporaryDirectory(prefix="creator_tool_output_")

    module_name = f"_creator_tool_validate_{_slug(cap.name)}_{abs(hash(str(path)))}"

    try:
        if trial:
            os.environ["SKILL_TRIAL_RUN"] = "1"
        else:
            os.environ.pop("SKILL_TRIAL_RUN", None)

        os.environ["OUTPUT_DIR"] = temp_output_dir.name

        # Temporary dependency target dirs should be searched before global site-packages.
        for item in reversed(extra_sys_path or []):
            item = str(item or "").strip()
            if item and item not in sys.path:
                sys.path.insert(0, item)

        # Also make the adapter directory importable for local sibling imports.
        adapter_parent = str(path.parent.resolve())
        if adapter_parent not in sys.path:
            sys.path.insert(0, adapter_parent)

        spec = importlib.util.spec_from_file_location(module_name, str(path))
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load adapter module from {path}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module

        try:
            spec.loader.exec_module(module)
        finally:
            # Avoid stale module reuse between repair attempts.
            sys.modules.pop(module_name, None)

        runner = getattr(module, "run", None)

        if not callable(runner) and function_name:
            candidate = getattr(module, function_name, None)
            if callable(candidate):
                runner = candidate

        if not callable(runner):
            raise RuntimeError("adapter must expose run(payload) or manifest function")

        value = runner(payload)

        if not isinstance(value, dict):
            value = {
                "success": True,
                "result": value,
            }

        return value

    finally:
        if old_trial is None:
            os.environ.pop("SKILL_TRIAL_RUN", None)
        else:
            os.environ["SKILL_TRIAL_RUN"] = old_trial

        if old_output_dir is None:
            os.environ.pop("OUTPUT_DIR", None)
        else:
            os.environ["OUTPUT_DIR"] = old_output_dir

        sys.path[:] = old_sys_path

        temp_output_dir.cleanup()

def _declared_output_field_names(output_schema: dict[str, Any] | None, *, required_only: bool = False) -> set[str]:
    """Extract business output field names from either JSON Schema or legacy field-map schema.

    JSON Schema shape:
      {"type": "object", "properties": {"success": ...}, "required": ["success"]}

    Legacy field-map shape:
      {"success": {"type": "boolean"}, "results": {"type": "array"}}
    """
    if not isinstance(output_schema, dict) or not output_schema:
        return set()

    properties = output_schema.get("properties")
    required = output_schema.get("required")

    if isinstance(properties, dict):
        if required_only and isinstance(required, list):
            return {str(item) for item in required if isinstance(item, str) and item in properties}
        return {str(key) for key in properties.keys()}

    meta_keys = {"type", "properties", "required", "description", "title", "$schema", "additionalProperties"}
    field_names = {str(key) for key in output_schema.keys() if key not in meta_keys}

    if required_only and isinstance(required, list):
        return {str(item) for item in required if isinstance(item, str) and item in field_names}

    return field_names

def validate_tool_manifest(
    manifest: dict[str, Any],
    *,
    adapter_code: str | None = None,
    sample_input: dict[str, Any] | None = None,
    dynamic: bool = True,
    real_run: bool = False,
) -> dict[str, Any]:
    _load_tool_authoring_config_store_from_disk()

    manifest = dict(manifest or {})

    def _schema_props(schema: Any) -> dict[str, Any]:
        if not isinstance(schema, dict):
            return {}

        props = schema.get("properties")
        if isinstance(props, dict):
            return props

        if schema.get("type") == "object":
            return {}

        meta_keys = {
            "type",
            "properties",
            "required",
            "description",
            "title",
            "$schema",
            "additionalProperties",
        }

        return {
            str(key): value
            for key, value in schema.items()
            if key not in meta_keys and isinstance(value, dict)
        }

    def _merge_output_schema_without_overwrite(
        existing_schema: Any,
        generic_schema: dict[str, Any],
    ) -> dict[str, Any]:
        generic_schema = generic_schema if isinstance(generic_schema, dict) else {}

        if not isinstance(existing_schema, dict) or not existing_schema:
            return generic_schema

        existing_props = _schema_props(existing_schema)
        generic_props = _schema_props(generic_schema)

        if existing_schema.get("type") == "object" or "properties" in existing_schema:
            merged_props = dict(existing_props)
            for key, value in generic_props.items():
                merged_props.setdefault(key, value)

            required: list[str] = []

            if isinstance(existing_schema.get("required"), list):
                required.extend(
                    str(item)
                    for item in existing_schema["required"]
                    if isinstance(item, str)
                )

            if isinstance(generic_schema.get("required"), list):
                for item in generic_schema["required"]:
                    if isinstance(item, str) and item not in required:
                        required.append(item)

            merged_schema = {
                **existing_schema,
                "type": "object",
                "properties": merged_props,
            }

            if required:
                merged_schema["required"] = required

            return merged_schema

        merged = dict(existing_schema)

        for key, value in generic_schema.items():
            if key in {
                "type",
                "properties",
                "required",
                "description",
                "title",
                "$schema",
                "additionalProperties",
            }:
                continue
            merged.setdefault(key, value)

        for key, value in generic_props.items():
            merged.setdefault(key, value)

        return merged

    is_external_api = (
        str(manifest.get("category") or "").lower() == "external_api"
        or str(manifest.get("type") or "").lower() == "external_api"
        or str(manifest.get("tool_kind") or "").lower() == "external_api"
        or _canonical_wrapper_family(str(manifest.get("wrapper_family") or "")) == "http_api"
        or manifest.get("needs_external_network") is True
    )

    if is_external_api:
        normalized_output_schema = _generic_external_api_output_schema()

        manifest["output_schema"] = _merge_output_schema_without_overwrite(
            manifest.get("output_schema"),
            normalized_output_schema,
        )

        fixed_functions = []
        for fn in manifest.get("functions") or []:
            if isinstance(fn, dict):
                fixed_fn = dict(fn)
                fixed_fn["output_schema"] = _merge_output_schema_without_overwrite(
                    fixed_fn.get("output_schema") or manifest.get("output_schema"),
                    normalized_output_schema,
                )
                fixed_functions.append(fixed_fn)
            else:
                fixed_functions.append(fn)

        if fixed_functions:
            manifest["functions"] = fixed_functions

    errors = _manifest_errors(manifest)
    warnings: list[str] = []

    cap = _capability_from_dict(manifest) if not errors else None

    if adapter_code:
        try:
            errors.extend(_code_security_errors(adapter_code, manifest))
        except TypeError:
            errors.extend(_code_security_errors(adapter_code))

    dynamic_result: dict[str, Any] = {"skipped": not dynamic}
    real_result: dict[str, Any] = {"skipped": not real_run}
    dependency_result: dict[str, Any] = {
        "success": True,
        "skipped": True,
        "installed": [],
        "target_dir": "",
        "errors": [],
    }

    if cap and dynamic and not errors:
        temp_code_dir: tempfile.TemporaryDirectory[str] | None = None
        temp_deps_dir: tempfile.TemporaryDirectory[str] | None = None

        try:
            if adapter_code:
                temp_code_dir = tempfile.TemporaryDirectory(prefix="creator_tool_validate_")
                path = Path(temp_code_dir.name) / f"{_slug(cap.name)}.py"
                path.write_text(adapter_code, encoding="utf-8")
            else:
                path = _adapter_module_path(cap)

            if not path.exists():
                errors.append(f"adapter file does not exist: {path}")
            else:
                packages = _dependency_packages_for_install(manifest)
                extra_sys_path: list[str] = []

                if packages:
                    temp_deps_dir = tempfile.TemporaryDirectory(prefix="creator_tool_deps_")
                    deps_path = Path(temp_deps_dir.name)

                    dependency_result = _install_dependencies_to_target(
                        packages,
                        deps_path,
                        timeout_seconds=int(
                            os.environ.get(
                                "TOOL_AUTHOR_DEP_INSTALL_TIMEOUT_SECONDS",
                                "180",
                            )
                        ),
                    )

                    if not dependency_result.get("success"):
                        errors.extend(
                            dependency_result.get("errors")
                            or ["dependency installation failed"]
                        )
                    else:
                        extra_sys_path.append(str(deps_path))

                if not errors:
                    try:
                        value = _run_adapter_once(
                            cap=cap,
                            path=path,
                            sample_input=sample_input or {},
                            trial=True,
                            extra_sys_path=extra_sys_path,
                        )

                        output_schema = cap.functions[0].output_schema if cap.functions else {}
                        required = _declared_output_field_names(output_schema, required_only=True)
                        missing_required = [
                            key
                            for key in sorted(required)
                            if key not in value
                        ]

                        if missing_required:
                            errors.append(
                                "dynamic trial did not return required output fields: "
                                + ", ".join(missing_required)
                            )

                        dynamic_result = {
                            "skipped": False,
                            "return_keys": sorted(value.keys()),
                            "preview": _normalized_preview(value),
                        }

                    except Exception as exc:
                        errors.append(f"dynamic trial failed: {exc}")

                if real_run and not errors:
                    try:
                        value = _run_adapter_once(
                            cap=cap,
                            path=path,
                            sample_input=sample_input or {},
                            trial=False,
                            extra_sys_path=extra_sys_path,
                        )

                        if value.get("success") is False:
                            errors.append(
                                "real run returned success=false: "
                                + str(value.get("error") or value.get("message") or value)
                            )

                        output_schema = cap.functions[0].output_schema if cap.functions else {}
                        required = _declared_output_field_names(output_schema, required_only=True)
                        missing_required = [
                            key
                            for key in sorted(required)
                            if key not in value
                        ]

                        if missing_required:
                            errors.append(
                                "real run did not return required output fields: "
                                + ", ".join(missing_required)
                            )

                        real_result = {
                            "skipped": False,
                            "return_keys": sorted(value.keys()),
                            "preview": _normalized_preview(
                                _redact_secrets(value, set(os.environ.values()))
                            ),
                        }

                    except Exception as exc:
                        errors.append(f"real run failed: {exc}")

        finally:
            if temp_code_dir is not None:
                temp_code_dir.cleanup()

            if temp_deps_dir is not None:
                temp_deps_dir.cleanup()

    snippet_validations = [
        validate_tool_snippet(cap, snippet)
        for snippet in snippets_for_tool(cap)
    ] if cap else []

    success = not errors

    return {
        "success": success,
        "status": "validated" if success else "failed",
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
        "dynamic_trial": dynamic_result,
        "real_run": real_result,
        "dependency_environment": dependency_result,
        "tool_card_preview": function_cards_for_tool(cap) if cap else [],
        "snippet_preview": [
            format_tool_snippet(cap, snippet)
            for snippet in snippets_for_tool(cap)
        ] if cap else [],
        "snippet_validations": snippet_validations,
    }

def _json_from_model_text(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    match = re.search(r"\{.*\}", raw, re.S)
    if match:
        raw = match.group(0)
    try:
        value = json.loads(raw)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


async def _complete_author_model(task: str, messages: list[dict[str, str]], *, reason: str) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    """Call the routed model with a short authoring timeout and JSON parsing."""
    try:
        from .llm_proxy import complete_chat_once
        from .model_router import route_model

        route = route_model(task, reason=reason)
        text = await asyncio.wait_for(complete_chat_once(messages, route.model), timeout=float(os.environ.get(f"TOOL_AUTHOR_{task.upper()}_TIMEOUT_SECONDS", os.environ.get("TOOL_AUTHOR_LLM_TIMEOUT_SECONDS", "120"))))
        parsed = _json_from_model_text(text)
        if task == "code" and not parsed and (text or "").strip():
            parsed = {"code": _strip_code_fence(text)}
        return parsed, route.ack(), None
    except Exception as exc:
        return {}, None, str(exc)


def _infer_schema_from_code(code: str) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    input_keys: set[str] = set()
    output_keys: set[str] = set()
    notes: list[str] = []
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return {}, {}, ["code_block has syntax errors; code_model should repair before trial run"]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            target = node.func.value
            if isinstance(target, ast.Name) and target.id in {"payload", "data", "input"} and node.args and isinstance(node.args[0], ast.Constant):
                if isinstance(node.args[0].value, str):
                    input_keys.add(node.args[0].value)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id in {"payload", "data", "input"}:
            key = node.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                input_keys.add(key.value)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for key in node.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    output_keys.add(key.value)
    input_schema = {key: {"type": "string", "required": False, "description": f"Inferred from code_block field {key}."} for key in sorted(input_keys)}
    output_schema = {key: {"type": "object", "description": f"Inferred from code_block return field {key}."} for key in sorted(output_keys)}
    if input_keys:
        notes.append(f"inferred input fields from code_block: {', '.join(sorted(input_keys))}")
    if output_keys:
        notes.append(f"inferred output fields from code_block: {', '.join(sorted(output_keys))}")
    return input_schema, output_schema, notes


def _first_function_name(code: str) -> str:
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return "run"
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_") and node.name not in {"main"}:
            return node.name
    return "run"


def _has_config_value(config: dict[str, Any], *keys: str) -> bool:
    return any(config.get(key) not in (None, "", {}, []) for key in keys)


def _plan_defaults() -> dict[str, Any]:
    return {
        "needs_clarification": False,
        "questions": [],
        "clarification_questions": [],
        "requires_config": False,
        "config_required_fields": [],
        "config_form_schema": {},
        "suggested_entrypoint": {},
        "additional_fields_schema": [],
        "requires_authorization": False,
        "tool_kind": "unknown",
        "operation": "",
        "resolved_clarifications": [],
        "requires_secret": False,
        "secret_env_suggestions": [],
        "requires_external_network": False,
        "requires_live_test": False,
        "ready_for_live_test": False,
        "ready_for_code_generation": False,
        "missing_fields": [],
        "suggested_config_schema": {},
        "sample_input_schema": {},
        "manifest": {},
        "implementation_plan": "",
        "sample_input": {},
        "risk_notes": [],
        "model_notes": [],
        "requires_authoring_tools": False,
        "authoring_tool_plan": [],
        "authoring_context": {},
        "wrapper_family": "auto",
        "internal_function_name": "",
        "auth_decision": {
            "required": "unknown",
            "confidence": 0.0,
            "reason": "",
            "evidence": [],
            "security_schemes": [],
        },
        "auth_gate": {
            "status": "needs_review",
            "block_code_generation": False,
            "block_registration": True,
            "reasons": ["auth decision has not been evaluated"],
        },
        "runtime_facts": {},
    }

WRAPPER_REGISTRY: dict[str, dict[str, Any]] = {
    "http_api": {
        "description": "固定 HTTP/API 服务调用包装",
        "model_scope": "normalize_response_only",
        "auth_policy": "declared_by_contract",
    },
    "managed_helper": {
        "description": "平台已有 helper 的受控包装",
        "model_scope": "none",
        "auth_policy": "platform_or_contract",
    },
    "python_compute": {
        "description": "纯 Python 计算/解析/转换包装",
        "model_scope": "transform_only",
        "auth_policy": "declared_by_contract",
    },
    "file_io": {
        "description": "文件输入输出/格式转换包装",
        "model_scope": "file_transform_only",
        "auth_policy": "declared_by_contract",
    },
    "database_query": {
        "description": "数据库只读查询包装",
        "model_scope": "sql_template_or_normalize_only",
        "auth_policy": "declared_by_contract",
    },
    "local_command": {
        "description": "受限本地命令包装，高风险",
        "model_scope": "none",
        "auth_policy": "declared_by_contract",
    },
    "custom_adapter": {
        "description": "无法归类时的兜底自定义 adapter",
        "model_scope": "full_adapter_with_strict_review",
        "auth_policy": "declared_by_contract",
    },
}

CAPABILITY_TO_WRAPPER: dict[str, str] = {
    # Fixed remote APIs with stable endpoint/templates.
    "http_request": "http_api",

    # Platform-managed helpers. These are generic capability classes, not business cases.
    "network_read": "managed_helper",
    "web_search": "managed_helper",
    "text_generation": "managed_helper",
    "image_generation": "managed_helper",
    "pdf_generation": "managed_helper",
    "docx_generation": "managed_helper",
    "pptx_generation": "managed_helper",
    "pdf_parsing": "managed_helper",
    "docx_parsing": "managed_helper",
    "pptx_parsing": "managed_helper",
    "spreadsheet_read": "managed_helper",
    "vision_understanding": "managed_helper",

    # Local deterministic computation.
    "deterministic_execution": "python_compute",

    # File-producing local/document tools.
    "file_output": "file_io",
    "html_asset_generation": "file_io",
    "asset_generation": "file_io",

    # Restricted read-only database operations.
    "database_read": "database_query",

    # High-risk local command tools.
    "local_command": "local_command",
    "shell_command": "local_command",
}

_PROTOCOL_CONFIG_FIELDS = {
    "base_url",
    "endpoint",
    "url",
    "method",
    "auth_type",
    "authentication",
    "secret_env",
    "secret_env_name",
    "auth_placement",
    "auth_header_name",
    "auth_query_param",
    "database_url",
    "dsn",
    "connection_string",
}

_CONNECTION_SCOPES = {
    "connection_config",
    "auth_config",
    "secret_ref",
    "credential_ref",
}

_RUNTIME_SCOPES = {
    "runtime_input",
    "business_input",
    "payload",
}


def _canonical_wrapper_family(value: Any) -> str:
    """Normalize only wrapper-family aliases.

    Do not map business semantics such as web_fetch/search/summarizer/crawler
    into wrapper families here. Business semantics belong to tool_kind and must
    be resolved by planner/validator/repair through capabilities.
    """
    text = str(value or "").strip().lower()

    aliases = {
        "": "python_compute",
        "auto": "python_compute",
        "unknown": "python_compute",

        # Runtime wrapper aliases only.
        "external_api": "http_api",
        "api": "http_api",
        "rest_api": "http_api",

        "python_helper": "python_compute",
        "local_helper": "python_compute",
        "data_transform": "python_compute",

        "file_converter": "file_io",
        "file_generator": "file_io",

        "shell_command": "local_command",
        "command": "local_command",
    }

    text = aliases.get(text, text)

    if text in WRAPPER_REGISTRY:
        return text

    return "custom_adapter"

def _schema_properties(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {}
    properties = schema.get("properties")
    return properties if isinstance(properties, dict) else {}


def _schema_required(schema: Any) -> list[str]:
    if not isinstance(schema, dict):
        return []
    required = schema.get("required")
    if not isinstance(required, list):
        return []
    return [str(item) for item in required if isinstance(item, str)]


def _property_scope(name: str, schema: Any) -> str:
    """Scope resolution without provider or natural-language keyword guessing.

    Priority:
    1. explicit x-scope / x_scope
    2. explicit x-config-kind / x_config_kind
    3. exact platform protocol field name
    4. default runtime_input
    """
    prop = schema if isinstance(schema, dict) else {}

    raw_scope = (
        prop.get("x-scope")
        or prop.get("x_scope")
        or prop.get("x-config-kind")
        or prop.get("x_config_kind")
        or ""
    )
    scope = str(raw_scope).strip().lower()

    if scope in _CONNECTION_SCOPES:
        return scope

    if scope in _RUNTIME_SCOPES:
        return "runtime_input"

    # 这里只允许平台协议字段 exact match，不做 token/key/auth/password/url 这类 contains 猜测。
    normalized_name = str(name or "").strip()
    if normalized_name in _PROTOCOL_CONFIG_FIELDS:
        if normalized_name in {"secret_env", "secret_env_name"}:
            return "secret_ref"
        if normalized_name in {"auth_type", "authentication", "auth_placement", "auth_header_name", "auth_query_param"}:
            return "auth_config"
        return "connection_config"

    return "runtime_input"


def _split_config_and_runtime_schema(schema: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split model-provided schema into connection config and runtime input.

    Conservative rule:
    - explicit x-scope wins;
    - exact platform protocol fields are config;
    - everything else is runtime input.
    """
    if not isinstance(schema, dict):
        return {}, {}

    props = _schema_properties(schema)
    if not props:
        return {}, {}

    required = set(_schema_required(schema))

    config_props: dict[str, Any] = {}
    runtime_props: dict[str, Any] = {}
    config_required: set[str] = set()
    runtime_required: set[str] = set()

    for name, prop in props.items():
        scope = _property_scope(str(name), prop)

        if scope in _CONNECTION_SCOPES:
            config_props[name] = prop
            if name in required:
                config_required.add(name)
        else:
            runtime_props[name] = prop
            if name in required:
                runtime_required.add(name)

    config_schema: dict[str, Any] = {}
    if config_props:
        config_schema = {
            **schema,
            "type": "object",
            "properties": config_props,
            "required": sorted(config_required),
        }

    runtime_schema: dict[str, Any] = {}
    if runtime_props:
        runtime_schema = {
            "type": "object",
            "properties": runtime_props,
            "required": sorted(runtime_required),
        }

    return config_schema, runtime_schema


def _merge_input_schema(manifest: dict[str, Any], runtime_schema: dict[str, Any]) -> dict[str, Any]:
    manifest = dict(manifest or {})

    if not runtime_schema:
        return manifest

    input_schema = manifest.get("input_schema")
    if not isinstance(input_schema, dict):
        legacy_inputs = manifest.get("inputs")
        if isinstance(legacy_inputs, dict):
            input_schema = {
                "type": "object",
                "properties": legacy_inputs,
                "required": [],
            }
        else:
            input_schema = {
                "type": "object",
                "properties": {},
                "required": [],
            }

    input_props = dict(_schema_properties(input_schema))
    input_required = set(_schema_required(input_schema))

    for name, prop in _schema_properties(runtime_schema).items():
        input_props.setdefault(name, prop)

    for name in _schema_required(runtime_schema):
        if name in input_props:
            input_required.add(name)

    manifest["input_schema"] = {
        **input_schema,
        "type": "object",
        "properties": input_props,
        "required": sorted(input_required),
    }
    manifest["inputs"] = input_props

    return manifest


def _normalize_model_config_schema_into_manifest(
    manifest: dict[str, Any],
    model_config_schema: Any,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Move runtime fields accidentally emitted in config_form_schema into manifest.input_schema."""
    config_schema, runtime_schema = _split_config_and_runtime_schema(model_config_schema)
    manifest = _merge_input_schema(manifest, runtime_schema)
    return manifest, config_schema, _schema_required(config_schema)


def _required_capabilities_from_contract(manifest: dict[str, Any], plan: dict[str, Any] | None = None) -> list[str]:
    plan = plan or {}
    values: list[Any] = []

    for source in (plan, manifest):
        raw = source.get("required_capabilities")
        if isinstance(raw, list):
            values.extend(raw)

    result = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)

    return result


def _infer_helper_name_from_capabilities(capabilities: list[str]) -> str:
    """Choose a helper only from the selected capability's helper_imports."""
    all_caps: dict[str, ToolCapability] = {
        **BUILTIN_TOOL_CAPABILITIES,
        **_REGISTERED_TOOL_CAPABILITIES,
    }

    for capability in capabilities:
        cap = all_caps.get(str(capability or "").strip())
        if cap and cap.helper_imports:
            return cap.helper_imports[0]

    return ""

def _build_tool_contract(
    *,
    request: dict[str, Any],
    plan: dict[str, Any],
    manifest: dict[str, Any],
    sample_input: dict[str, Any],
) -> dict[str, Any]:
    request = dict(request or {})
    plan = dict(plan or {})
    manifest = dict(manifest or {})

    wrapper_family = _canonical_wrapper_family(
        plan.get("wrapper_family")
        or manifest.get("wrapper_family")
        or request.get("wrapper_family")
        or _infer_wrapper_family({**request, **plan, "manifest": manifest})
    )

    capabilities = _required_capabilities_from_contract(manifest, plan)

    if not capabilities:
        if wrapper_family == "http_api":
            capabilities = ["http_request"]
        elif wrapper_family == "managed_helper":
            # 不默认 network_read/web_search。managed_helper 必须由 planner/repair 明确选择能力。
            capabilities = []
        elif wrapper_family == "database_query":
            capabilities = ["database_read"]
        elif wrapper_family == "file_io":
            capabilities = ["file_output"]
        elif wrapper_family == "python_compute":
            capabilities = ["deterministic_execution"]
        elif wrapper_family == "local_command":
            capabilities = ["local_command"]

    normalized_capabilities: list[str] = []
    for item in capabilities:
        text = str(item or "").strip()
        if text and text not in normalized_capabilities:
            normalized_capabilities.append(text)

    capabilities = normalized_capabilities

    helper_contract: dict[str, Any] = {}

    manifest_optional = manifest.get("optional") if isinstance(manifest.get("optional"), dict) else {}
    optional_helper_contract = (
        manifest_optional.get("helper_contract")
        if isinstance(manifest_optional.get("helper_contract"), dict)
        else {}
    )

    if optional_helper_contract:
        helper_contract.update(optional_helper_contract)

    if isinstance(manifest.get("helper_contract"), dict):
        helper_contract.update(manifest["helper_contract"])

    if isinstance(plan.get("helper_contract"), dict):
        helper_contract.update(plan["helper_contract"])

    helper_name = str(
        plan.get("helper_name")
        or helper_contract.get("helper_name")
        or _infer_helper_name_from_capabilities(capabilities)
        or ""
    ).strip()

    if helper_name:
        helper_contract["helper_name"] = helper_name

    function_name = _slug(
        str(
            manifest.get("name")
            or request.get("tool_name")
            or plan.get("operation")
            or "custom_tool"
        )
    )

    manifest["wrapper_family"] = wrapper_family
    manifest["tool_kind"] = (
        plan.get("tool_kind")
        or manifest.get("tool_kind")
        or _infer_tool_kind({**request, **plan, "manifest": manifest})
    )
    manifest["required_capabilities"] = capabilities

    if helper_contract:
        manifest["helper_contract"] = helper_contract

    return {
        "contract_version": "1.0",
        "wrapper_family": wrapper_family,
        "tool_kind": manifest.get("tool_kind") or "",
        "function_name": function_name,
        "required_capabilities": capabilities,
        "helper_name": helper_name,
        "helper_contract": helper_contract,
        "manifest": manifest,
        "sample_input": sample_input,
        "auth_decision": plan.get("auth_decision") or manifest.get("auth_decision") or {},
        "auth_gate": plan.get("auth_gate") or manifest.get("auth_gate") or {},
    }


def _capability_allowed_wrappers(capability_name: str) -> list[str]:
    wrapper = CAPABILITY_TO_WRAPPER.get(str(capability_name or "").strip())
    return [wrapper] if wrapper else []


def _capability_allowed_helpers(capability_name: str) -> list[str]:
    cap = BUILTIN_TOOL_CAPABILITIES.get(capability_name) or _REGISTERED_TOOL_CAPABILITIES.get(capability_name)
    if not cap:
        return []
    return list(cap.helper_imports or [])


def _contract_registry_summary() -> dict[str, Any]:
    return {
        "wrapper_registry": {
            name: {
                "description": meta.get("description", ""),
                "model_scope": meta.get("model_scope", ""),
                "auth_policy": meta.get("auth_policy", ""),
            }
            for name, meta in WRAPPER_REGISTRY.items()
        },
        "capability_registry": {
            name: {
                "display_name": cap.display_name,
                "category": cap.category,
                "allowed_wrapper": CAPABILITY_TO_WRAPPER.get(name, ""),
                "helper_imports": cap.helper_imports,
                "usage_policy": cap.usage_policy,
                "required_env": cap.required_env,
                "required_secrets": cap.required_secrets,
                "prompt_guidance": cap.prompt_guidance,
            }
            for name, cap in {
                **BUILTIN_TOOL_CAPABILITIES,
                **_REGISTERED_TOOL_CAPABILITIES,
            }.items()
            if cap.allow_creator_use
        },
    }


def validate_tool_contract_registry_consistency(contract: dict[str, Any]) -> dict[str, Any]:
    """Deterministic registry-level contract validation.

    This function does not infer business semantics.
    It only checks wrapper/capability/helper consistency.
    """
    contract = dict(contract or {})
    manifest = contract.get("manifest") if isinstance(contract.get("manifest"), dict) else {}

    wrapper_family = _canonical_wrapper_family(
        contract.get("wrapper_family")
        or manifest.get("wrapper_family")
        or ""
    )

    capabilities = contract.get("required_capabilities")
    if not isinstance(capabilities, list):
        capabilities = manifest.get("required_capabilities") if isinstance(manifest.get("required_capabilities"), list) else []

    capabilities = [
        str(item).strip()
        for item in capabilities
        if str(item).strip()
    ]

    helper_name = str(
        contract.get("helper_name")
        or (
            (contract.get("helper_contract") or {}).get("helper_name")
            if isinstance(contract.get("helper_contract"), dict)
            else ""
        )
        or (
            (manifest.get("helper_contract") or {}).get("helper_name")
            if isinstance(manifest.get("helper_contract"), dict)
            else ""
        )
        or ""
    ).strip()

    errors: list[str] = []
    warnings: list[str] = []

    if wrapper_family not in WRAPPER_REGISTRY:
        errors.append(f"unknown wrapper_family: {wrapper_family}")

    if not capabilities:
        errors.append("required_capabilities is empty; planner must choose one or more capabilities from capability_registry")

    for capability in capabilities:
        cap = BUILTIN_TOOL_CAPABILITIES.get(capability) or _REGISTERED_TOOL_CAPABILITIES.get(capability)
        if not cap:
            errors.append(f"unknown capability: {capability}")
            continue

        allowed_wrappers = _capability_allowed_wrappers(capability)
        if allowed_wrappers and wrapper_family not in allowed_wrappers:
            errors.append(
                f"capability {capability} is incompatible with wrapper_family={wrapper_family}; "
                f"allowed_wrappers={allowed_wrappers}"
            )

    if wrapper_family == "managed_helper":
        allowed_helpers: set[str] = set()
        for capability in capabilities:
            allowed_helpers.update(_capability_allowed_helpers(capability))

        if not helper_name:
            errors.append(
                "managed_helper requires helper_contract.helper_name or helper_name. "
                f"Allowed helpers from selected capabilities: {sorted(allowed_helpers)}"
            )
        elif allowed_helpers and helper_name not in allowed_helpers:
            errors.append(
                f"helper {helper_name} is not allowed by selected capabilities; "
                f"allowed_helpers={sorted(allowed_helpers)}"
            )

    if wrapper_family == "python_compute":
        helper_contract = manifest.get("helper_contract") if isinstance(manifest.get("helper_contract"), dict) else {}
        if helper_name or helper_contract:
            warnings.append("python_compute should not declare managed helper_contract/helper_name")

    if wrapper_family == "http_api" and "http_request" not in capabilities:
        warnings.append("http_api usually declares required_capabilities=[\"http_request\"]")

    return {
        "success": not errors,
        "status": "validated" if not errors else "failed",
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
        "wrapper_family": wrapper_family,
        "required_capabilities": capabilities,
        "helper_name": helper_name,
    }

async def _run_contract_validation_model(
    *,
    request: dict[str, Any],
    plan: dict[str, Any],
    contract: dict[str, Any],
    model_notes: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    deterministic = validate_tool_contract_registry_consistency(contract)

    messages = [
        {
            "role": "system",
            "content": (
                "You are contract_validator_model for a generic Tool Authoring system. "
                "Return strict JSON only. "
                "Your job is semantic consistency validation, not code generation. "
                "Do not hard-code provider-specific rules or example business cases. "
                "Use only the finite wrapper/capability/helper registry provided by the user. "
                "Check whether the draft ToolContract can implement the user's requested capability. "
                "If wrapper/capability/helper are inconsistent or under-specified, return status='repair'. "
                "If multiple implementations are genuinely possible and cannot be chosen from the request, return status='needs_user'. "
                "If valid, return status='pass'. "
                "Never output Python code."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "user_request": {
                        "description": request.get("description"),
                        "operation": request.get("operation"),
                        "tool_name": request.get("tool_name"),
                        "input_description": request.get("input_description"),
                        "output_description": request.get("output_description"),
                        "tool_kind": request.get("tool_kind"),
                        "needs_external_network": request.get("needs_external_network"),
                        "clarification_answers": request.get("clarification_answers") or [],
                    },
                    "draft_plan": plan,
                    "draft_contract": contract,
                    "deterministic_registry_validation": deterministic,
                    "registry": _contract_registry_summary(),
                    "required_output_json_schema": {
                        "status": "pass|repair|needs_user|fail",
                        "summary": "short explanation",
                        "issues": [
                            {
                                "id": "string",
                                "layer": "contract|capability|wrapper|helper|auth|schema",
                                "severity": "error|warning",
                                "message": "string",
                                "evidence": [],
                                "suggested_patch": {
                                    "wrapper_family": "optional",
                                    "required_capabilities": [],
                                    "helper_contract": {},
                                    "manifest_patch": {},
                                },
                                "needs_user_confirmation": False,
                                "question": {},
                            }
                        ],
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]

    result, ack, err = await _complete_author_model(
        "validator",
        messages,
        reason="creator_tool_contract_validation",
    )

    if ack:
        model_notes.append(f"contract_validator_model={ack['model']}")

    if err:
        warnings.append(f"contract validator unavailable, used deterministic contract validation only: {err}")
        if deterministic["success"]:
            return {
                "status": "pass",
                "summary": "deterministic registry validation passed",
                "issues": [],
                "deterministic": deterministic,
            }
        return {
            "status": "repair",
            "summary": "deterministic registry validation failed",
            "issues": [
                {
                    "id": "deterministic_registry_validation_failed",
                    "layer": "contract",
                    "severity": "error",
                    "message": "; ".join(deterministic.get("errors") or []),
                    "evidence": deterministic.get("errors") or [],
                    "suggested_patch": {},
                    "needs_user_confirmation": False,
                }
            ],
            "deterministic": deterministic,
        }

    result = result if isinstance(result, dict) else {}
    status = str(result.get("status") or "").strip().lower()
    if status not in {"pass", "repair", "needs_user", "fail"}:
        status = "pass" if deterministic.get("success") else "repair"

    issues = result.get("issues") if isinstance(result.get("issues"), list) else []

    if deterministic.get("errors"):
        issues = [
            *issues,
            {
                "id": "deterministic_registry_validation_failed",
                "layer": "contract",
                "severity": "error",
                "message": "; ".join(deterministic.get("errors") or []),
                "evidence": deterministic.get("errors") or [],
                "suggested_patch": {},
                "needs_user_confirmation": False,
            },
        ]
        if status == "pass":
            status = "repair"

    return {
        "status": status,
        "summary": str(result.get("summary") or ""),
        "issues": issues,
        "deterministic": deterministic,
    }

async def _repair_contract_with_model(
    *,
    request: dict[str, Any],
    plan: dict[str, Any],
    validation: dict[str, Any],
    model_notes: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    messages = [
        {
            "role": "system",
            "content": (
                "You are contract_repair_model for Tool Authoring. "
                "Return strict JSON only. "
                "Repair only ToolContract fields: wrapper_family, required_capabilities, helper_contract, "
                "manifest, sample_input, config_form_schema, auth_decision, code_policy, dependencies, artifact_policy. "
                "Do not generate adapter code. "
                "Do not invent provider-specific registry entries. "
                "Choose wrapper_family only from wrapper_registry. "
                "Choose required_capabilities only from capability_registry. "
                "For managed_helper, choose helper_contract.helper_name only from helper_imports of selected capabilities. "
                "For tools that need third-party Python libraries, declare them in manifest.dependencies and/or "
                "manifest.code_policy.allowed_imports instead of changing backend code. "
                "Prefer this output shape: "
                "{\"plan_patch\": {...}, \"manifest_patch\": {...}, \"clarification_questions\": [], \"notes\": []}. "
                "If you return top-level patch fields, the system will still normalize them. "
                "If validation says needs_user, preserve that and create clarification_questions instead of guessing."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "user_request": {
                        "description": request.get("description"),
                        "operation": request.get("operation"),
                        "tool_name": request.get("tool_name"),
                        "input_description": request.get("input_description"),
                        "output_description": request.get("output_description"),
                        "tool_kind": request.get("tool_kind"),
                        "needs_external_network": request.get("needs_external_network"),
                        "clarification_answers": request.get("clarification_answers") or [],
                    },
                    "current_plan": plan,
                    "validation": validation,
                    "registry": _contract_registry_summary(),
                    "required_output": {
                        "plan_patch": {},
                        "manifest_patch": {},
                        "clarification_questions": [],
                        "notes": [],
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]

    result, ack, err = await _complete_author_model(
        "planner",
        messages,
        reason="creator_tool_contract_repair",
    )

    if ack:
        model_notes.append(f"contract_repair_model={ack['model']}")

    if err:
        warnings.append(f"contract repair model unavailable: {err}")
        result = {}

    result = result if isinstance(result, dict) else {}

    repaired = dict(plan or {})
    manifest = dict(repaired.get("manifest") or {})

    allowed_plan_keys = {
        "wrapper_family",
        "tool_kind",
        "required_capabilities",
        "helper_contract",
        "helper_name",
        "auth_decision",
        "config_form_schema",
        "sample_input",
        "requires_config",
        "requires_external_network",
        "requires_live_test",
        "ready_for_code_generation",
        "implementation_plan",

        # 新增：允许规划层携带代码策略/依赖，但最终也会同步进 manifest。
        "code_policy",
        "dependencies",
        "artifact_policy",
        "required_files",
    }

    allowed_manifest_keys = {
        "wrapper_family",
        "tool_kind",
        "required_capabilities",
        "helper_contract",
        "auth_decision",
        "auth_gate",
        "auth_override",
        "input_schema",
        "output_schema",
        "inputs",
        "outputs",
        "tool_type",
        "needs_external_network",
        "required_env",
        "required_secrets",
        "security_schemes",
        "optional",

        # 新增：让 planner/repair 能声明第三方库、导入策略、文件/产物策略。
        "code_policy",
        "dependencies",
        "allowed_imports",
        "required_files",
        "artifact_policy",
        "output_artifacts",
        "runtime_constraints",
        "sandbox_policy",
    }

    def deep_merge_dict(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        merged = dict(base or {})
        for key, value in (patch or {}).items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = deep_merge_dict(merged[key], value)
            else:
                merged[key] = value
        return merged

    def normalize_capabilities(values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        result_caps: list[str] = []
        for item in values:
            text = str(item or "").strip()
            if text and text not in result_caps:
                result_caps.append(text)
        return result_caps

    def normalize_string_list(values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        result_values: list[str] = []
        for item in values:
            text = str(item or "").strip()
            if text and text not in result_values:
                result_values.append(text)
        return result_values

    plan_patch: dict[str, Any] = {}
    manifest_patch: dict[str, Any] = {}

    if isinstance(result.get("plan_patch"), dict):
        plan_patch.update(result["plan_patch"])

    if isinstance(result.get("manifest_patch"), dict):
        manifest_patch.update(result["manifest_patch"])

    # 兼容模型直接返回顶层 patch。
    for key in allowed_plan_keys:
        if key in result:
            plan_patch[key] = result[key]

    if isinstance(result.get("manifest"), dict):
        manifest_patch = deep_merge_dict(manifest_patch, result["manifest"])

    # 如果 repair model 没返回 patch，则退回 validator suggested_patch。
    if not plan_patch and not manifest_patch:
        for issue in validation.get("issues") or []:
            if not isinstance(issue, dict):
                continue

            suggested = issue.get("suggested_patch")
            if not isinstance(suggested, dict):
                continue

            for key in allowed_plan_keys:
                if key in suggested:
                    plan_patch[key] = suggested[key]

            if isinstance(suggested.get("manifest_patch"), dict):
                manifest_patch = deep_merge_dict(manifest_patch, suggested["manifest_patch"])

            for key in allowed_manifest_keys:
                if key in suggested:
                    manifest_patch[key] = suggested[key]

    for key, value in plan_patch.items():
        if key in allowed_plan_keys:
            repaired[key] = value

    for key, value in manifest_patch.items():
        if key in allowed_manifest_keys:
            if isinstance(value, dict) and isinstance(manifest.get(key), dict):
                manifest[key] = deep_merge_dict(manifest[key], value)
            else:
                manifest[key] = value

    # 兼容 plan 级 code_policy/dependencies，同步到 manifest。
    if isinstance(repaired.get("code_policy"), dict):
        if isinstance(manifest.get("code_policy"), dict):
            manifest["code_policy"] = deep_merge_dict(manifest["code_policy"], repaired["code_policy"])
        else:
            manifest["code_policy"] = repaired["code_policy"]

    deps = normalize_string_list(repaired.get("dependencies"))
    manifest_deps = normalize_string_list(manifest.get("dependencies"))
    merged_deps = []
    for item in [*manifest_deps, *deps]:
        if item and item not in merged_deps:
            merged_deps.append(item)
    if merged_deps:
        repaired["dependencies"] = merged_deps
        manifest["dependencies"] = merged_deps

    if isinstance(repaired.get("artifact_policy"), dict):
        if isinstance(manifest.get("artifact_policy"), dict):
            manifest["artifact_policy"] = deep_merge_dict(manifest["artifact_policy"], repaired["artifact_policy"])
        else:
            manifest["artifact_policy"] = repaired["artifact_policy"]

    # 兼容 manifest.allowed_imports，把它归入 manifest.code_policy.allowed_imports。
    allowed_imports = normalize_string_list(manifest.get("allowed_imports"))
    if allowed_imports:
        code_policy = manifest.get("code_policy") if isinstance(manifest.get("code_policy"), dict) else {}
        existing = normalize_string_list(code_policy.get("allowed_imports"))
        merged_imports: list[str] = []
        for item in [*existing, *allowed_imports]:
            if item and item not in merged_imports:
                merged_imports.append(item)
        code_policy["allowed_imports"] = merged_imports
        manifest["code_policy"] = code_policy

    # 兼容 manifest.optional.helper_contract，把它提升成正式 helper_contract。
    optional = manifest.get("optional") if isinstance(manifest.get("optional"), dict) else {}
    optional_helper_contract = (
        optional.get("helper_contract")
        if isinstance(optional.get("helper_contract"), dict)
        else {}
    )

    helper_contract: dict[str, Any] = {}
    if optional_helper_contract:
        helper_contract.update(optional_helper_contract)

    if isinstance(manifest.get("helper_contract"), dict):
        helper_contract.update(manifest["helper_contract"])

    if isinstance(repaired.get("helper_contract"), dict):
        helper_contract.update(repaired["helper_contract"])

    helper_name = str(repaired.get("helper_name") or helper_contract.get("helper_name") or "").strip()
    if helper_name:
        helper_contract["helper_name"] = helper_name

    if repaired.get("wrapper_family"):
        manifest["wrapper_family"] = repaired["wrapper_family"]

    if repaired.get("tool_kind"):
        manifest["tool_kind"] = repaired["tool_kind"]

    if repaired.get("auth_decision"):
        manifest["auth_decision"] = repaired["auth_decision"]

    caps = normalize_capabilities(repaired.get("required_capabilities"))
    if not caps:
        caps = normalize_capabilities(manifest.get("required_capabilities"))

    if caps:
        repaired["required_capabilities"] = caps
        manifest["required_capabilities"] = caps

    if helper_contract:
        repaired["helper_contract"] = helper_contract
        manifest["helper_contract"] = helper_contract

    repaired["manifest"] = manifest

    questions = result.get("clarification_questions")
    if isinstance(questions, list) and questions:
        repaired["clarification_questions"] = questions
        repaired["questions"] = questions
        repaired["needs_clarification"] = True
        repaired["ready_for_code_generation"] = False

    notes = result.get("notes")
    if isinstance(notes, list):
        repaired["model_notes"] = [
            *repaired.get("model_notes", []),
            *[str(item) for item in notes if item],
        ]

    return repaired

async def _validate_and_repair_contract_loop(
    *,
    request: dict[str, Any],
    plan: dict[str, Any],
    model_notes: list[str],
    warnings: list[str],
    max_rounds: int = 2,
) -> dict[str, Any]:
    current = dict(plan or {})
    contract_validation_log: list[dict[str, Any]] = []

    for round_idx in range(max_rounds + 1):
        manifest = current.get("manifest") if isinstance(current.get("manifest"), dict) else {}
        sample_input = current.get("sample_input") if isinstance(current.get("sample_input"), dict) else {}

        contract = _build_tool_contract(
            request=request,
            plan=current,
            manifest=manifest,
            sample_input=sample_input,
        )

        validation = await _run_contract_validation_model(
            request=request,
            plan=current,
            contract=contract,
            model_notes=model_notes,
            warnings=warnings,
        )

        contract_validation_log.append(
            {
                "round": round_idx + 1,
                "status": validation.get("status"),
                "summary": validation.get("summary"),
                "issues": validation.get("issues") or [],
                "deterministic": validation.get("deterministic") or {},
            }
        )

        status = str(validation.get("status") or "").lower()

        if status == "pass":
            current["contract_validation"] = validation
            current["contract_validation_log"] = contract_validation_log
            current["adapter_contract"] = contract
            return current

        if status == "needs_user":
            questions: list[dict[str, Any]] = []
            for issue in validation.get("issues") or []:
                if not isinstance(issue, dict):
                    continue
                question = issue.get("question")
                if isinstance(question, dict):
                    normalized = _normalize_clarification_question(question)
                    if normalized:
                        questions.append(normalized)

            if not questions:
                questions.append(
                    {
                        "id": "implementation_choice",
                        "type": "short_text",
                        "question": "这个工具有多种合理实现方式，请补充希望使用哪类平台能力或外部系统。",
                        "required": True,
                    }
                )

            current["needs_clarification"] = True
            current["questions"] = questions[:3]
            current["clarification_questions"] = questions[:3]
            current["ready_for_code_generation"] = False
            current["contract_validation"] = validation
            current["contract_validation_log"] = contract_validation_log
            return current

        if round_idx >= max_rounds:
            current["ready_for_code_generation"] = False
            current["requires_authoring_tools"] = False
            current["contract_validation"] = validation
            current["contract_validation_log"] = contract_validation_log
            current["validation"] = {
                "success": False,
                "status": "contract_validation_failed",
                "errors": [
                    str(issue.get("message") or issue)
                    for issue in validation.get("issues") or []
                ],
                "warnings": [],
            }
            return current

        current = await _repair_contract_with_model(
            request=request,
            plan=current,
            validation=validation,
            model_notes=model_notes,
            warnings=warnings,
        )

        current = _normalize_author_plan(current, request)

    return current

def _external_api_missing_fields(
    config: dict[str, Any],
    sample_input: dict[str, Any],
    request: dict[str, Any],
    *,
    auth_decision: dict[str, Any] | None = None,
) -> list[str]:
    """Return only connection/config panel requirements.

    Runtime input fields must not appear here.
    Auth fields are required only when auth_decision.required=yes.
    """
    config = config if isinstance(config, dict) else {}
    request = request if isinstance(request, dict) else {}

    missing: list[str] = []

    if not _has_config_value(config, "url", "endpoint", "base_url"):
        missing.append("base_url")

    decision = _normalize_auth_decision(
        auth_decision or request.get("auth_decision") or {},
        default_required="no",
    )

    auth_required = decision.get("required") == "yes"

    auth_type = _normalize_auth_type(
        config.get("auth_type")
        or config.get("authentication")
        or ((config.get("auth") or {}).get("type") if isinstance(config.get("auth"), dict) else "")
    )

    has_secret_ref = bool(
        _extract_env_refs(config)
        or _has_config_value(
            config,
            "secret_env",
            "secret_env_name",
            "api_key_env",
            "token_env",
            "password_env",
        )
    )

    if auth_required:
        if auth_type in {"", "none", "unknown"}:
            missing.append("auth_type")
        elif not has_secret_ref:
            missing.append("secret_env")

    return missing


def _slug_env_prefix(value: str) -> str:
    prefix = re.sub(r"[^A-Za-z0-9]+", "_", (value or "TOOL")).strip("_").upper()
    return prefix or "TOOL"


def _suggest_external_api_entrypoint(request: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Build a best-effort prefill for the authorization modal without asking the user."""
    text = " ".join(str(request.get(key) or "") for key in ("description", "operation", "tool_name"))
    url_match = re.search(r"https?://[^\s，。；,;]+", text)
    base_url = str(config.get("base_url") or config.get("endpoint") or config.get("url") or (url_match.group(0) if url_match else "")).strip()
    method = str(config.get("method") or "").strip().upper()
    lowered = text.lower()
    if not method:
        method = "POST" if any(token in lowered for token in ("创建", "新增", "提交", "发送", "发布", "上传", "create", "post", "send", "submit")) else "GET"
    auth_type = str(config.get("auth_type") or config.get("authentication") or "").strip().lower()
    has_secret_hint = bool(request.get("needs_secret") or any(token in lowered for token in ("key", "token", "密钥", "令牌", "认证", "鉴权", "bearer")))
    if not auth_type:
        auth_type = "token" if "token" in lowered or "令牌" in lowered or "bearer" in lowered else "api_key" if has_secret_hint else "none"
    env_prefix = _slug_env_prefix(str(request.get("tool_name") or request.get("operation") or request.get("description") or "TOOL"))
    existing_secret = str(config.get("secret_env") or config.get("secret_env_name") or config.get("api_key_env") or config.get("token_env") or "").strip()
    secret_env = existing_secret or (f"{env_prefix}_{'TOKEN' if auth_type == 'token' else 'API_KEY'}" if auth_type not in {"none", "no_auth", "anonymous"} else "")
    confidence = "high" if base_url and (config.get("auth_type") or config.get("authentication") or existing_secret) else "medium" if base_url else "low"
    auth_placement = str(config.get("auth_placement") or ((config.get("auth") or {}).get("placement") if isinstance(config.get("auth"), dict) else "") or ("header" if auth_type == "api_key" else "")).strip()
    auth_header_name = str(config.get("auth_header_name") or ((config.get("auth") or {}).get("header_name") if isinstance(config.get("auth"), dict) else "") or ("X-API-KEY" if auth_type == "api_key" else "Authorization" if auth_type == "token" else "")).strip()
    auth_query_param = str(config.get("auth_query_param") or ((config.get("auth") or {}).get("query_param") if isinstance(config.get("auth"), dict) else "") or "api_key").strip()
    return {"base_url": base_url, "method": method, "auth_type": auth_type, "secret_env": secret_env, "auth_placement": auth_placement, "auth_header_name": auth_header_name, "auth_query_param": auth_query_param, "confidence": confidence}


def _clarification_answer_texts(request_or_answers: Any) -> list[str]:
    answers = request_or_answers.get("clarification_answers") if isinstance(request_or_answers, dict) else request_or_answers
    values: list[str] = []
    for item in answers or []:
        if isinstance(item, dict):
            answer = str(item.get("answer_label") or item.get("answer") or "").strip()
        else:
            answer = str(item or "").strip()
        if answer:
            values.append(answer)
    return values


def _apply_clarification_answers(request: dict[str, Any]) -> dict[str, Any]:
    updated = dict(request or {})
    answers = [item for item in (updated.get("clarification_answers") or []) if isinstance(item, dict) and str(item.get("answer_label") or item.get("answer") or "").strip()]
    capability_answers = _clarification_answer_texts(answers)
    if capability_answers:
        updated["operation"] = capability_answers[-1]
    if answers:
        updated["resolved_clarifications"] = answers
    return updated


def _question_text(question: Any) -> str:
    if isinstance(question, dict):
        return str(question.get("question") or question.get("text") or "").strip()
    return str(question or "").strip()


def _normalize_clarification_question(question: Any) -> dict[str, Any] | None:
    text = _question_text(question)
    if not text:
        return None
    if isinstance(question, dict):
        normalized = {
            "id": str(question.get("id") or _slug(text) or "capability_detail"),
            "type": str(question.get("type") or ("single_choice" if question.get("options") else "short_text")),
            "question": text,
            "required": bool(question.get("required", True)),
        }
        options: list[dict[str, str]] = []
        for idx, option in enumerate(question.get("options") or []):
            if isinstance(option, dict):
                label = str(option.get("label") or option.get("text") or option.get("value") or "").strip()
                value = str(option.get("value") or label or idx).strip()
            else:
                label = str(option or "").strip()
                value = label
            if label:
                options.append({"label": label, "value": value})
        if options:
            normalized["options"] = options
        elif normalized["type"] in {"single_choice", "multi_choice"}:
            normalized["type"] = "short_text"
        return normalized
    return {"id": _slug(text) or "capability_detail", "type": "short_text", "question": text, "required": True}


def _filter_answered_clarification_questions(questions: list[Any], answers: list[Any]) -> list[dict[str, Any]]:
    answered_questions = {
        str(item.get("question") or "").strip()
        for item in answers or []
        if isinstance(item, dict) and str(item.get("answer_label") or item.get("answer") or "").strip()
    }
    answered_ids = {
        str(item.get("id") or item.get("question_id") or "").strip()
        for item in answers or []
        if isinstance(item, dict) and str(item.get("answer_label") or item.get("answer") or "").strip()
    }
    filtered: list[dict[str, Any]] = []
    for question in questions or []:
        normalized = _normalize_clarification_question(question)
        if not normalized:
            continue
        if normalized.get("id") in answered_ids or normalized.get("question") in answered_questions:
            continue
        filtered.append(normalized)
    return filtered


def _needs_capability_clarification(request: dict[str, Any]) -> bool:
    if _clarification_answer_texts(request):
        return False
    text = " ".join(str(request.get(key) or "") for key in ("description", "operation", "input_description", "output_description")).strip().lower()
    if not text:
        return True
    vague_phrases = ("调用接口", "连接接口", "外部 api", "某个api", "某个 api", "api tool", "http tool", "小工具")
    has_vague_api_goal = any(phrase in text for phrase in vague_phrases)
    action_tokens = ("查询", "创建", "更新", "删除", "同步", "发送", "发布", "下载", "上传", "检索", "搜索", "分析", "转换", "生成", "通知", "query", "create", "update", "delete", "sync", "send", "search", "fetch")
    object_tokens = ("数据", "订单", "用户", "消息", "文件", "报告", "记录", "天气", "价格", "库存", "邮件", "短信", "result", "record", "message", "file")
    has_action = any(token in text for token in action_tokens)
    has_object = any(token in text for token in object_tokens)
    return has_vague_api_goal and not (has_action and has_object)


def _external_api_clarification_questions(request: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    """Ask only about real capability ambiguity; connection/key fields belong to config_form_schema."""
    questions: list[dict[str, Any]] = []
    if _needs_capability_clarification(request):
        questions.append({"id": "operation_detail", "type": "short_text", "question": "请用一句话补充这个工具要完成的具体能力。", "required": True})
    return questions[:3]


def _infer_tool_kind(request: dict[str, Any]) -> str:
    """Infer only a human/business-facing semantic label.

    tool_kind must not drive backend dispatch. It is for UI, logs, and semantic
    validation prompts only.
    """
    request = request if isinstance(request, dict) else {}

    explicit = str(request.get("tool_kind") or "").strip()
    if explicit:
        return explicit

    manifest = request.get("manifest") if isinstance(request.get("manifest"), dict) else {}
    manifest_kind = str(manifest.get("tool_kind") or "").strip()
    if manifest_kind:
        return manifest_kind

    operation = str(
        request.get("operation")
        or request.get("description")
        or request.get("tool_name")
        or ""
    ).strip()

    if operation:
        return _slug(operation, fallback="custom_tool")

    wrapper_family = _infer_wrapper_family(request)

    if wrapper_family == "http_api":
        return "external_api"
    if wrapper_family == "python_compute":
        return "local_helper"
    if wrapper_family == "file_io":
        return "file_tool"
    if wrapper_family == "database_query":
        return "database_tool"
    if wrapper_family == "managed_helper":
        return "managed_helper_tool"

    return wrapper_family or "unknown"

def _generic_external_api_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "results": {
                "type": "array",
                "items": {"type": "object"},
            },
            "total": {"type": "integer"},
            "error": {"type": "string"},
            "status_code": {"type": "integer"},
            "response_preview": {"type": "string"},
            "raw": {},
            "trial_run": {"type": "boolean"},
        },
        "required": ["success", "results", "total"],
    }


def _normalize_auth_required(value: Any, default: str = "unknown") -> str:
    text = str(value or "").strip().lower()
    if text in {"yes", "true", "required", "requires_auth", "auth_required", "need", "needed"}:
        return "yes"
    if text in {"no", "false", "none", "noauth", "no_auth", "anonymous", "public"}:
        return "no"
    if text in {"unknown", "uncertain", "maybe", "unsure"}:
        return "unknown"
    return default


def _normalize_auth_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"", "none", "noauth", "no_auth", "anonymous", "public"}:
        return "none"
    if text in {"api_key", "apikey", "key"}:
        return "api_key"
    if text in {"bearer", "token", "oauth_bearer"}:
        return "token"
    if text == "basic":
        return "basic"
    if text in {"oauth", "oauth2"}:
        return "oauth2"
    if text == "custom":
        return "custom"
    return "unknown"


def _normalize_security_schemes(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    schemes: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        scheme = dict(item)
        scheme["type"] = _normalize_auth_type(scheme.get("type") or scheme.get("scheme"))
        schemes.append(scheme)
    return schemes


def _normalize_auth_decision(decision: Any, *, default_required: str = "unknown") -> dict[str, Any]:
    if not isinstance(decision, dict):
        decision = {}

    required = _normalize_auth_required(decision.get("required"), default_required)
    try:
        confidence = float(decision.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0

    confidence = max(0.0, min(confidence, 1.0))

    evidence = decision.get("evidence") or decision.get("reasons") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    if not isinstance(evidence, list):
        evidence = []

    schemes = _normalize_security_schemes(
        decision.get("security_schemes") or decision.get("securitySchemes") or []
    )

    return {
        "required": required,
        "confidence": confidence,
        "reason": str(decision.get("reason") or ""),
        "evidence": [str(item) for item in evidence if item],
        "security_schemes": schemes,
    }


def _config_has_auth_material(config: dict[str, Any]) -> bool:
    if not isinstance(config, dict):
        return False

    auth_type = _normalize_auth_type(
        config.get("auth_type") or
        config.get("authentication") or
        ((config.get("auth") or {}).get("type") if isinstance(config.get("auth"), dict) else "")
    )

    if auth_type not in {"", "none"}:
        return True

    if _extract_env_refs(config):
        return True

    for key in (
        "secret_env",
        "secret_env_name",
        "api_key_env",
        "token_env",
        "password_env",
        "secret_value",
        "api_key",
        "token",
        "password",
    ):
        if config.get(key) not in (None, "", {}, []):
            return True

    if isinstance(config.get("auth"), dict) and config["auth"]:
        return True

    return False


def _derive_auth_decision(
    request: dict[str, Any],
    *,
    wrapper_family: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Derive auth only from explicit auth/security contract.

    wrapper_family/tool_kind/network usage must not decide auth.
    """
    request = request if isinstance(request, dict) else {}
    config = config if isinstance(config, dict) else {}

    auth_override = _auth_override_from_request(request)
    override_decision = _auth_override_to_decision(auth_override)
    if override_decision:
        return _normalize_auth_decision(override_decision, default_required="unknown")

    explicit = request.get("auth_decision")
    if isinstance(explicit, dict):
        return _normalize_auth_decision(explicit, default_required="unknown")

    manifest = request.get("manifest") if isinstance(request.get("manifest"), dict) else {}

    manifest_decision = manifest.get("auth_decision")
    if isinstance(manifest_decision, dict):
        return _normalize_auth_decision(manifest_decision, default_required="unknown")

    required_secrets = manifest.get("required_secrets")
    if isinstance(required_secrets, list) and any(str(item).strip() for item in required_secrets):
        return {
            "required": "yes",
            "confidence": 1.0,
            "reason": "manifest declares required_secrets",
            "evidence": ["manifest.required_secrets"],
            "security_schemes": [{"type": "api_key"}],
        }

    auth = manifest.get("auth") if isinstance(manifest.get("auth"), dict) else {}
    if auth:
        auth_type = _normalize_auth_type(auth.get("type") or auth.get("scheme"))
        if auth_type != "none":
            scheme = dict(auth)
            scheme["type"] = auth_type
            return {
                "required": "yes",
                "confidence": 1.0,
                "reason": "manifest declares non-none auth scheme",
                "evidence": ["manifest.auth"],
                "security_schemes": [scheme],
            }

    security_schemes = manifest.get("security_schemes")
    if isinstance(security_schemes, list):
        non_none = []
        for scheme in security_schemes:
            if not isinstance(scheme, dict):
                continue
            scheme_type = _normalize_auth_type(scheme.get("type") or scheme.get("scheme"))
            if scheme_type != "none":
                item = dict(scheme)
                item["type"] = scheme_type
                non_none.append(item)

        if non_none:
            return {
                "required": "yes",
                "confidence": 1.0,
                "reason": "manifest declares non-none security_schemes",
                "evidence": ["manifest.security_schemes"],
                "security_schemes": non_none,
            }

    auth_type = _normalize_auth_type(
        config.get("auth_type")
        or config.get("authentication")
        or ((config.get("auth") or {}).get("type") if isinstance(config.get("auth"), dict) else "")
    )
    secret_env = str(config.get("secret_env") or config.get("secret_env_name") or "").strip()

    if auth_type not in {"", "none", "unknown"}:
        scheme: dict[str, Any] = {"type": auth_type}
        if secret_env:
            scheme["env"] = secret_env
        return {
            "required": "yes",
            "confidence": 1.0,
            "reason": "config declares non-none auth_type",
            "evidence": ["config.auth_type"],
            "security_schemes": [scheme],
        }

    if secret_env:
        return {
            "required": "yes",
            "confidence": 1.0,
            "reason": "config declares secret_env",
            "evidence": ["config.secret_env"],
            "security_schemes": [{"type": "api_key", "env": secret_env}],
        }

    if manifest.get("requires_auth") is True or request.get("requires_authorization") is True or request.get("needs_secret") is True:
        return {
            "required": "unknown",
            "confidence": 0.8,
            "reason": "request/manifest says auth is required but no concrete scheme is declared",
            "evidence": ["requires_authorization/requires_auth/needs_secret"],
            "security_schemes": [],
        }

    if manifest.get("requires_auth") is False:
        return {
            "required": "no",
            "confidence": 1.0,
            "reason": "manifest explicitly says requires_auth=false",
            "evidence": ["manifest.requires_auth=false"],
            "security_schemes": [{"type": "none"}],
        }

    if auth_type == "none":
        return {
            "required": "no",
            "confidence": 1.0,
            "reason": "config explicitly says auth_type=none",
            "evidence": ["config.auth_type=none"],
            "security_schemes": [{"type": "none"}],
        }

    return {
        "required": "no",
        "confidence": 0.75,
        "reason": "no explicit auth contract found",
        "evidence": [],
        "security_schemes": [{"type": "none"}],
    }

def _author_fallback_plan(request: dict[str, Any]) -> dict[str, Any]:
    request = dict(request or {})

    code_block = str(request.get("code_block") or "")
    code_input_schema, code_output_schema, notes = (
        _infer_schema_from_code(code_block)
        if code_block.strip()
        else ({}, {}, [])
    )

    merged = dict(request)
    config = request.get("config") if isinstance(request.get("config"), dict) else {}

    wrapper_family = _canonical_wrapper_family(_infer_wrapper_family(request))
    tool_kind = _infer_tool_kind({**request, "wrapper_family": wrapper_family})

    sample = (
        request.get("sample_input")
        if isinstance(request.get("sample_input"), dict) and request.get("sample_input")
        else {}
    )

    if code_input_schema and not merged.get("input_schema"):
        merged["input_schema"] = code_input_schema
    if code_output_schema and not merged.get("output_schema"):
        merged["output_schema"] = code_output_schema

    auth_override = _auth_override_from_request(request)

    auth_decision = _derive_auth_decision(
        request,
        wrapper_family=wrapper_family,
        config=config,
    )

    override_decision = _auth_override_to_decision(auth_override)
    if override_decision:
        auth_decision = override_decision

    if isinstance(request.get("manifest"), dict) and request.get("manifest"):
        manifest = dict(request["manifest"])
    else:
        manifest_seed = dict(merged)
        manifest_seed["wrapper_family"] = wrapper_family
        manifest_seed["tool_kind"] = tool_kind

        if wrapper_family == "http_api":
            manifest_seed["needs_external_network"] = True
            manifest_seed["tool_type"] = "custom_adapter"
            manifest_seed.setdefault("required_capabilities", ["http_request"])
            manifest_seed["input_schema"] = request.get("input_schema") or {
                "type": "object",
                "properties": {
                    "payload": {
                        "type": "object",
                        "description": "Fields used to render confirmed request templates.",
                    }
                },
                "required": ["payload"],
            }
            manifest_seed["output_schema"] = request.get("output_schema") or _generic_external_api_output_schema()

        elif wrapper_family == "managed_helper":
            manifest_seed["tool_type"] = "custom_adapter"
            # Do not default to network_read/web_search here. The planner/repair loop
            # must explicitly choose required_capabilities and helper_contract.
            manifest_seed.setdefault("required_capabilities", [])
            manifest_seed.setdefault(
                "input_schema",
                request.get("input_schema")
                or {
                    "type": "object",
                    "properties": {
                        "payload": {
                            "type": "object",
                            "description": "Runtime payload passed to the managed helper wrapper.",
                        }
                    },
                    "required": [],
                },
            )

        elif wrapper_family == "python_compute":
            manifest_seed["tool_type"] = "custom_adapter"
            manifest_seed.setdefault("required_capabilities", ["deterministic_execution"])

        elif wrapper_family == "file_io":
            manifest_seed["tool_type"] = "custom_adapter"
            manifest_seed.setdefault("required_capabilities", ["file_output"])

        elif wrapper_family == "database_query":
            manifest_seed["tool_type"] = "custom_adapter"
            manifest_seed.setdefault("required_capabilities", ["database_read"])

        manifest = build_tool_manifest_draft(manifest_seed)

    manifest = dict(manifest or {})
    manifest["wrapper_family"] = wrapper_family
    manifest["tool_kind"] = tool_kind
    manifest["auth_decision"] = auth_decision
    manifest["auth_override"] = auth_override

    model_config_schema = (
        request.get("config_form_schema")
        if isinstance(request.get("config_form_schema"), dict)
        else {}
    )

    manifest, connection_config_schema, connection_required_fields = _normalize_model_config_schema_into_manifest(
        manifest,
        model_config_schema,
    )

    runtime_facts = extract_runtime_facts(code_block, manifest)

    live_test = request.get("live_test_result") if isinstance(request.get("live_test_result"), dict) else None
    if not live_test:
        ctx = request.get("authoring_context") if isinstance(request.get("authoring_context"), dict) else {}
        live_test = ctx.get("live_test_result") if isinstance(ctx.get("live_test_result"), dict) else None

    auth_gate = merge_auth_gate(
        {"auth_decision": auth_decision},
        runtime_facts,
        live_test,
        auth_override=auth_override,
    )

    missing_fields: list[str] = []
    questions: list[Any] = []

    if wrapper_family == "http_api":
        missing_fields = _external_api_missing_fields(
            config,
            sample,
            request,
            auth_decision=auth_decision,
        )
        questions = _external_api_clarification_questions(request, config)

    elif connection_required_fields:
        missing_fields = list(connection_required_fields)

    if tool_kind == "unknown" and not (
        request.get("tool_name") and (request.get("description") or code_block.strip())
    ):
        missing_fields = ["tool_name", "description or code_block"]
        questions = [
            {
                "id": "operation_detail",
                "type": "short_text",
                "question": "请用一句话补充这个工具要完成的具体能力。",
                "required": True,
            }
        ]

    if not sample:
        input_schema = manifest.get("input_schema") if isinstance(manifest.get("input_schema"), dict) else {}
        sample = {
            key: (
                value.get("default")
                if isinstance(value, dict) and "default" in value
                else "demo"
            )
            for key, value in _schema_properties(input_schema).items()
        }

    has_fixed_endpoint = _has_config_value(config, "base_url", "endpoint", "url")
    ready_live = (
        wrapper_family == "http_api"
        and has_fixed_endpoint
        and not _external_api_missing_fields(
            config,
            sample,
            {**request, "allow_external_network": True, "auth_decision": auth_decision},
            auth_decision=auth_decision,
        )
    )

    live_success = _live_test_success_from_request(request)

    authoring_tool_plan: list[dict[str, Any]] = []

    if missing_fields and (wrapper_family == "http_api" or connection_config_schema or auth_gate["status"] == "needs_config"):
        authoring_tool_plan.append(
            {
                "tool_name": "authoring_config_collector",
                "reason": "需要收集连接/认证/secret 引用配置；运行参数不会进入此配置面板",
                "input": {
                    "config_required_fields": missing_fields,
                    "config": config,
                    "sample_input": sample,
                },
            }
        )
    elif (
        wrapper_family == "http_api"
        and ready_live
        and not live_success
        and not request.get("skip_live_test")
    ):
        authoring_tool_plan.append(
            {
                "tool_name": "authoring_live_test",
                "reason": "需要在生成 adapter 前确认固定远程接口配置和 sample input 可用",
                "input": {
                    "config": config,
                    "sample_input": sample,
                },
            }
        )

    if code_block.strip():
        authoring_tool_plan.append(
            {
                "tool_name": "authoring_schema_infer",
                "reason": "从现有代码推断输入输出 schema",
                "input": {"code_block": code_block},
            }
        )

    requires_config = bool(missing_fields) or auth_gate["status"] == "needs_config"
    requires_live_test = bool(
        auth_gate["status"] == "needs_live_test"
        or (
            wrapper_family == "http_api"
            and has_fixed_endpoint
            and not live_success
            and not request.get("skip_live_test")
        )
    )

    ready_code = bool(manifest) and not questions and not authoring_tool_plan and not auth_gate.get("block_code_generation")
    if wrapper_family == "http_api":
        ready_code = ready_code and (
            live_success
            or request.get("skip_live_test") is True
            or not requires_live_test
        )

    result = {
        **_plan_defaults(),
        "needs_clarification": bool(questions),
        "questions": questions[:3],
        "clarification_questions": questions[:3],
        "requires_config": requires_config,
        "config_required_fields": missing_fields,
        "config_form_schema": connection_config_schema,
        "suggested_entrypoint": (
            _suggest_external_api_entrypoint(request, config)
            if wrapper_family == "http_api"
            else {}
        ),
        "additional_fields_schema": [],
        "requires_authorization": auth_decision["required"] in {"yes", "unknown"},
        "tool_kind": tool_kind,
        "operation": str(request.get("operation") or request.get("description") or ""),
        "requires_secret": auth_decision["required"] == "yes",
        "secret_env_suggestions": (
            sorted(_extract_env_refs(config))
            or (
                [str(config.get("secret_env") or config.get("secret_env_name"))]
                if config.get("secret_env") or config.get("secret_env_name")
                else []
            )
        ),
        "requires_external_network": bool(
            request.get("needs_external_network")
            or wrapper_family in {"http_api", "managed_helper"}
            or runtime_facts.get("uses_external_network")
        ),
        "requires_live_test": requires_live_test,
        "ready_for_live_test": ready_live,
        "ready_for_code_generation": ready_code,
        "requires_authoring_tools": bool(authoring_tool_plan),
        "authoring_tool_plan": authoring_tool_plan,
        "missing_fields": [],
        "suggested_config_schema": connection_config_schema,
        "sample_input_schema": manifest.get("input_schema") if isinstance(manifest.get("input_schema"), dict) else {},
        "manifest": manifest,
        "implementation_plan": (
            "Use the finite wrapper/capability/helper registry. Generate only wrapper-specific internal code; "
            "never generate a provider-specific full adapter unless custom_adapter is explicitly selected."
        ),
        "sample_input": sample,
        "risk_notes": notes,
        "model_notes": ["deterministic fallback planner used", *notes],
        "wrapper_family": wrapper_family,
        "auth_decision": auth_decision,
        "auth_gate": auth_gate,
        "auth_override": auth_override,
        "runtime_facts": runtime_facts,
    }

    return _apply_auth_override_to_plan(result, request)

def _safe_clarification_questions(questions: list[Any], fallback: list[Any]) -> list[dict[str, Any]]:
    banned = ("headers", "body", "query", "schema", "expected output", "输出字段", "输入输出", "method", "模板", "sample input", "服务地址", "连接地址", "endpoint", "密钥", "token", "认证", "auth", "外部网络", "连接测试")
    safe: list[dict[str, Any]] = []
    for item in questions or []:
        normalized = _normalize_clarification_question(item)
        if not normalized:
            continue
        text = normalized.get("question", "")
        if any(token.lower() in text.lower() for token in banned):
            continue
        safe.append(normalized)
    if not safe:
        for item in fallback or []:
            normalized = _normalize_clarification_question(item)
            if not normalized:
                continue
            text = normalized.get("question", "")
            if not any(token.lower() in text.lower() for token in banned):
                safe.append(normalized)
    return safe[:3]

def _live_test_success_from_request(request: dict[str, Any]) -> bool:
    live = request.get("live_test_result")
    if isinstance(live, dict) and live.get("success") is True:
        return True
    ctx = request.get("authoring_context") if isinstance(request.get("authoring_context"), dict) else {}
    live = ctx.get("live_test_result")
    return isinstance(live, dict) and live.get("success") is True

def extract_runtime_facts(code: str, manifest: dict) -> dict[str, Any]:
    manifest = manifest if isinstance(manifest, dict) else {}
    manifest_text = json.dumps(manifest, ensure_ascii=False)

    facts: dict[str, Any] = {
        "uses_external_network": False,
        "has_network_import": False,
        "has_remote_literal": bool(re.search(r"https?://", f"{code or ''}\n{manifest_text}")),
        "has_configurable_endpoint": any(
            manifest.get(key) not in (None, "", {}, [])
            for key in ("base_url", "endpoint", "url")
        ),
        "reads_env_secret": False,
        "declared_env": [],
        "declared_secrets": [],
        "auth_config_present": False,
        "auth_decision_present": False,
        "auth_decision_required": "unknown",
        "uses_subprocess": False,
        "writes_file": False,
    }

    declared_env: set[str] = set()
    declared_secrets: set[str] = set()

    for key in ("required_env", "env"):
        value = manifest.get(key)
        if isinstance(value, list):
            declared_env.update(str(item) for item in value if item)

    for key in ("required_secrets", "secrets"):
        value = manifest.get(key)
        if isinstance(value, list):
            declared_secrets.update(str(item) for item in value if item)

    auth = manifest.get("auth") if isinstance(manifest.get("auth"), dict) else {}
    auth_decision_raw = manifest.get("auth_decision") if isinstance(manifest.get("auth_decision"), dict) else {}
    auth_decision = _normalize_auth_decision(auth_decision_raw, default_required="unknown")

    facts["auth_decision_present"] = bool(auth_decision_raw)
    facts["auth_decision_required"] = auth_decision.get("required") or "unknown"

    security_schemes = auth_decision.get("security_schemes") or []
    non_none_schemes = []
    for scheme in security_schemes:
        if not isinstance(scheme, dict):
            continue
        scheme_type = _normalize_auth_type(scheme.get("type"))
        if scheme_type not in {"", "none"}:
            non_none_schemes.append(scheme)

    manifest_auth_requires_secret = bool(
        auth
        or manifest.get("needs_secret") is True
        or manifest.get("requires_secret") is True
        or declared_secrets
        or auth_decision.get("required") == "yes"
        or (
            auth_decision.get("required") == "unknown"
            and bool(non_none_schemes)
        )
    )

    facts["auth_config_present"] = manifest_auth_requires_secret

    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        facts["declared_env"] = sorted(declared_env)
        facts["declared_secrets"] = sorted(declared_secrets)
        facts["auth_config_present"] = bool(
            facts["auth_config_present"]
            or declared_secrets
        )
        return facts

    network_roots = {"requests", "httpx", "aiohttp", "urllib"}
    subprocess_roots = {"subprocess", "os"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = {alias.name.split(".")[0] for alias in node.names}

            if roots & network_roots:
                facts["has_network_import"] = True
                facts["uses_external_network"] = True

            if roots & subprocess_roots:
                facts["uses_subprocess"] = True

        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]

            if root in network_roots:
                facts["has_network_import"] = True
                facts["uses_external_network"] = True

            if root in subprocess_roots:
                facts["uses_subprocess"] = True

        elif isinstance(node, ast.Call):
            func_text = ""
            try:
                func_text = ast.unparse(node.func)
            except Exception:
                pass

            if any(token in func_text for token in ("requests.", "httpx.", "aiohttp.", "urllib.")):
                facts["uses_external_network"] = True

            if any(token in func_text for token in ("os.getenv", "os.environ.get")):
                facts["reads_env_secret"] = True

            if func_text in {"open", "Path", "pathlib.Path"} or func_text.endswith(".write"):
                facts["writes_file"] = True

            if any(token in func_text for token in ("subprocess.", "os.system")):
                facts["uses_subprocess"] = True

        elif isinstance(node, ast.Subscript):
            try:
                target = ast.unparse(node.value)
            except Exception:
                target = ""

            if target.endswith("environ"):
                facts["reads_env_secret"] = True

        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.startswith(("http://", "https://")):
                facts["has_remote_literal"] = True

    facts["declared_env"] = sorted(declared_env)
    facts["declared_secrets"] = sorted(declared_secrets)

    facts["auth_config_present"] = bool(
        facts["auth_config_present"]
        or facts["reads_env_secret"]
        or declared_secrets
    )

    return facts

_AUTH_AUTHORING_TOOL_NAMES = {"authoring_config_collector", "authoring_live_test"}


def _auth_override_from_request(request: dict[str, Any] | None) -> dict[str, Any]:
    request = request or {}

    raw = request.get("auth_override")
    if not isinstance(raw, dict):
        raw_config = request.get("config") if isinstance(request.get("config"), dict) else {}
        raw = raw_config.get("auth_override") if isinstance(raw_config.get("auth_override"), dict) else {}

    mode = str(raw.get("mode") or "auto").strip().lower()
    aliases = {
        "required": "force_required",
        "force_auth": "force_required",
        "add": "force_required",
        "add_auth": "force_required",
        "no_auth": "force_no_auth",
        "remove": "force_no_auth",
        "remove_auth": "force_no_auth",
        "none": "force_no_auth",
    }
    mode = aliases.get(mode, mode)

    if mode not in {"auto", "force_required", "force_no_auth"}:
        mode = "auto"

    return {
        "mode": mode,
        "reason": str(raw.get("reason") or "").strip(),
        "source": str(raw.get("source") or "user").strip() or "user",
        "updated_at": str(raw.get("updated_at") or "").strip(),
    }


def _remove_auth_authoring_tools(items: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        tool_name = str(item.get("tool_name") or item.get("name") or "").strip()
        if tool_name in _AUTH_AUTHORING_TOOL_NAMES:
            continue
        result.append(item)
    return result


def _auth_override_to_decision(override: dict[str, Any]) -> dict[str, Any] | None:
    mode = override.get("mode")
    reason = override.get("reason") or ""

    if mode == "force_required":
        return {
            "required": "yes",
            "confidence": 1.0,
            "reason": reason or "user manually requires authentication",
            "evidence": ["manual_override"],
            "security_schemes": [],
        }

    if mode == "force_no_auth":
        return {
            "required": "no",
            "confidence": 1.0,
            "reason": reason or "user manually confirms no authentication is needed",
            "evidence": ["manual_override"],
            "security_schemes": [{"type": "none"}],
        }

    return None


def _apply_auth_override_to_plan(plan: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    override = _auth_override_from_request(request)
    mode = override["mode"]

    if mode == "auto":
        plan["auth_override"] = override
        return plan

    updated = dict(plan)
    updated["auth_override"] = override

    if mode == "force_required":
        decision = _auth_override_to_decision(override) or {}
        updated["auth_decision"] = decision
        updated["auth_gate"] = {
            "status": "needs_config",
            "block_code_generation": False,
            "block_registration": True,
            "reasons": [override.get("reason") or "用户手动要求该工具必须配置认证"],
        }
        updated["requires_config"] = True
        updated["requires_authorization"] = True
        updated["requires_secret"] = True
        updated["config_form_schema"] = updated.get("config_form_schema") or _external_api_config_schema()
        updated["suggested_config_schema"] = updated.get("suggested_config_schema") or _external_api_config_schema()
        updated["ready_for_code_generation"] = False

        config = request.get("config") if isinstance(request.get("config"), dict) else {}
        sample = request.get("sample_input") if isinstance(request.get("sample_input"), dict) else {}
        existing = _remove_auth_authoring_tools(updated.get("authoring_tool_plan") or [])
        existing.insert(0, {
            "tool_name": "authoring_config_collector",
            "reason": "用户手动添加认证配置，需要收集认证/配置字段",
            "input": {
                "config_required_fields": ["auth_type", "secret_env"],
                "config": config,
                "sample_input": sample,
            },
        })
        updated["authoring_tool_plan"] = existing
        updated["requires_authoring_tools"] = True
        return updated

    if mode == "force_no_auth":
        decision = _auth_override_to_decision(override) or {}
        updated["auth_decision"] = decision
        updated["auth_gate"] = {
            "status": "none",
            "block_code_generation": False,
            "block_registration": False,
            "reasons": [override.get("reason") or "用户手动确认该工具无需认证"],
        }
        updated["requires_config"] = False
        updated["requires_authorization"] = False
        updated["requires_secret"] = False
        updated["requires_authoring_tools"] = False
        updated["authoring_tool_plan"] = _remove_auth_authoring_tools(updated.get("authoring_tool_plan") or [])

        if updated.get("manifest") and not updated.get("needs_clarification"):
            updated["ready_for_code_generation"] = True

        return updated

    return updated

def merge_auth_gate(
    planner: dict[str, Any],
    facts: dict[str, Any],
    live_test: dict[str, Any] | None = None,
    auth_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    override = auth_override if isinstance(auth_override, dict) else {}
    mode = str(override.get("mode") or "auto").strip().lower()

    if mode == "force_required":
        return {
            "status": "needs_config",
            "block_code_generation": False,
            "block_registration": True,
            "reasons": [override.get("reason") or "用户手动要求该工具必须配置认证"],
        }

    if mode == "force_no_auth":
        return {
            "status": "none",
            "block_code_generation": False,
            "block_registration": False,
            "reasons": [override.get("reason") or "用户手动确认该工具无需认证"],
        }

    planner = planner if isinstance(planner, dict) else {}
    facts = facts if isinstance(facts, dict) else {}

    raw_decision = planner.get("auth_decision") if isinstance(planner.get("auth_decision"), dict) else planner
    decision = _normalize_auth_decision(raw_decision)

    required = decision["required"]
    confidence = float(decision.get("confidence") or 0)

    if isinstance(live_test, dict) and live_test:
        status_code = live_test.get("status_code")
        missing_env = live_test.get("missing_env") or []

        if status_code in (401, 403) or missing_env:
            return {
                "status": "needs_config",
                "block_code_generation": False,
                "block_registration": True,
                "reasons": ["live_test 显示需要认证、授权失败或缺少密钥"],
            }

        if live_test.get("success") is True:
            return {
                "status": "none",
                "block_code_generation": False,
                "block_registration": False,
                "reasons": ["live_test 已通过"],
            }

    declared_secrets = facts.get("declared_secrets") or []
    auth_signals = bool(
        facts.get("reads_env_secret")
        or declared_secrets
        or facts.get("auth_config_present")
    )

    fixed_remote_entrypoint = bool(
        facts.get("has_configurable_endpoint")
        or facts.get("has_remote_literal")
    )

    if required == "yes":
        return {
            "status": "needs_config",
            "block_code_generation": False,
            "block_registration": True,
            "reasons": decision.get("evidence") or [decision.get("reason") or "Planner 判定需要认证"],
        }

    if required == "unknown":
        return {
            "status": "needs_review",
            "block_code_generation": False,
            "block_registration": True,
            "reasons": decision.get("evidence") or [decision.get("reason") or "Planner 无法确定是否需要认证"],
        }

    if required == "no":
        if auth_signals:
            return {
                "status": "needs_config",
                "block_code_generation": False,
                "block_registration": True,
                "reasons": ["Planner 判定无需认证，但代码或 manifest 存在真实 secret/env/auth 配置信号"],
            }

        # 注意：网络访问本身不是认证需求。
        # required=no 且 confidence>=0.75 时，不因为 http/url/web_fetch 自动阻止注册。
        if fixed_remote_entrypoint and confidence < 0.75:
            return {
                "status": "needs_live_test",
                "block_code_generation": False,
                "block_registration": True,
                "reasons": ["固定远程入口未经过 live_test，且 no-auth 置信度不足"],
            }

        if confidence < 0.6:
            return {
                "status": "needs_review",
                "block_code_generation": False,
                "block_registration": True,
                "reasons": ["Planner 的 no-auth 置信度过低"],
            }

    return {
        "status": "none",
        "block_code_generation": False,
        "block_registration": False,
        "reasons": ["未发现认证需求或认证风险已被确认"],
    }

def _normalizable_authoring_tool_plan(items: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in items or []:
        if isinstance(item, dict):
            tool_name = str(item.get("tool_name") or item.get("name") or "").strip()
            if not tool_name:
                continue
            normalized.append({**item, "tool_name": tool_name})
        elif isinstance(item, str):
            tool_name = item.strip()
            if tool_name:
                normalized.append({"tool_name": tool_name, "reason": "planner requested this internal authoring helper", "input": {}})
    return normalized

def _normalize_author_plan(plan: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    request = dict(request or {})
    fallback = _author_fallback_plan(request)

    normalized = {
        **_plan_defaults(),
        **(plan if isinstance(plan, dict) else {}),
    }

    model_tool_kind = str(normalized.get("tool_kind") or "").strip()

    raw_manifest = normalized.get("manifest") if isinstance(normalized.get("manifest"), dict) else {}
    request_manifest = request.get("manifest") if isinstance(request.get("manifest"), dict) else {}

    wrapper_family = _canonical_wrapper_family(
        normalized.get("wrapper_family")
        or raw_manifest.get("wrapper_family")
        or fallback.get("wrapper_family")
        or request.get("wrapper_family")
        or request_manifest.get("wrapper_family")
        or _infer_wrapper_family({**request, **normalized})
    )

    normalized["wrapper_family"] = wrapper_family

    for key in (
        "questions",
        "clarification_questions",
        "config_required_fields",
        "missing_fields",
        "authoring_tool_plan",
    ):
        if not isinstance(normalized.get(key), list):
            normalized[key] = []

    if not isinstance(normalized.get("additional_fields_schema"), (list, dict)):
        normalized["additional_fields_schema"] = []

    for key in (
        "config_form_schema",
        "suggested_entrypoint",
        "manifest",
        "authoring_context",
    ):
        if not isinstance(normalized.get(key), dict):
            normalized[key] = {}

    if not normalized.get("tool_kind") or normalized.get("tool_kind") == "unknown":
        normalized["tool_kind"] = (
            model_tool_kind
            or fallback.get("tool_kind")
            or _infer_tool_kind({**request, **normalized, "wrapper_family": wrapper_family})
        )

    if not normalized.get("operation"):
        normalized["operation"] = str(
            request.get("operation")
            or request.get("description")
            or fallback.get("operation")
            or ""
        )

    if not normalized.get("manifest"):
        normalized["manifest"] = fallback.get("manifest") or {}

    config = request.get("config") if isinstance(request.get("config"), dict) else {}
    auth_override = _auth_override_from_request(request)

    raw_decision = normalized.get("auth_decision")
    if not isinstance(raw_decision, dict) or not raw_decision:
        raw_decision = fallback.get("auth_decision") or {}

    auth_decision = _normalize_auth_decision(raw_decision, default_required="unknown")

    if auth_decision["required"] == "unknown":
        derived = _derive_auth_decision(
            {**request, "manifest": normalized.get("manifest") or {}},
            wrapper_family=wrapper_family,
            config=config,
        )
        if derived:
            auth_decision = derived

    override_decision = _auth_override_to_decision(auth_override)
    if override_decision:
        auth_decision = override_decision

    manifest = dict(normalized.get("manifest") or {})
    manifest["wrapper_family"] = wrapper_family
    manifest["tool_kind"] = normalized.get("tool_kind") or _infer_tool_kind({**request, **normalized})

    # 同步 required_capabilities，repair 后不能被旧 manifest/request 覆盖。
    capabilities: list[str] = []
    for source in (normalized, manifest):
        raw_caps = source.get("required_capabilities") if isinstance(source, dict) else None
        if isinstance(raw_caps, list):
            for item in raw_caps:
                text = str(item or "").strip()
                if text and text not in capabilities:
                    capabilities.append(text)

    if capabilities:
        normalized["required_capabilities"] = capabilities
        manifest["required_capabilities"] = capabilities

    # 兼容 manifest.optional.helper_contract，并提升为正式 helper_contract。
    optional = manifest.get("optional") if isinstance(manifest.get("optional"), dict) else {}
    optional_helper_contract = (
        optional.get("helper_contract")
        if isinstance(optional.get("helper_contract"), dict)
        else {}
    )

    helper_contract: dict[str, Any] = {}
    if optional_helper_contract:
        helper_contract.update(optional_helper_contract)

    if isinstance(manifest.get("helper_contract"), dict):
        helper_contract.update(manifest["helper_contract"])

    if isinstance(normalized.get("helper_contract"), dict):
        helper_contract.update(normalized["helper_contract"])

    helper_name = str(normalized.get("helper_name") or helper_contract.get("helper_name") or "").strip()
    if helper_name:
        helper_contract["helper_name"] = helper_name

    if helper_contract:
        normalized["helper_contract"] = helper_contract
        manifest["helper_contract"] = helper_contract

    model_config_schema = (
        normalized.get("config_form_schema")
        if isinstance(normalized.get("config_form_schema"), dict)
        else {}
    )

    manifest, connection_config_schema, connection_required_fields = _normalize_model_config_schema_into_manifest(
        manifest,
        model_config_schema,
    )

    manifest["auth_decision"] = auth_decision
    manifest["auth_override"] = auth_override
    normalized["manifest"] = manifest

    runtime_facts = extract_runtime_facts(
        str(request.get("code_block") or request.get("adapter_code") or ""),
        manifest,
    )

    live_test = request.get("live_test_result") if isinstance(request.get("live_test_result"), dict) else None
    if not live_test:
        ctx = request.get("authoring_context") if isinstance(request.get("authoring_context"), dict) else {}
        live_test = ctx.get("live_test_result") if isinstance(ctx.get("live_test_result"), dict) else None

    auth_gate = merge_auth_gate(
        {"auth_decision": auth_decision},
        runtime_facts,
        live_test,
        auth_override=auth_override,
    )

    normalized["auth_decision"] = auth_decision
    normalized["auth_gate"] = auth_gate
    normalized["auth_override"] = auth_override
    normalized["runtime_facts"] = runtime_facts

    live_success = _live_test_success_from_request(request)

    sample = request.get("sample_input") if isinstance(request.get("sample_input"), dict) else {}
    if not sample and isinstance(normalized.get("sample_input"), dict):
        sample = normalized.get("sample_input") or {}

    missing: list[str] = []

    if wrapper_family == "http_api":
        missing = _external_api_missing_fields(
            config,
            sample,
            {**request, "auth_decision": auth_decision},
            auth_decision=auth_decision,
        )
        normalized["tool_kind"] = normalized.get("tool_kind") or "external_api"
        normalized["requires_external_network"] = True
        normalized["ready_for_live_test"] = not _external_api_missing_fields(
            config,
            sample,
            {**request, "allow_external_network": True, "auth_decision": auth_decision},
            auth_decision=auth_decision,
        )
        normalized["suggested_entrypoint"] = {
            **_suggest_external_api_entrypoint(request, config),
            **(normalized.get("suggested_entrypoint") or {}),
        }
    else:
        normalized["ready_for_live_test"] = False
        normalized["suggested_entrypoint"] = normalized.get("suggested_entrypoint") or {}

    if connection_required_fields:
        missing = sorted(set([*missing, *connection_required_fields]))

    normalized["config_required_fields"] = missing
    normalized["config_form_schema"] = connection_config_schema
    normalized["suggested_config_schema"] = connection_config_schema
    normalized["additional_fields_schema"] = []

    normalized["requires_authorization"] = auth_decision["required"] in {"yes", "unknown"}
    normalized["requires_secret"] = auth_decision["required"] == "yes"
    normalized["requires_config"] = bool(missing) or auth_gate["status"] == "needs_config"
    normalized["requires_live_test"] = bool(
        auth_gate["status"] == "needs_live_test"
        or (
            wrapper_family == "http_api"
            and normalized.get("ready_for_live_test")
            and not live_success
            and not request.get("skip_live_test")
        )
    )
    normalized["requires_external_network"] = bool(
        normalized.get("requires_external_network")
        or wrapper_family in {"http_api", "managed_helper"}
        or runtime_facts.get("uses_external_network")
    )

    normalized["missing_fields"] = []
    normalized["sample_input_schema"] = manifest.get("input_schema") if isinstance(manifest.get("input_schema"), dict) else {}

    if not sample:
        input_schema = manifest.get("input_schema") if isinstance(manifest.get("input_schema"), dict) else {}
        sample = {
            key: (
                value.get("default")
                if isinstance(value, dict) and "default" in value
                else "demo"
            )
            for key, value in _schema_properties(input_schema).items()
        }

    normalized["sample_input"] = sample

    safe_questions = _safe_clarification_questions(
        normalized.get("clarification_questions") or normalized.get("questions") or [],
        fallback.get("clarification_questions") or fallback.get("questions") or [],
    )
    safe_questions = _filter_answered_clarification_questions(
        safe_questions,
        request.get("clarification_answers") or [],
    )

    normalized["clarification_questions"] = safe_questions
    normalized["questions"] = safe_questions
    normalized["needs_clarification"] = bool(safe_questions)

    plan_tools = _normalizable_authoring_tool_plan(normalized.get("authoring_tool_plan") or [])

    # Only config/auth/connection missing fields should trigger config collector.
    plan_tools = [
        item
        for item in plan_tools
        if item.get("tool_name") not in {"authoring_config_collector", "authoring_live_test"}
    ]

    if normalized["requires_config"]:
        plan_tools.insert(
            0,
            {
                "tool_name": "authoring_config_collector",
                "reason": "需要收集连接/认证/secret 引用配置；运行参数不进入配置面板",
                "input": {
                    "config_required_fields": missing,
                    "config": config,
                    "sample_input": sample,
                },
            },
        )
    elif (
        wrapper_family == "http_api"
        and normalized.get("ready_for_live_test")
        and not live_success
        and not request.get("skip_live_test")
    ):
        plan_tools.insert(
            0,
            {
                "tool_name": "authoring_live_test",
                "reason": "需要在生成 adapter 前确认固定远程接口配置和 sample input 可用",
                "input": {
                    "config": config,
                    "sample_input": sample,
                },
            },
        )

    normalized["authoring_tool_plan"] = _normalizable_authoring_tool_plan(plan_tools)
    normalized["requires_authoring_tools"] = bool(normalized["authoring_tool_plan"])

    normalized["ready_for_code_generation"] = bool(
        normalized.get("manifest")
        and not normalized.get("needs_clarification")
        and not normalized.get("requires_authoring_tools")
        and not auth_gate.get("block_code_generation")
    )

    if wrapper_family == "http_api" and normalized["requires_live_test"]:
        normalized["ready_for_code_generation"] = False

    if (
        request.get("stage") == "draft"
        and not normalized.get("needs_clarification")
        and normalized.get("manifest")
        and normalized.get("tool_kind") != "external_api"
        and not normalized.get("requires_authoring_tools")
    ):
        normalized["ready_for_code_generation"] = True

    return _apply_auth_override_to_plan(normalized, request)

def _external_api_config_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "ui": "authorization_modal",
        "required": ["base_url", "auth_type"],
        "properties": {
            "base_url": {"type": "string", "title": "连接地址 / endpoint / IP", "placeholder": "https://api.example.com"},
            "auth_type": {"type": "string", "title": "认证方式", "enum": ["none", "api_key", "token", "basic", "custom"], "default": "none"},
            "secret_env": {"type": "string", "title": "密钥名称", "description": "自动生成 env 名，可修改；代码只使用 os.getenv 引用。"},
            "secret_value": {"type": "string", "title": "密钥值", "format": "password", "writeOnly": True},
            "auth_placement": {"type": "string", "title": "密钥放置位置", "enum": ["header", "query", "bearer"], "default": "header"},
            "auth_header_name": {"type": "string", "title": "Header 名称", "placeholder": "X-API-KEY"},
            "auth_query_param": {"type": "string", "title": "Query 参数名", "placeholder": "api_key"},
            "extra": {"type": "object", "title": "其他字段", "additionalProperties": {"type": "string"}},
            "sample_input": {"type": "object", "title": "sample input（可选）"},
        },
    }



def _external_api_additional_fields_schema() -> list[dict[str, Any]]:
    return [
        {"key": "", "value": "", "sensitive": False, "description": "Optional extra connection field added by the user."}
    ]


def _strip_code_fence(text: str) -> str:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:python)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return raw


def _normalize_existing_code_fallback(code: str, manifest: dict[str, Any]) -> str:
    cap = _capability_from_dict(manifest)
    fn = cap.functions[0] if cap.functions else _function_from_dict(build_tool_manifest_draft(manifest)["functions"][0])
    code = _strip_code_fence(code)
    if not code.strip():
        return generate_adapter_code(manifest)
    try:
        tree = ast.parse(code)
        has_run = any(isinstance(node, ast.FunctionDef) and node.name == "run" for node in tree.body)
        has_main = any(isinstance(node, ast.FunctionDef) and node.name == "main" for node in tree.body)
        first_fn = _first_function_name(code)
    except SyntaxError:
        return generate_adapter_code(manifest)
    suffix = ""
    if not has_run:
        suffix += f'''


def run(payload: dict | None = None) -> dict:
    payload = dict(payload or {{}})
    value = {first_fn}(payload)
    if isinstance(value, dict):
        return value
    return {{"result": value}}
'''
    if fn.function_name not in {"run", first_fn}:
        suffix += f'''


def {fn.function_name}(payload: dict | None = None) -> dict:
    return run(payload)
'''
    if not has_main:
        suffix += '''


def main() -> None:
    import json
    import sys
    raw = sys.stdin.read().strip() or "{}"
    payload = json.loads(raw)
    print(json.dumps(run(payload), ensure_ascii=False))


if __name__ == "__main__":
    main()
'''
    return code.rstrip() + suffix


def _author_adapter_static_errors(code: str, manifest: dict[str, Any]) -> list[str]:
    errors = _code_security_errors(code)
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return errors

    function_names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    if "run" not in function_names:
        errors.append("adapter must define run(payload: dict) -> dict")
    if "main" not in function_names:
        errors.append("adapter must define a JSON stdin/stdout main() entrypoint")

    def collect_top_level_string_constants(module: ast.Module) -> dict[str, str]:
        constants: dict[str, str] = {}
        for item in module.body:
            if isinstance(item, ast.Assign) and len(item.targets) == 1:
                target = item.targets[0]
                if (
                    isinstance(target, ast.Name)
                    and isinstance(item.value, ast.Constant)
                    and isinstance(item.value.value, str)
                ):
                    constants[target.id] = item.value.value
            elif isinstance(item, ast.AnnAssign):
                target = item.target
                if (
                    isinstance(target, ast.Name)
                    and isinstance(item.value, ast.Constant)
                    and isinstance(item.value.value, str)
                ):
                    constants[target.id] = item.value.value
        return constants

    def resolve_string(node: ast.AST, constants: dict[str, str]) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return constants.get(node.id)
        return None

    try:
        cap = _capability_from_dict(manifest)
        if cap.functions and cap.functions[0].function_name not in function_names:
            errors.append(f"adapter must expose manifest function {cap.functions[0].function_name}")

        constants = collect_top_level_string_constants(tree)
        imports: set[str] = set()
        env_keys: set[str] = set()
        has_dynamic_env_read = False

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)

            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])

            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                # os.getenv("KEY") / os.getenv(SECRET_ENV)
                if node.func.attr == "getenv" and node.args:
                    value = resolve_string(node.args[0], constants)
                    if value:
                        env_keys.add(value)
                    else:
                        has_dynamic_env_read = True

                # os.environ.get("KEY") / os.environ.get(SECRET_ENV)
                elif node.func.attr == "get" and node.args:
                    try:
                        owner = ast.unparse(node.func.value)
                    except Exception:
                        owner = ""
                    if owner.endswith("environ"):
                        value = resolve_string(node.args[0], constants)
                        if value:
                            env_keys.add(value)
                        else:
                            has_dynamic_env_read = True

            elif isinstance(node, ast.Subscript):
                # os.environ["KEY"] / os.environ[SECRET_ENV]
                try:
                    owner = ast.unparse(node.value)
                except Exception:
                    owner = ""
                if owner.endswith("environ"):
                    value = resolve_string(node.slice, constants)
                    if value:
                        env_keys.add(value)
                    else:
                        has_dynamic_env_read = True

        allowed_runtime_env = {"OUTPUT_DIR", "SKILL_TRIAL_RUN"}
        declared_env = set(cap.required_env or [])
        declared_secrets = set(cap.required_secrets or [])

        for fn in cap.functions or []:
            declared_env.update(fn.required_env or [])
            declared_secrets.update(fn.required_secrets or [])

        external_env_keys = env_keys - allowed_runtime_env
        declared_external = declared_env | declared_secrets

        if imports & {"requests", "httpx", "urllib"} and cap.safety_level not in {"medium", "high"}:
            errors.append("adapter imports network libraries but manifest does not declare external network access")

        undeclared_env_keys = external_env_keys - declared_external
        if undeclared_env_keys:
            errors.append(
                "adapter reads undeclared environment variables or secrets: "
                + ", ".join(sorted(undeclared_env_keys))
            )

        # 只有在完全没有读到 declared secret，且也没有动态 env 读取时才报错。
        # 这能避免 os.getenv(SECRET_ENV) 被误判。
        if declared_secrets and not (external_env_keys & declared_secrets) and not has_dynamic_env_read:
            errors.append(
                "adapter does not read required declared secret env vars: "
                + ", ".join(sorted(declared_secrets))
            )

    except Exception:
        pass

    return sorted(set(errors))


def _validate_author_snippet(snippet: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    required = ["id", "title", "description", "code", "return_rule", "usage_policy", "priority"]
    for key in required:
        if snippet.get(key) in (None, ""):
            errors.append(f"snippet {key} is required")
    if not isinstance(snippet.get("anti_patterns", []), list) or not all(isinstance(item, str) for item in snippet.get("anti_patterns", [])):
        errors.append("snippet anti_patterns must be a string array")
    if snippet.get("usage_policy") not in _ALLOWED_USAGE_POLICIES:
        errors.append("snippet usage_policy is invalid")
    try:
        priority = int(snippet.get("priority", 0))
        if priority < 0 or priority > 100:
            warnings.append("snippet priority should be between 0 and 100")
    except Exception:
        errors.append("snippet priority must be numeric")
    try:
        cap = _capability_from_dict({**manifest, "snippets": [snippet]})
        if cap.snippets:
            result = validate_tool_snippet(cap, cap.snippets[0])
            errors.extend(result["errors"])
            warnings.extend(result["warnings"])
    except Exception as exc:
        errors.append(f"snippet cannot be parsed: {exc}")
    return {"success": not errors, "errors": sorted(set(errors)), "warnings": sorted(set(warnings))}

def _schema_from_planner_io(manifest: dict[str, Any], *, input_side: bool) -> dict[str, Any]:
    direct_key = "input_schema" if input_side else "output_schema"
    value = manifest.get(direct_key)
    if isinstance(value, dict) and value:
        return value

    legacy_key = "inputs" if input_side else "outputs"
    legacy = manifest.get(legacy_key)
    if isinstance(legacy, dict) and legacy:
        return {
            "type": "object",
            "properties": legacy,
            "required": [
                name for name, spec in legacy.items()
                if isinstance(spec, dict) and spec.get("required") is True
            ],
        }

    fields_key = "input_fields" if input_side else "output_fields"
    fields = manifest.get(fields_key)
    if isinstance(fields, list) and fields:
        props: dict[str, Any] = {}
        required: list[str] = []
        for item in fields:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            props[name] = {
                "type": item.get("type") or "string",
                "description": item.get("description") or "",
            }
            if item.get("required"):
                required.append(name)
        return {"type": "object", "properties": props, "required": required}

    if input_side:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query or API input."}
            },
            "required": ["query"],
        }

    return {
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "results": {"type": "array"},
            "total": {"type": "integer"},
            "error": {"type": "string"},
        },
        "required": ["success", "results", "total"],
    }


def _normalize_external_api_manifest_for_adapter(
    manifest: dict[str, Any],
    request: dict[str, Any],
    plan: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    original = dict(manifest or {})
    name = _slug(str(original.get("name") or request.get("tool_name") or plan.get("operation") or "external_api_tool"))
    display_name = str(original.get("display_name") or original.get("description") or name.replace("_", " ").title())

    secret_env = str((contract.get("auth") or {}).get("required_secret_env") or "").strip()
    required_secrets = [secret_env] if secret_env else []

    platform_env_prefix = str(contract.get("platform_env_prefix") or "").strip()
    required_env = []
    if platform_env_prefix:
        required_env = [
            f"{platform_env_prefix}_BASE_URL",
            f"{platform_env_prefix}_METHOD",
            f"{platform_env_prefix}_AUTH_HEADER",
            f"{platform_env_prefix}_BODY_TEMPLATE_JSON",
            f"{platform_env_prefix}_QUERY_TEMPLATE_JSON",
            f"{platform_env_prefix}_HEADERS_TEMPLATE_JSON",
        ]

    input_schema = _schema_from_planner_io(original, input_side=True)

    # external_api adapter 的最终输出不是 provider 原始响应，
    # 而是平台 wrapper 归一化后的输出。不要沿用 planner 的 raw API output_schema。
    output_schema = {
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "results": {
                "type": "array",
                "items": {"type": "object"},
            },
            "total": {"type": "integer"},
            "error": {"type": "string"},
            "status_code": {"type": "integer"},
            "response_preview": {"type": "string"},
            "knowledgeGraph": {"type": "object"},
            "answerBox": {"type": "object"},
            "relatedSearches": {
                "type": "array",
                "items": {"type": "object"},
            },
            "raw": {"type": "object"},
            "trial_run": {"type": "boolean"},
        },
        "required": ["success", "results", "total"],
    }

    adapter_import = f"backend.services.runtime_tools.custom_tools.{name}"

    return {
        **original,
        "name": name,
        "display_name": display_name,
        "description": str(original.get("description") or request.get("description") or display_name),
        "category": original.get("category") or "external_api",
        "tool_type": "custom_adapter",
        "usage_policy": original.get("usage_policy") or "helper_preferred",
        "allowed_roles": original.get("allowed_roles") or original.get("roles") or ["generic_script", "search_reader"],
        "roles": original.get("roles") or original.get("allowed_roles") or ["generic_script", "search_reader"],
        "required_capabilities": original.get("required_capabilities") or [name],
        "required_env": required_env,
        "required_secrets": required_secrets,
        "dependencies": sorted(set([*(original.get("dependencies") or []), "requests"])),
        "safety_level": original.get("safety_level") or "medium",
        "enabled": False,
        "enabled_by_default": False,
        "approval_status": "draft",
        "test_status": "untested",
        "adapter_path": f"backend/services/runtime_tools/custom_tools/{name}.py",
        "version": str(original.get("version") or "1.0.0"),
        "input_schema": input_schema,
        "output_schema": output_schema,
        "functions": [
            {
                "function_name": name,
                "import_path": adapter_import,
                "short_description": str(original.get("description") or request.get("description") or display_name),
                "when_to_use": str(original.get("when_to_use") or f"Use {display_name} when this external API is needed."),
                "signature": f"{name}(payload: dict) -> dict",
                "input_schema": input_schema,
                "output_schema": output_schema,
                "return_contract": "Returns a normalized adapter result with success/results/total. Provider raw response may be included under raw.",
                "example_call": f"from {adapter_import} import {name}\nresult = {name}(payload)\nreturn result",
                "example_stdout": "return result",
                "common_mistakes": [
                    "Do not pass API keys in payload.",
                    "Do not call external network when SKILL_TRIAL_RUN=1.",
                    "Do not require provider raw response fields as top-level adapter outputs.",
                    "Do not write or expect files outside OUTPUT_DIR.",
                ],
                "trial_mode_behavior": "When SKILL_TRIAL_RUN=1, return deterministic mock output matching output_schema.",
                "safety_notes": [
                    "Reads API secret from platform-scoped environment only.",
                    "External API network access is declared.",
                ],
                "required_env": required_env,
                "required_secrets": required_secrets,
                "usage_policy": "helper_preferred",
                "allowed_roles": original.get("allowed_roles") or original.get("roles") or ["generic_script", "search_reader"],
                "required_capabilities": original.get("required_capabilities") or [name],
                "forbidden_imports": sorted(_DANGEROUS_IMPORTS),
                "forbidden_side_effects": ["leak secrets", "undeclared network access"],
            }
        ],
        "snippets": original.get("snippets") or [],
        "needs_external_network": True,
        "needs_secret": bool(required_secrets),
        "generates_file": False,
        "high_risk": False,
    }

def _confirmed_external_api_code_contract(
    request: dict[str, Any],
    plan: dict[str, Any],
    manifest: dict[str, Any],
    sample_input: dict[str, Any],
) -> dict[str, Any]:
    raw_config = request.get("config") if isinstance(request.get("config"), dict) else {}

    saved_record = _find_saved_authoring_record_for_request(request, manifest, plan)
    saved_config = saved_record.get("config") if isinstance(saved_record.get("config"), dict) else {}

    # 保存配置是事实源；当前请求 config 只补充未保存字段。
    config = {**raw_config}
    for key, value in saved_config.items():
        if key not in config or config.get(key) in (None, "", {}, []):
            config[key] = value

    tool_name = str(
        saved_record.get("tool_name")
        or request.get("tool_name")
        or manifest.get("name")
        or plan.get("operation")
        or "external_api_tool"
    )

    platform_env_prefix = str(config.get("platform_env_prefix") or saved_record.get("platform_env_prefix") or "").strip()

    # 如果 config 里已有 secret_env=TOOLCFG_xxx_SECRET，也能反推出 prefix。
    config_secret_env = str(config.get("secret_env") or "").strip()
    if not platform_env_prefix and config_secret_env.startswith("TOOLCFG_") and config_secret_env.endswith("_SECRET"):
        platform_env_prefix = config_secret_env[: -len("_SECRET")]

    if platform_env_prefix:
        envs = _tool_platform_envs_from_prefix(platform_env_prefix)
    else:
        envs = _tool_platform_envs(tool_name)
        platform_env_prefix = envs["prefix"]

    url = (
        _resolve_env_ref_or_value(config.get("url"))
        or _resolve_env_ref_or_value(config.get("endpoint"))
        or _resolve_env_ref_or_value(config.get("base_url"))
        or os.environ.get(envs["base_url"], "")
    )

    method = (
        _resolve_env_ref_or_value(config.get("method"))
        or os.environ.get(envs["method"], "")
        or "GET"
    ).upper()

    body_template = config.get("json_body_template") if "json_body_template" in config else config.get("body_template", {})
    if isinstance(body_template, str):
        ref = _env_ref_name(body_template)
        body_template = _load_json_env(ref, {}) if ref else {}
    if not body_template:
        body_template = _load_json_env(envs["body_template"], {})
    if method not in {"GET", "HEAD"} and not body_template and isinstance(sample_input, dict):
        body_template = sample_input

    query_template = config.get("query_template") or config.get("params_template") or {}
    if isinstance(query_template, str):
        ref = _env_ref_name(query_template)
        query_template = _load_json_env(ref, {}) if ref else {}
    if not query_template:
        query_template = _load_json_env(envs["query_template"], {})
    if method in {"GET", "DELETE"} and not query_template and isinstance(sample_input, dict):
        query_template = sample_input

    headers_template = config.get("headers_template") or config.get("headers") or {}
    if isinstance(headers_template, str):
        ref = _env_ref_name(headers_template)
        headers_template = _load_json_env(ref, {}) if ref else {}
    if not headers_template:
        headers_template = _load_json_env(envs["headers_template"], {})
    headers_template = dict(headers_template or {})
    if method not in {"GET", "HEAD"}:
        headers_template.setdefault("Content-Type", "application/json")

    auth = _auth_config_for_live_test(config)
    auth_type = auth.get("type") or config.get("auth_type") or "none"

    auth_header = (
        os.environ.get(envs["auth_header"], "")
        or _resolve_env_ref_or_value(config.get("auth_header_name"))
        or auth.get("header_name")
        or "X-API-KEY"
    )

    auth_query_param = (
        os.environ.get(envs["auth_query_param"], "")
        or _resolve_env_ref_or_value(config.get("auth_query_param"))
        or auth.get("query_param")
        or ""
    )

    function_name = _slug(str(manifest.get("name") or request.get("tool_name") or plan.get("operation") or tool_name or "external_api_tool"))

    return {
        "adapter_contract_version": "1.0",
        "tool_kind": "external_api",
        "function_name": function_name,
        "method": method,
        "url": url,
        "platform_env_prefix": platform_env_prefix,
        "auth": {
            "type": auth_type,
            "required_secret_env": envs["secret"] if auth_type not in {"", "none", "no_auth", "anonymous"} else "",
            "placement": auth.get("placement") or config.get("auth_placement") or "header",
            "header_name": auth_header,
            "query_param": auth_query_param,
        },
        "headers_template": headers_template,
        "query_template": query_template or {},
        "json_body_template": body_template or {},
        "sample_input": sample_input or {},
        "runtime_protocol": {
            "must_define_run": True,
            "must_define_manifest_function": True,
            "must_define_main": True,
            "main_input": "stdin_json",
            "main_output": "stdout_json",
            "trial_env": "SKILL_TRIAL_RUN",
        },
    }

def _default_internal_normalize_code() -> str:
    return _default_core_logic_code("http_api")


def _default_helper_normalize_code() -> str:
    return _default_core_logic_code("managed_helper")


def _default_transform_code() -> str:
    return _default_core_logic_code("python_compute")


def _internal_code_errors(code: str) -> list[str]:
    return _core_logic_code_errors("http_api", code)


def _helper_normalize_code_errors(code: str) -> list[str]:
    return _core_logic_code_errors("managed_helper", code)

def _managed_helper_wrapper_code(
    contract: dict[str, Any],
    internal_code: str,
    manifest: dict[str, Any],
) -> str:
    function_name = _slug(str(contract.get("function_name") or manifest.get("name") or "managed_helper_tool"))

    helper_contract: dict[str, Any] = {}
    manifest_optional = manifest.get("optional") if isinstance(manifest.get("optional"), dict) else {}
    optional_helper_contract = (
        manifest_optional.get("helper_contract")
        if isinstance(manifest_optional.get("helper_contract"), dict)
        else {}
    )

    if optional_helper_contract:
        helper_contract.update(optional_helper_contract)

    if isinstance(manifest.get("helper_contract"), dict):
        helper_contract.update(manifest["helper_contract"])

    if isinstance(contract.get("helper_contract"), dict):
        helper_contract.update(contract["helper_contract"])

    helper_name = str(
        contract.get("helper_name")
        or helper_contract.get("helper_name")
        or ""
    ).strip()

    if helper_name:
        helper_contract["helper_name"] = helper_name

    manifest = dict(manifest or {})
    if helper_contract:
        manifest["helper_contract"] = helper_contract

    safe_internal = _strip_code_fence(internal_code or "")
    internal_errors = _core_logic_code_errors("managed_helper", safe_internal, contract=contract, manifest=manifest)
    internal_audit = "model_execute_task_core_logic"

    if internal_errors:
        safe_internal = _default_core_logic_code("managed_helper")
        internal_audit = "fallback_default_managed_helper_core_logic"

    manifest_json_literal = repr(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    helper_contract_json_literal = repr(json.dumps(helper_contract, ensure_ascii=False, sort_keys=True))

    template = r'''from __future__ import annotations

# AUTO-GENERATED MANAGED HELPER WRAPPER.
# Only MODEL_INTERNAL_CODE may come from code_model.
# Platform owns helper registry, helper call boundary, run(), manifest(), main(), trial behavior.
# internal_audit=__INTERNAL_AUDIT__

import inspect
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

from backend.services import skill_runtime as _skill_runtime


FUNCTION_NAME = __FUNCTION_NAME_LITERAL__
HELPER_CONTRACT = json.loads(__HELPER_CONTRACT_JSON_LITERAL__)
HELPER_NAME = str(HELPER_CONTRACT.get("helper_name") or "").strip()
MANIFEST_DATA = json.loads(__MANIFEST_JSON_LITERAL__)


def _schema_properties(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {}
    props = schema.get("properties")
    if isinstance(props, dict):
        return props
    if schema.get("type") == "object":
        return {}
    return {str(key): value for key, value in schema.items() if isinstance(value, dict)}


def _output_properties() -> dict[str, Any]:
    return _schema_properties(MANIFEST_DATA.get("output_schema"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first_payload_value(payload: dict[str, Any], names: list[str]) -> Any:
    for name in names:
        if name in payload and payload.get(name) not in (None, ""):
            return payload.get(name)
    return None


def _get_first(data: dict[str, Any], names: list[str], default: Any = None) -> Any:
    for name in names:
        if name in data and data.get(name) not in (None, ""):
            return data.get(name)
    return default


def _as_bool_success(value: dict[str, Any]) -> bool:
    if "success" in value:
        return bool(value.get("success"))
    status = str(value.get("status") or "").strip().lower()
    if status in {"success", "ok", "done", "completed"}:
        return True
    if status in {"failed", "error", "timeout", "not_found"}:
        return False
    if value.get("error") or value.get("error_message"):
        return False
    status_code = value.get("status_code")
    if isinstance(status_code, int) and status_code >= 400:
        return False
    return True


def _status_from_value(data: dict[str, Any], success: bool) -> str:
    status = str(data.get("status") or "").strip()
    if status:
        return status
    status_code = data.get("status_code")
    if isinstance(status_code, int):
        if status_code == 404:
            return "not_found"
        if status_code >= 400:
            return "failed"
    if data.get("timeout") is True:
        return "timeout"
    return "success" if success else "failed"


def _fill_schema_field(output: dict[str, Any], field: str, spec: dict[str, Any], payload: dict[str, Any]) -> None:
    if field in output and output.get(field) is not None:
        return

    field_lower = field.lower()
    typ = str(spec.get("type") or "").lower()
    enum_values = spec.get("enum") if isinstance(spec.get("enum"), list) else []

    if field_lower in {"success", "ok"}:
        output[field] = _as_bool_success(output)
        return

    if field_lower in {"status", "state"}:
        output[field] = _status_from_value(output, _as_bool_success(output))
        if enum_values and output[field] not in enum_values:
            output[field] = enum_values[0]
        return

    if field_lower in {"content", "text", "body", "extracted_text", "markdown"}:
        output[field] = _get_first(
            output,
            ["content", "text", "body", "extracted_text", "markdown", "result", "response"],
            "",
        )
        return

    if field_lower in {"url", "source_url", "original_url", "source", "uri"} or field_lower.endswith("_url"):
        output[field] = _get_first(
            output,
            ["url", "source_url", "original_url", "source", "uri"],
            _first_payload_value(payload, ["target_url", "url", "uri", "source"]),
        )
        return

    if field_lower in {"error", "error_message", "message"}:
        output[field] = _get_first(output, ["error_message", "error", "message"], "")
        return

    if field_lower in {"extracted_at", "created_at", "updated_at", "timestamp", "time"} or field_lower.endswith("_at"):
        output[field] = _utc_now()
        return

    if enum_values:
        output[field] = enum_values[0]
        return

    if typ in {"integer", "number"}:
        output[field] = 0
    elif typ == "boolean":
        output[field] = False
    elif typ == "array":
        output[field] = []
    elif typ == "object":
        output[field] = {}
    else:
        output[field] = ""


def _filter_kwargs_for_signature(helper: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    try:
        signature = inspect.signature(helper)
    except Exception:
        return kwargs

    params = signature.parameters
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()):
        return kwargs

    allowed = {
        name
        for name, param in params.items()
        if param.kind in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
    }
    return {key: value for key, value in kwargs.items() if key in allowed}


def _allowed_helper_names() -> set[str]:
    names: set[str] = set()
    if HELPER_NAME:
        names.add(HELPER_NAME)

    allowed = HELPER_CONTRACT.get("allowed_helpers")
    if isinstance(allowed, list):
        names.update(str(item).strip() for item in allowed if str(item).strip())

    return names


def _call_helper(helper_name: str | None = None, *args: Any, **kwargs: Any) -> Any:
    selected = str(helper_name or HELPER_NAME or "").strip()
    allowed = _allowed_helper_names()

    if not selected:
        raise RuntimeError("managed helper_contract.helper_name is required")

    if allowed and selected not in allowed:
        raise RuntimeError("helper is not allowed by helper_contract: " + selected)

    helper = getattr(_skill_runtime, selected, None)
    if not callable(helper):
        raise RuntimeError("managed helper is not available: " + selected)

    kwargs = _filter_kwargs_for_signature(helper, dict(kwargs or {}))

    return helper(*args, **kwargs)


# === MODEL_INTERNAL_CODE_START ===
__MODEL_INTERNAL_CODE__
# === MODEL_INTERNAL_CODE_END ===


def _core_context(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "wrapper_family": "managed_helper",
        "helper_name": HELPER_NAME,
        "helper_contract": HELPER_CONTRACT,
        "manifest": MANIFEST_DATA,
        "call_helper": _call_helper,
        "helpers": {name: (lambda *args, _name=name, **kwargs: _call_helper(_name, *args, **kwargs)) for name in _allowed_helper_names()},
    }


def _normalize_output(value: Any, payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, dict):
        output = dict(value)
    else:
        output = {"success": True, "result": value, "content": "" if value is None else str(value)}

    output.setdefault("success", _as_bool_success(output))

    props = _output_properties()
    for field, spec in props.items():
        _fill_schema_field(output, field, spec if isinstance(spec, dict) else {}, payload)

    return output


def _trial_result(payload: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {
        "success": True,
        "trial_run": True,
        "result": {"trial_run": True, "payload_keys": sorted(payload.keys())},
    }
    props = _output_properties()
    for field, spec in props.items():
        _fill_schema_field(output, field, spec if isinstance(spec, dict) else {}, payload)
    return output


def run(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(payload or {})

    if os.getenv("SKILL_TRIAL_RUN") == "1":
        return _trial_result(payload)

    fn = globals().get("execute_task")
    if not callable(fn):
        return {"success": False, "error": "execute_task(payload, context) is not defined"}

    try:
        value = fn(payload, _core_context(payload))
        return _normalize_output(value, payload)
    except Exception as exc:
        output: dict[str, Any] = {
            "success": False,
            "status": "failed",
            "error": str(exc),
            "error_message": str(exc),
        }
        props = _output_properties()
        for field, spec in props.items():
            _fill_schema_field(output, field, spec if isinstance(spec, dict) else {}, payload)
        return output


def __FUNCTION_NAME__(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return run(payload)


def manifest() -> dict[str, Any]:
    return MANIFEST_DATA


def main() -> None:
    raw = sys.stdin.read().strip() or "{}"
    payload = json.loads(raw)
    print(json.dumps(run(payload), ensure_ascii=False))


if __name__ == "__main__":
    main()
'''

    return (
        template
        .replace("__FUNCTION_NAME_LITERAL__", repr(function_name))
        .replace("__HELPER_CONTRACT_JSON_LITERAL__", helper_contract_json_literal)
        .replace("__MANIFEST_JSON_LITERAL__", manifest_json_literal)
        .replace("__MODEL_INTERNAL_CODE__", safe_internal)
        .replace("__INTERNAL_AUDIT__", internal_audit)
        .replace("__FUNCTION_NAME__", function_name)
    )

def _transform_code_errors(code: str) -> list[str]:
    return _core_logic_code_errors("python_compute", code)

def _file_transform_code_errors(code: str) -> list[str]:
    return _core_logic_code_errors("file_io", code)

def _database_query_code_errors(code: str) -> list[str]:
    return _core_logic_code_errors("database_query", code)

def _python_compute_wrapper_code(contract: dict[str, Any], internal_code: str, manifest: dict[str, Any]) -> str:
    function_name = _slug(str(contract.get("function_name") or manifest.get("name") or "python_compute_tool"))

    safe_internal = _strip_code_fence(internal_code or "")
    if _core_logic_code_errors("python_compute", safe_internal, contract=contract, manifest=manifest):
        safe_internal = _default_core_logic_code("python_compute")

    manifest_json_literal = repr(json.dumps(manifest, ensure_ascii=False, sort_keys=True))

    return f'''from __future__ import annotations

# AUTO-GENERATED PYTHON COMPUTE WRAPPER.
# Only MODEL_INTERNAL_CODE may come from code_model.
# Platform owns run(), manifest(), main(), trial behavior and IO protocol.

import json
import os
import sys
from typing import Any


FUNCTION_NAME = {function_name!r}
MANIFEST_DATA = json.loads({manifest_json_literal})


# === MODEL_INTERNAL_CODE_START ===
{safe_internal}
# === MODEL_INTERNAL_CODE_END ===


def _trial_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {{
        "success": True,
        "result": {{"trial_run": True, "payload_keys": sorted(payload.keys())}},
        "trial_run": True,
    }}


def _core_context(payload: dict[str, Any]) -> dict[str, Any]:
    return {{
        "wrapper_family": "python_compute",
        "manifest": MANIFEST_DATA,
    }}


def run(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(payload or {{}})

    if os.getenv("SKILL_TRIAL_RUN") == "1":
        return _trial_result(payload)

    fn = globals().get("execute_task")
    if not callable(fn):
        return {{"success": False, "error": "execute_task(payload, context) is not defined"}}

    value = fn(payload, _core_context(payload))

    if isinstance(value, dict):
        return value

    return {{"success": True, "result": value}}


def {function_name}(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return run(payload)


def manifest() -> dict[str, Any]:
    return MANIFEST_DATA


def main() -> None:
    raw = sys.stdin.read().strip() or "{{}}"
    payload = json.loads(raw)
    print(json.dumps(run(payload), ensure_ascii=False))


if __name__ == "__main__":
    main()
'''

def _external_api_wrapper_code(contract: dict[str, Any], internal_code: str, manifest: dict[str, Any]) -> str:
    function_name = _slug(str(contract.get("function_name") or manifest.get("name") or "external_api_tool"))

    platform_env_prefix = str(contract.get("platform_env_prefix") or "").strip()
    if not platform_env_prefix:
        platform_env_prefix = _tool_platform_env_prefix(function_name)

    endpoint_env = f"{platform_env_prefix}_BASE_URL"
    method_env = f"{platform_env_prefix}_METHOD"
    auth_header_env = f"{platform_env_prefix}_AUTH_HEADER"
    body_template_env = f"{platform_env_prefix}_BODY_TEMPLATE_JSON"
    query_template_env = f"{platform_env_prefix}_QUERY_TEMPLATE_JSON"
    headers_template_env = f"{platform_env_prefix}_HEADERS_TEMPLATE_JSON"

    endpoint = str(contract.get("url") or "")
    method = str(contract.get("method") or "POST").upper()

    auth = contract.get("auth") if isinstance(contract.get("auth"), dict) else {}
    secret_env = str(auth.get("required_secret_env") or f"{platform_env_prefix}_SECRET").strip()
    header_name = str(auth.get("header_name") or "X-API-KEY").strip() or "X-API-KEY"

    body_template = contract.get("json_body_template") if isinstance(contract.get("json_body_template"), dict) else {}
    query_template = contract.get("query_template") if isinstance(contract.get("query_template"), dict) else {}
    headers_template = contract.get("headers_template") if isinstance(contract.get("headers_template"), dict) else {}

    safe_internal = _strip_code_fence(internal_code or "")
    internal_errors = _core_logic_code_errors("http_api", safe_internal, contract=contract, manifest=manifest)

    if internal_errors:
        safe_internal = _default_core_logic_code("http_api")
        internal_audit = "fallback_default_http_core_logic"
    else:
        internal_audit = "model_execute_task_core_logic"

    body_template_json_literal = repr(json.dumps(body_template, ensure_ascii=False, sort_keys=True))
    query_template_json_literal = repr(json.dumps(query_template, ensure_ascii=False, sort_keys=True))
    headers_template_json_literal = repr(json.dumps(headers_template, ensure_ascii=False, sort_keys=True))
    manifest_json_literal = repr(json.dumps(manifest, ensure_ascii=False, sort_keys=True))

    return f'''from __future__ import annotations

# AUTO-GENERATED EXTERNAL API WRAPPER.
# Only MODEL_INTERNAL_CODE may come from code_model.
# Platform owns endpoint/env/auth/secret/network boundary, manifest(), run(), main(), trial behavior.
# internal_audit={internal_audit}

import json
import os
import sys
from typing import Any

import requests


FUNCTION_NAME = {function_name!r}
TOOL_ENV_PREFIX = {platform_env_prefix!r}
ENDPOINT_ENV = {endpoint_env!r}
METHOD_ENV = {method_env!r}
SECRET_ENV = {secret_env!r}
AUTH_HEADER_ENV = {auth_header_env!r}
BODY_TEMPLATE_ENV = {body_template_env!r}
QUERY_TEMPLATE_ENV = {query_template_env!r}
HEADERS_TEMPLATE_ENV = {headers_template_env!r}

ENDPOINT = os.getenv(ENDPOINT_ENV, {endpoint!r})
METHOD = os.getenv(METHOD_ENV, {method!r}).upper()
AUTH_HEADER_NAME = os.getenv(AUTH_HEADER_ENV, {header_name!r})
BODY_TEMPLATE = json.loads(os.getenv(BODY_TEMPLATE_ENV, {body_template_json_literal}))
QUERY_TEMPLATE = json.loads(os.getenv(QUERY_TEMPLATE_ENV, {query_template_json_literal}))
HEADERS_TEMPLATE = json.loads(os.getenv(HEADERS_TEMPLATE_ENV, {headers_template_json_literal}))
MANIFEST_DATA = json.loads({manifest_json_literal})


def _flatten(prefix: str, value: Any, out: dict[str, Any]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{{prefix}}.{{key}}" if prefix else str(key)
            _flatten(path, item, out)
    else:
        out[prefix] = value


def _render_string(text: str, payload: dict[str, Any]) -> str:
    values: dict[str, Any] = {{}}
    _flatten("", payload, values)
    for key, value in values.items():
        rendered = "" if value is None else str(value)
        text = text.replace("${{input." + key + "}}", rendered)
        text = text.replace("${{payload." + key + "}}", rendered)
        text = text.replace("{{{{" + key + "}}}}", rendered)
        text = text.replace("{{{{ " + key + " }}}}", rendered)
    return text


def _render(value: Any, payload: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return _render_string(value, payload)
    if isinstance(value, dict):
        return {{key: _render(item, payload) for key, item in value.items() if item is not None}}
    if isinstance(value, list):
        return [_render(item, payload) for item in value]
    return value


# === MODEL_INTERNAL_CODE_START ===
{safe_internal}
# === MODEL_INTERNAL_CODE_END ===


def _base_headers(payload: dict[str, Any]) -> dict[str, Any]:
    headers = _render(HEADERS_TEMPLATE, payload)
    headers = dict(headers or {{}}) if isinstance(headers, dict) else {{}}
    if METHOD not in {{"GET", "HEAD"}}:
        headers.setdefault("Content-Type", "application/json")
    return headers


def _base_params(payload: dict[str, Any]) -> dict[str, Any]:
    params = _render(QUERY_TEMPLATE, payload)
    return dict(params or {{}}) if isinstance(params, dict) else {{}}


def _base_json(payload: dict[str, Any]) -> Any:
    body = _render(BODY_TEMPLATE, payload)
    return dict(body or {{}}) if isinstance(body, dict) else body


def _http_request(
    *,
    url: str | None = None,
    method: str | None = None,
    headers: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    json: Any = None,
    data: Any = None,
    timeout: float | int | None = None,
) -> dict[str, Any]:
    endpoint = str(url or ENDPOINT or "")
    http_method = str(method or METHOD or "GET").upper()

    if not endpoint.startswith(("http://", "https://")):
        return {{"success": False, "error": "Invalid endpoint", "raw": None}}

    api_key = os.getenv(SECRET_ENV) if SECRET_ENV else ""
    if SECRET_ENV and not api_key:
        return {{
            "success": False,
            "error": f"Missing required environment variable: {{SECRET_ENV}}",
            "raw": None,
        }}

    request_headers = _base_headers({{}})
    if isinstance(headers, dict):
        request_headers.update({{str(k): str(v) for k, v in headers.items() if v is not None}})

    if SECRET_ENV:
        request_headers[AUTH_HEADER_NAME] = api_key

    body = json_body if json_body is not None else json
    timeout_value = float(timeout or 30)

    try:
        response = requests.request(
            http_method,
            endpoint,
            headers=request_headers,
            params=params if isinstance(params, dict) else None,
            json=body if http_method not in {{"GET", "HEAD"}} and data is None else None,
            data=data,
            timeout=timeout_value,
        )
        status_code = response.status_code
        preview = response.text[:4000]
        response.raise_for_status()

        try:
            parsed: Any = response.json()
        except ValueError:
            parsed = {{"text": response.text}}

        return {{
            "success": True,
            "status_code": status_code,
            "data": parsed,
            "text": response.text,
            "headers": dict(response.headers),
            "raw": parsed,
        }}

    except requests.exceptions.HTTPError:
        return {{
            "success": False,
            "error": f"HTTP {{status_code}}",
            "status_code": status_code,
            "response_preview": preview,
            "raw": None,
        }}
    except requests.exceptions.RequestException as exc:
        return {{"success": False, "error": str(exc), "raw": None}}


def _trial_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {{
        "success": True,
        "trial_run": True,
        "request_preview": {{
            "method": METHOD,
            "endpoint_configured": bool(ENDPOINT),
            "payload_keys": sorted(payload.keys()),
        }},
        "raw": {{}},
    }}


def _core_context(payload: dict[str, Any]) -> dict[str, Any]:
    return {{
        "wrapper_family": "http_api",
        "manifest": MANIFEST_DATA,
        "endpoint": ENDPOINT,
        "method": METHOD,
        "base_headers": _base_headers(payload),
        "base_params": _base_params(payload),
        "base_json": _base_json(payload),
        "http_request": _http_request,
    }}


def run(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(payload or {{}})

    if os.getenv("SKILL_TRIAL_RUN") == "1":
        return _trial_result(payload)

    fn = globals().get("execute_task")
    if not callable(fn):
        return {{"success": False, "error": "execute_task(payload, context) is not defined", "raw": None}}

    try:
        value = fn(payload, _core_context(payload))
    except Exception as exc:
        return {{"success": False, "error": str(exc), "raw": None}}

    if isinstance(value, dict):
        value.setdefault("success", True)
        return value

    return {{"success": True, "result": value, "raw": value}}


def {function_name}(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return run(payload)


def manifest() -> dict[str, Any]:
    return MANIFEST_DATA


def main() -> None:
    raw = sys.stdin.read().strip() or "{{}}"
    payload = json.loads(raw)
    print(json.dumps(run(payload), ensure_ascii=False))


if __name__ == "__main__":
    main()
'''

def _file_io_wrapper_code(
    contract: dict[str, Any],
    internal_code: str,
    manifest: dict[str, Any],
) -> str:
    function_name = _slug(str(contract.get("function_name") or manifest.get("name") or "file_tool"))

    safe_internal = _strip_code_fence(internal_code or "")
    if _core_logic_code_errors("file_io", safe_internal, contract=contract, manifest=manifest):
        safe_internal = _default_core_logic_code("file_io")

    manifest = dict(manifest or {})
    manifest["wrapper_family"] = "file_io"
    manifest_json_literal = repr(json.dumps(manifest, ensure_ascii=False, sort_keys=True))

    template = r'''from __future__ import annotations

# AUTO-GENERATED FILE_IO WRAPPER.
# Only MODEL_INTERNAL_CODE may come from code_model.
# Platform owns OUTPUT_DIR, path safety, run(), manifest(), main(), trial behavior.

import json
import os
import sys
from pathlib import Path
from typing import Any


FUNCTION_NAME = __FUNCTION_NAME_LITERAL__
MANIFEST_DATA = json.loads(__MANIFEST_JSON_LITERAL__)


def safe_output_path(filename: str) -> str:
    base = Path(os.getenv("OUTPUT_DIR") or "outputs").resolve()
    base.mkdir(parents=True, exist_ok=True)

    raw = str(filename or "output.txt").strip().replace("\\", "/")
    raw = raw.split("/")[-1] or "output.txt"

    path = (base / raw).resolve()

    if path != base and base not in path.parents:
        raise RuntimeError("unsafe output path")

    return str(path)


# === MODEL_INTERNAL_CODE_START ===
__MODEL_INTERNAL_CODE__
# === MODEL_INTERNAL_CODE_END ===


def _collect_paths(result: dict[str, Any]) -> list[str]:
    paths: list[str] = []

    for key in ("path", "file_path", "output_path"):
        value = result.get(key)
        if isinstance(value, str) and value:
            paths.append(value)

    for key in ("file_paths", "paths"):
        value = result.get(key)
        if isinstance(value, list):
            paths.extend(str(item) for item in value if item)

    value = result.get("file_outputs")
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item.get("path"):
                paths.append(str(item["path"]))

    return sorted(set(paths))


def _validate_file_result(result: dict[str, Any]) -> dict[str, Any]:
    result = dict(result or {})
    paths = _collect_paths(result)
    missing = [path for path in paths if not Path(path).exists()]

    result.setdefault("file_paths", paths)
    result.setdefault("success", not missing)

    if missing:
        result["success"] = False
        result["error"] = "missing output files: " + ", ".join(missing)

    return result


def _trial_result(payload: dict[str, Any]) -> dict[str, Any]:
    path = safe_output_path("trial_output.txt")
    Path(path).write_text("trial run", encoding="utf-8")
    return {
        "success": True,
        "trial_run": True,
        "path": path,
        "file_path": path,
        "file_paths": [path],
        "file_outputs": [{"path": path}],
    }


def _core_context(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "wrapper_family": "file_io",
        "manifest": MANIFEST_DATA,
        "output_dir": os.getenv("OUTPUT_DIR") or "outputs",
        "safe_output_path": safe_output_path,
    }


def run(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(payload or {})

    if os.getenv("SKILL_TRIAL_RUN") == "1":
        return _trial_result(payload)

    fn = globals().get("execute_task")
    if not callable(fn):
        return {"success": False, "error": "execute_task(payload, context) is not defined"}

    try:
        value = fn(payload, _core_context(payload))
        if not isinstance(value, dict):
            value = {"success": True, "result": value}
        return _validate_file_result(value)
    except Exception as exc:
        return {"success": False, "error": str(exc), "file_paths": [], "file_outputs": []}


def __FUNCTION_NAME__(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return run(payload)


def manifest() -> dict[str, Any]:
    return MANIFEST_DATA


def main() -> None:
    raw = sys.stdin.read().strip() or "{}"
    payload = json.loads(raw)
    print(json.dumps(run(payload), ensure_ascii=False))


if __name__ == "__main__":
    main()
'''

    return (
        template
        .replace("__FUNCTION_NAME_LITERAL__", repr(function_name))
        .replace("__MANIFEST_JSON_LITERAL__", manifest_json_literal)
        .replace("__MODEL_INTERNAL_CODE__", safe_internal)
        .replace("__FUNCTION_NAME__", function_name)
    )

def _database_query_wrapper_code(
    contract: dict[str, Any],
    internal_code: str,
    manifest: dict[str, Any],
) -> str:
    function_name = _slug(str(contract.get("function_name") or manifest.get("name") or "database_tool"))

    safe_internal = _strip_code_fence(internal_code or "")
    if _core_logic_code_errors("database_query", safe_internal, contract=contract, manifest=manifest):
        safe_internal = _default_core_logic_code("database_query")

    manifest = dict(manifest or {})
    manifest["wrapper_family"] = "database_query"
    manifest_json_literal = repr(json.dumps(manifest, ensure_ascii=False, sort_keys=True))

    template = r'''from __future__ import annotations

# AUTO-GENERATED DATABASE_QUERY WRAPPER.
# Only MODEL_INTERNAL_CODE may come from code_model.
# Platform owns DB connection, readonly SQL validation, execution, manifest(), main().

import json
import os
import re
import sqlite3
import sys
from typing import Any


FUNCTION_NAME = __FUNCTION_NAME_LITERAL__
MANIFEST_DATA = json.loads(__MANIFEST_JSON_LITERAL__)

_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|replace|merge|grant|revoke|attach|detach|pragma|vacuum)\b",
    re.I,
)


# === MODEL_INTERNAL_CODE_START ===
__MODEL_INTERNAL_CODE__
# === MODEL_INTERNAL_CODE_END ===


def _database_url() -> str:
    return (
        os.getenv("DATABASE_URL")
        or str(MANIFEST_DATA.get("database_url") or "")
        or str(MANIFEST_DATA.get("dsn") or "")
    )


def _validate_readonly_sql(sql: str) -> None:
    text = str(sql or "").strip()

    if not text:
        raise RuntimeError("SQL is empty")

    if ";" in text.rstrip(";"):
        raise RuntimeError("multiple SQL statements are not allowed")

    lowered = text.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise RuntimeError("only SELECT/WITH read-only SQL is allowed")

    if _FORBIDDEN_SQL.search(text):
        raise RuntimeError("forbidden SQL operation detected")


def _connect(url: str):
    if not url:
        raise RuntimeError("DATABASE_URL is required")

    if url.startswith("sqlite:///"):
        return sqlite3.connect(url[len("sqlite:///"):])

    if url.startswith("sqlite://"):
        return sqlite3.connect(url[len("sqlite://"):])

    if url == ":memory:" or url.endswith(".db") or "/" in url:
        return sqlite3.connect(url)

    raise RuntimeError("generic database_query wrapper currently supports sqlite only; register a managed helper for other engines")


def query_readonly(sql: str, params: dict[str, Any] | None = None, limit: int = 100) -> list[dict[str, Any]]:
    _validate_readonly_sql(sql)

    conn = _connect(_database_url())
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(sql, params or {})
        rows = [dict(row) for row in cursor.fetchmany(int(limit or 100))]
        return rows
    finally:
        conn.close()


def _trial_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {"success": True, "trial_run": True, "rows": [], "results": [], "total": 0}


def _core_context(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "wrapper_family": "database_query",
        "manifest": MANIFEST_DATA,
        "query_readonly": query_readonly,
    }


def run(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(payload or {})

    if os.getenv("SKILL_TRIAL_RUN") == "1":
        return _trial_result(payload)

    fn = globals().get("execute_task")
    if not callable(fn):
        return {"success": False, "error": "execute_task(payload, context) is not defined", "rows": [], "results": []}

    try:
        value = fn(payload, _core_context(payload))
        if isinstance(value, dict):
            value.setdefault("success", True)
            return value
        return {"success": True, "result": value}
    except Exception as exc:
        return {"success": False, "error": str(exc), "rows": [], "results": [], "total": 0}


def __FUNCTION_NAME__(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return run(payload)


def manifest() -> dict[str, Any]:
    return MANIFEST_DATA


def main() -> None:
    raw = sys.stdin.read().strip() or "{}"
    payload = json.loads(raw)
    print(json.dumps(run(payload), ensure_ascii=False))


if __name__ == "__main__":
    main()
'''

    return (
        template
        .replace("__FUNCTION_NAME_LITERAL__", repr(function_name))
        .replace("__MANIFEST_JSON_LITERAL__", manifest_json_literal)
        .replace("__MODEL_INTERNAL_CODE__", safe_internal)
        .replace("__FUNCTION_NAME__", function_name)
    )

def _local_command_wrapper_code(
    contract: dict[str, Any],
    internal_code: str,
    manifest: dict[str, Any],
) -> str:
    function_name = _slug(str(contract.get("function_name") or manifest.get("name") or "local_command_tool"))

    safe_internal = _strip_code_fence(internal_code or "")
    if _core_logic_code_errors("local_command", safe_internal, contract=contract, manifest=manifest):
        safe_internal = _default_core_logic_code("local_command")

    manifest = dict(manifest or {})
    manifest["wrapper_family"] = "local_command"
    manifest["safety_level"] = "high"
    manifest.setdefault("approval_status", "pending_review")

    manifest_json_literal = repr(json.dumps(manifest, ensure_ascii=False, sort_keys=True))

    template = r'''from __future__ import annotations

# AUTO-GENERATED LOCAL_COMMAND WRAPPER.
# Only MODEL_INTERNAL_CODE may come from code_model.
# Platform owns approval gate, command rendering, execution, manifest(), main().

import json
import os
import subprocess
import sys
from typing import Any


FUNCTION_NAME = __FUNCTION_NAME_LITERAL__
MANIFEST_DATA = json.loads(__MANIFEST_JSON_LITERAL__)


# === MODEL_INTERNAL_CODE_START ===
__MODEL_INTERNAL_CODE__
# === MODEL_INTERNAL_CODE_END ===


def _render_template(value: Any, payload: dict[str, Any]) -> Any:
    if isinstance(value, str):
        result = value
        for key, item in payload.items():
            result = result.replace("{{" + str(key) + "}}", str(item))
        return result

    if isinstance(value, list):
        return [_render_template(item, payload) for item in value]

    if isinstance(value, dict):
        return {str(k): _render_template(v, payload) for k, v in value.items()}

    return value


def render_command(payload: dict[str, Any]) -> list[str]:
    command = MANIFEST_DATA.get("command_args_template") or MANIFEST_DATA.get("command")

    if isinstance(command, str):
        raise RuntimeError("local_command requires command_args_template list, not shell string")

    if not isinstance(command, list) or not command:
        raise RuntimeError("manifest.command_args_template must be a non-empty list")

    rendered = _render_template(command, payload)
    argv = [str(item) for item in rendered if str(item).strip()]

    if not argv:
        raise RuntimeError("empty command")

    return argv


def _is_approved() -> bool:
    return str(MANIFEST_DATA.get("approval_status") or "").lower() == "approved"


def _trial_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": True,
        "trial_run": True,
        "command_preview": render_command(payload) if MANIFEST_DATA.get("command_args_template") else [],
    }


def _core_context(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "wrapper_family": "local_command",
        "manifest": MANIFEST_DATA,
        "render_command": render_command,
    }


def run(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(payload or {})

    if os.getenv("SKILL_TRIAL_RUN") == "1":
        return _trial_result(payload)

    if not _is_approved():
        return {
            "success": False,
            "error": "local command tool is not approved",
            "approval_status": MANIFEST_DATA.get("approval_status") or "pending_review",
        }

    fn = globals().get("execute_task")
    if not callable(fn):
        return {"success": False, "error": "execute_task(payload, context) is not defined"}

    try:
        plan = fn(payload, _core_context(payload))
        if not isinstance(plan, dict):
            raise RuntimeError("execute_task must return a dict")

        argv = plan.get("argv")
        if not isinstance(argv, list):
            argv = render_command(payload)

        argv = [str(item) for item in argv if str(item).strip()]
        timeout = float(MANIFEST_DATA.get("timeout_seconds") or payload.get("timeout") or 30)

        completed = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

        return {
            "success": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "command_preview": argv,
        }

    except Exception as exc:
        return {"success": False, "error": str(exc)}


def __FUNCTION_NAME__(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return run(payload)


def manifest() -> dict[str, Any]:
    return MANIFEST_DATA


def main() -> None:
    raw = sys.stdin.read().strip() or "{}"
    payload = json.loads(raw)
    print(json.dumps(run(payload), ensure_ascii=False))


if __name__ == "__main__":
    main()
'''

    return (
        template
        .replace("__FUNCTION_NAME_LITERAL__", repr(function_name))
        .replace("__MANIFEST_JSON_LITERAL__", manifest_json_literal)
        .replace("__MODEL_INTERNAL_CODE__", safe_internal)
        .replace("__FUNCTION_NAME__", function_name)
    )

async def _author_core_logic_with_model(
    *,
    wrapper_family: str,
    request: dict[str, Any],
    plan: dict[str, Any],
    contract: dict[str, Any],
    manifest: dict[str, Any],
    sample_input: dict[str, Any],
    model_notes: list[str],
    warnings: list[str],
) -> tuple[str, list[str]]:
    wrapper_family = _canonical_wrapper_family(wrapper_family)

    def _allowed_imports_for_prompt() -> list[str]:
        # 优先复用你后面如果已经加好的 _core_logic_policy。
        if "_core_logic_policy" in globals():
            try:
                policy = _core_logic_policy(wrapper_family, contract=contract, manifest=manifest)  # type: ignore[name-defined]
                raw = policy.get("allowed_imports") if isinstance(policy, dict) else None
                if isinstance(raw, set):
                    return sorted(str(item) for item in raw if str(item).strip())
                if isinstance(raw, list):
                    return sorted(str(item) for item in raw if str(item).strip())
            except Exception:
                pass

        # 兜底：不依赖额外函数，避免你现在直接替换时报 NameError。
        safe_stdlib_imports = {
            "base64",
            "collections",
            "csv",
            "datetime",
            "decimal",
            "fractions",
            "functools",
            "hashlib",
            "html",
            "io",
            "itertools",
            "json",
            "math",
            "operator",
            "random",
            "re",
            "statistics",
            "string",
            "textwrap",
            "typing",
            "uuid",
        }

        allowed = set(safe_stdlib_imports)

        dependency_import_names = globals().get("_DEPENDENCY_IMPORT_NAMES")
        if not isinstance(dependency_import_names, dict):
            dependency_import_names = {
                "python-docx": "docx",
                "python-pptx": "pptx",
                "pillow": "PIL",
                "opencv-python": "cv2",
                "beautifulsoup4": "bs4",
                "scikit-learn": "sklearn",
            }

        deps = manifest.get("dependencies")
        if isinstance(deps, list):
            for dep in deps:
                dep_text = str(dep or "").strip()
                if not dep_text:
                    continue
                mapped = dependency_import_names.get(dep_text, dep_text)
                allowed.add(str(mapped).replace("-", "_").split(".")[0])

        for source in (
            manifest.get("code_policy") if isinstance(manifest.get("code_policy"), dict) else {},
            contract.get("code_policy") if isinstance(contract.get("code_policy"), dict) else {},
        ):
            raw = source.get("allowed_imports")
            if isinstance(raw, list):
                allowed.update(
                    str(item).replace("-", "_").split(".")[0]
                    for item in raw
                    if str(item).strip()
                )

        return sorted(allowed)

    allowed_imports = _allowed_imports_for_prompt()

    context_guidance = {
        "http_api": (
            "context contains http_request(...), endpoint, method, base_headers, base_params, base_json. "
            "Use context['http_request'] to perform the platform-controlled HTTP request. "
            "Do not import requests/httpx/urllib yourself and do not read secrets yourself."
        ),
        "managed_helper": (
            "context contains call_helper(helper_name, *args, **kwargs), helpers dict, helper_name, helper_contract. "
            "Use only context['call_helper'] or context['helpers'][allowed_name] to call platform-managed helpers."
        ),
        "python_compute": (
            "context contains manifest and wrapper_family. Implement deterministic local business logic."
        ),
        "file_io": (
            "context contains safe_output_path(filename), output_dir and manifest. "
            "Use context['safe_output_path'] for every file path before opening/writing. "
            "Never construct output paths manually."
        ),
        "database_query": (
            "context contains query_readonly(sql, params, limit). "
            "Only SELECT/WITH readonly SQL is allowed. Do not import database drivers or connect directly."
        ),
        "local_command": (
            "context contains render_command(payload). "
            "Return {'argv': [...]} only; platform approval gate controls execution."
        ),
    }.get(wrapper_family, "context contains manifest and wrapper metadata.")

    messages = [
        {
            "role": "system",
            "content": (
                "You are code_model for a generic tool authoring platform. "
                "Return strict JSON only: {\"internal_code\":\"...python code...\"}. "
                "Do NOT write a full adapter. "
                "Write the actual core task logic for this tool. "
                "The internal code must define exactly one function: "
                "execute_task(payload: dict, context: dict) -> dict. "
                "The platform wrapper controls run(), manifest(), main(), trial behavior, IO protocol, auth, secrets, "
                "network/database/helper/file/command boundaries, and unsafe operations. "
                "Do not define run/main/manifest. "
                "Do not read environment variables. "
                "Do not call eval/exec/compile/__import__. "
                "Allowed imports for this tool are: "
                + (", ".join(allowed_imports) if allowed_imports else "(none)")
                + ". Do not import anything else. "
                "External/network/database/file/command capabilities must use context APIs, not direct imports. "
                + context_guidance
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "wrapper_family": wrapper_family,
                    "user_request": {
                        "description": request.get("description"),
                        "operation": request.get("operation"),
                        "tool_name": request.get("tool_name"),
                        "input_description": request.get("input_description"),
                        "output_description": request.get("output_description"),
                        "tool_kind": plan.get("tool_kind") or manifest.get("tool_kind"),
                    },
                    "manifest": manifest,
                    "adapter_contract": contract,
                    "sample_input": sample_input,
                    "input_schema": manifest.get("input_schema") or {},
                    "output_schema": manifest.get("output_schema") or {},
                    "code_policy": manifest.get("code_policy") or contract.get("code_policy") or {},
                    "dependencies": manifest.get("dependencies") or contract.get("dependencies") or [],
                    "implementation_plan": plan.get("implementation_plan"),
                    "context_guidance": context_guidance,
                    "task": (
                        "Write the real core functionality in execute_task(payload, context). "
                        "Do not merely reformat an already-perfect result. "
                        "Use context capabilities to perform the required operation, then return a dict conforming to output_schema. "
                        "Template/wrapper code is not part of your output."
                    ),
                },
                ensure_ascii=False,
            ),
        },
    ]

    internal_json, code_ack, code_err = await _complete_author_model(
        "code",
        messages,
        reason=f"creator_tool_author_{wrapper_family}_core_logic",
    )

    if code_ack:
        model_notes.append(f"code_model={code_ack['model']}")

    if code_err:
        warnings.append(f"code_model unavailable, used default {wrapper_family} core logic: {code_err}")

    internal_code = _strip_code_fence(
        str((internal_json or {}).get("internal_code") or (internal_json or {}).get("code") or "")
    )

    try:
        internal_errors = _core_logic_code_errors(
            wrapper_family,
            internal_code,
            contract=contract,
            manifest=manifest,
        )
    except TypeError:
        # 兼容你当前旧签名 _core_logic_code_errors(wrapper_family, code)。
        internal_errors = _core_logic_code_errors(wrapper_family, internal_code)

    if internal_errors:
        warnings.extend(f"{wrapper_family}_core_logic fallback: {err}" for err in internal_errors)
        internal_code = _default_core_logic_code(wrapper_family)

    return internal_code, internal_errors

async def _author_wrapper_with_core_logic(
    *,
    wrapper_family: str,
    request: dict[str, Any],
    plan: dict[str, Any],
    contract: dict[str, Any],
    raw_manifest: dict[str, Any],
    sample_input: dict[str, Any],
    contract_registry_validation: dict[str, Any],
    model_notes: list[str],
    warnings: list[str],
    live_success: bool = False,
) -> dict[str, Any]:
    wrapper_family = _canonical_wrapper_family(wrapper_family)

    manifest = dict(raw_manifest or {})
    manifest["wrapper_family"] = wrapper_family
    manifest["tool_type"] = manifest.get("tool_type") or "custom_adapter"

    if wrapper_family == "http_api":
        draft_contract = _confirmed_external_api_code_contract(request, plan, manifest, sample_input)
        manifest = _normalize_external_api_manifest_for_adapter(manifest, request, plan, draft_contract)
        manifest["wrapper_family"] = "http_api"
        contract = _confirmed_external_api_code_contract(request, plan, manifest, sample_input)

    elif wrapper_family == "managed_helper":
        capabilities = contract.get("required_capabilities")
        if isinstance(capabilities, list):
            manifest["required_capabilities"] = [str(item) for item in capabilities if str(item).strip()]

        helper_contract: dict[str, Any] = {}
        optional = manifest.get("optional") if isinstance(manifest.get("optional"), dict) else {}
        if isinstance(optional.get("helper_contract"), dict):
            helper_contract.update(optional["helper_contract"])
        if isinstance(manifest.get("helper_contract"), dict):
            helper_contract.update(manifest["helper_contract"])
        if isinstance(contract.get("helper_contract"), dict):
            helper_contract.update(contract["helper_contract"])

        helper_name = str(contract.get("helper_name") or helper_contract.get("helper_name") or "").strip()
        if helper_name:
            helper_contract["helper_name"] = helper_name
        if helper_contract:
            manifest["helper_contract"] = helper_contract

    elif wrapper_family == "python_compute":
        manifest["required_capabilities"] = (
            contract.get("required_capabilities")
            or manifest.get("required_capabilities")
            or ["deterministic_execution"]
        )

    elif wrapper_family == "file_io":
        manifest["required_capabilities"] = (
            contract.get("required_capabilities")
            or manifest.get("required_capabilities")
            or ["file_output"]
        )

    elif wrapper_family == "database_query":
        manifest["required_capabilities"] = (
            contract.get("required_capabilities")
            or manifest.get("required_capabilities")
            or ["database_read"]
        )

    elif wrapper_family == "local_command":
        manifest["required_capabilities"] = (
            contract.get("required_capabilities")
            or manifest.get("required_capabilities")
            or ["local_command"]
        )
        manifest["safety_level"] = "high"
        manifest["approval_status"] = manifest.get("approval_status") or "pending_review"

    internal_code, internal_errors = await _author_core_logic_with_model(
        wrapper_family=wrapper_family,
        request=request,
        plan=plan,
        contract=contract,
        manifest=manifest,
        sample_input=sample_input,
        model_notes=model_notes,
        warnings=warnings,
    )

    if wrapper_family == "http_api":
        adapter_code = _external_api_wrapper_code(contract, internal_code, manifest)
        real_run = bool(live_success or request.get("allow_external_network"))

    elif wrapper_family == "managed_helper":
        adapter_code = _managed_helper_wrapper_code(contract, internal_code, manifest)
        real_run = False

    elif wrapper_family == "python_compute":
        adapter_code = _python_compute_wrapper_code(contract, internal_code, manifest)
        real_run = False

    elif wrapper_family == "file_io":
        adapter_code = _file_io_wrapper_code(contract, internal_code, manifest)
        real_run = False

    elif wrapper_family == "database_query":
        adapter_code = _database_query_wrapper_code(contract, internal_code, manifest)
        real_run = False

    elif wrapper_family == "local_command":
        adapter_code = _local_command_wrapper_code(contract, internal_code, manifest)
        real_run = False

    else:
        adapter_code = generate_adapter_code(manifest)
        real_run = False

    validation = validate_tool_manifest(
        manifest,
        adapter_code=adapter_code,
        sample_input=sample_input,
        dynamic=True,
        real_run=real_run,
    )

    static_errors = _author_adapter_static_errors(adapter_code, manifest)
    if static_errors:
        validation["errors"] = sorted(set(validation.get("errors", []) + static_errors))
        validation["success"] = False
        validation["status"] = "failed"

    validation["adapter_contract"] = contract
    validation["contract_registry_validation"] = contract_registry_validation
    validation["internal_code_errors"] = internal_errors

    if wrapper_family == "local_command":
        validation["warnings"] = sorted(
            set(
                validation.get("warnings", [])
                + [
                    "local_command is high risk and requires human approval before real execution",
                    "code_model may only return argv planning logic through execute_task",
                ]
            )
        )

    return {
        "needs_clarification": False,
        "questions": [],
        "tool_kind": plan.get("tool_kind"),
        "operation": plan.get("operation"),
        "manifest": manifest,
        "adapter_code": adapter_code,
        "adapter_code_kind": "wrapper_with_model_internal_code",
        "adapter_edit_policy": "internal_code_only" if wrapper_family != "local_command" else "internal_code_only_until_approved",
        "model_internal_code": internal_code,
        "sample_input": sample_input,
        "validation": validation,
        "snippet": None,
        "model_notes": model_notes,
        "warnings": warnings,
        "requires_human_confirmation": True,
    }

def _fallback_snippet(manifest: dict[str, Any], sample_input: dict[str, Any]) -> dict[str, Any]:
    cap = _capability_from_dict(manifest)
    fn = cap.functions[0]

    input_schema = fn.input_schema or cap.input_schema or {}
    output_schema = fn.output_schema or cap.output_schema or {}

    props = input_schema.get("properties") if isinstance(input_schema, dict) else {}
    required = input_schema.get("required") if isinstance(input_schema, dict) else []
    props = props if isinstance(props, dict) else {}
    required = [str(item) for item in required or [] if isinstance(item, str)]

    sample = sample_input if isinstance(sample_input, dict) else {}
    payload: dict[str, Any] = {}

    # 先按 input_schema.properties 构造示例，避免 snippet 里出现 q 但 schema 要 query。
    for name, spec in props.items():
        if name in sample:
            payload[name] = sample[name]
            continue

        if isinstance(spec, dict) and "default" in spec:
            payload[name] = spec["default"]
            continue

        # 如果 schema 需要字段但 sample 没有，就给一个类型安全占位。
        typ = str((spec or {}).get("type") or "string") if isinstance(spec, dict) else "string"
        if typ in {"integer", "number"}:
            payload[name] = 10
        elif typ == "boolean":
            payload[name] = True
        elif typ == "array":
            payload[name] = []
        elif typ == "object":
            payload[name] = {}
        else:
            payload[name] = "demo"

    # 如果 schema 没有 properties，就退回 sample_input。
    if not payload:
        payload = dict(sample)

    # required 字段必须存在。
    for name in required:
        payload.setdefault(name, "demo")

    is_external_api = (
        str(manifest.get("category") or "").lower() == "external_api"
        or str(manifest.get("type") or "").lower() == "external_api"
        or manifest.get("needs_external_network") is True
    )

    failure_layers = ["helper_call_failed", "final_platform_output_value_invalid"]
    anti_patterns = [
        "Do not pass API keys or secrets in payload.",
        "Do not guess parameter names; follow expected_input_shape.",
        "Do not expect provider raw response fields as top-level output unless expected_output_shape declares them.",
    ]
    return_rule = "Return the adapter result directly. It should match expected_output_shape."

    if not is_external_api and manifest.get("generates_file"):
        failure_layers.append("artifact_missing")
        anti_patterns.append("Do not write or expect files outside OUTPUT_DIR.")
        return_rule = "Return the adapter result directly. If it contains generated file paths, pass those exact OUTPUT_DIR paths to downstream skill steps."

    return {
        "id": f"{cap.name}.minimal_usage",
        "title": f"Use {cap.display_name}",
        "kind": "minimal_usage",
        "applies_to": {
            "roles": cap.allowed_roles or cap.roles,
            "capabilities": cap.required_capabilities or [cap.name],
            "failure_layers": failure_layers,
        },
        "description": fn.when_to_use or fn.short_description,
        "code": (
            f"from {fn.import_path} import {fn.function_name}\n\n"
            f"payload = {json.dumps(payload, ensure_ascii=False, indent=2)}\n"
            f"result = {fn.function_name}(payload)\n"
            f"return result"
        ),
        "expected_input_shape": input_schema,
        "expected_output_shape": output_schema,
        "return_rule": return_rule,
        "anti_patterns": anti_patterns,
        "requires": cap.required_capabilities or [cap.name],
        "usage_policy": cap.usage_policy,
        "priority": 80,
    }


_ENV_REF_RE = re.compile(r"\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)\}")
_INPUT_REF_RE = re.compile(r"\$\{(?:input|payload)\.([A-Za-z0-9_.-]+)\}")
_MUSTACHE_REF_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}")


def _extract_env_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, str):
        refs.update(_ENV_REF_RE.findall(value))
    elif isinstance(value, dict):
        for item in value.values():
            refs.update(_extract_env_refs(item))
    elif isinstance(value, list):
        for item in value:
            refs.update(_extract_env_refs(item))
    return refs


def _redact_secrets(value: Any, secret_values: set[str] | None = None) -> Any:
    secret_values = {item for item in (secret_values or set()) if item}
    if isinstance(value, str):
        redacted = value
        for secret in secret_values:
            redacted = redacted.replace(secret, "***")
        if _ENV_REF_RE.search(redacted):
            return _ENV_REF_RE.sub("${ENV:***}", redacted)
        return redacted
    if isinstance(value, dict):
        return {key: _redact_secrets(item, secret_values) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_secrets(item, secret_values) for item in value]
    return value


def _get_by_path(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return ""
    return current


def _render_template(value: Any, sample_input: dict[str, Any], secret_values: dict[str, str]) -> Any:
    if isinstance(value, str):
        def env_replace(match: re.Match[str]) -> str:
            name = match.group(1)
            return secret_values.get(name, "")

        def input_replace(match: re.Match[str]) -> str:
            resolved = _get_by_path(sample_input, match.group(1))
            return str(resolved if resolved is not None else "")

        rendered = _ENV_REF_RE.sub(env_replace, value)
        rendered = _INPUT_REF_RE.sub(input_replace, rendered)
        rendered = _MUSTACHE_REF_RE.sub(input_replace, rendered)
        return rendered

    if isinstance(value, dict):
        return {key: _render_template(item, sample_input, secret_values) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_render_template(item, sample_input, secret_values) for item in value]
    return value


def _normalized_preview(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        keys = sorted(str(key) for key in data.keys())
        return {"type": "object", "keys": keys[:50], "preview": {key: data[key] for key in list(data.keys())[:10]}}
    if isinstance(data, list):
        return {"type": "array", "length": len(data), "first_item": data[0] if data else None}
    return {"type": type(data).__name__, "value": data}


def _auth_env_refs(config: dict[str, Any]) -> set[str]:
    refs = _extract_env_refs(config)
    auth = config.get("auth") if isinstance(config.get("auth"), dict) else {}
    for key in ("env", "username_env", "password_env"):
        value = str(auth.get(key) or "").strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            refs.add(value)
    secret_env = str(config.get("secret_env") or "").strip()
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", secret_env):
        refs.add(secret_env)
    return refs


def _auth_config_for_live_test(config: dict[str, Any]) -> dict[str, Any]:
    auth = dict(config.get("auth") or {}) if isinstance(config.get("auth"), dict) else {}

    auth_type = str(
        auth.get("type")
        or config.get("auth_type")
        or config.get("authentication")
        or "none"
    ).strip().lower()

    auth_type = (
        "token" if auth_type in {"token", "bearer"}
        else "api_key" if auth_type in {"api_key", "key", "header"}
        else auth_type
    )

    if auth_type in {"", "none", "no_auth", "anonymous"}:
        return {}

    # env_name 是变量名，不要解析成 secret 值。
    env_name = str(
        auth.get("env")
        or config.get("secret_env")
        or config.get("api_key_env")
        or config.get("token_env")
        or config.get("password_env")
        or ""
    ).strip()

    placement = _resolve_env_ref_or_value(
        auth.get("placement") or config.get("auth_placement") or "",
        "",
    ).strip().lower()

    if auth_type in {"token", "api_key", "basic"}:
        placement = placement or "header"

    normalized = {**auth, "type": auth_type, "env": env_name, "placement": placement}

    if auth_type == "token":
        normalized["header_name"] = _resolve_env_ref_or_value(
            auth.get("header_name") or config.get("auth_header_name") or "Authorization",
            "Authorization",
        ).strip() or "Authorization"
        normalized["scheme"] = _resolve_env_ref_or_value(
            auth.get("scheme") or config.get("auth_scheme") or "Bearer",
            "Bearer",
        ).strip() or "Bearer"

    elif auth_type == "api_key":
        if placement == "query":
            normalized["query_param"] = _resolve_env_ref_or_value(
                auth.get("query_param") or config.get("auth_query_param") or "api_key",
                "api_key",
            ).strip() or "api_key"
        elif placement == "bearer":
            normalized["header_name"] = "Authorization"
            normalized["scheme"] = "Bearer"
        else:
            normalized["placement"] = "header"
            normalized["header_name"] = _resolve_env_ref_or_value(
                auth.get("header_name") or config.get("auth_header_name") or "X-API-KEY",
                "X-API-KEY",
            ).strip() or "X-API-KEY"

    elif auth_type == "basic":
        normalized["placement"] = "header"
        normalized["header_name"] = "Authorization"
        normalized["scheme"] = "Basic"

    return normalized


def _apply_auth_to_request(headers: dict[str, Any], query: dict[str, Any], config: dict[str, Any], secret_values: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any], list[str], bool]:
    auth = _auth_config_for_live_test(config)
    if not auth:
        return headers, query, [], False
    env_name = str(auth.get("env") or "").strip()
    secret = secret_values.get(env_name, "")
    warnings: list[str] = []
    if not env_name or not secret:
        return headers, query, warnings, False
    auth_type = str(auth.get("type") or "").lower()
    placement = str(auth.get("placement") or "").lower()
    if auth_type == "api_key" and placement == "query":
        query[str(auth.get("query_param") or "api_key")] = secret
        return headers, query, warnings, True
    if auth_type == "api_key":
        if placement == "bearer":
            headers["Authorization"] = f"Bearer {secret}"
        else:
            header_name = str(auth.get("header_name") or "").strip()
            if not header_name:
                warnings.append("已保存密钥，但当前请求模板没有使用该密钥，请配置 Header 名称或认证方式。")
                return headers, query, warnings, False
            headers[header_name] = secret
        return headers, query, warnings, True
    if auth_type == "token":
        header_name = str(auth.get("header_name") or "Authorization").strip() or "Authorization"
        scheme = str(auth.get("scheme") or "Bearer").strip()
        headers[header_name] = f"{scheme} {secret}".strip()
        return headers, query, warnings, True
    if auth_type == "basic":
        encoded = base64.b64encode(secret.encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {encoded}"
        return headers, query, warnings, True
    if auth_type == "custom":
        return headers, query, warnings, bool(headers)
    return headers, query, warnings, False


def live_test_tool(request: dict[str, Any]) -> dict[str, Any]:
    """Perform one generic external API probe without generating or registering code."""
    _load_tool_authoring_config_store_from_disk()

    if request.get("allow_external_network") is not True:
        return {
            "success": False,
            "status": "blocked",
            "errors": ["allow_external_network must be true for live_test"],
            "preview": None,
            "normalized_preview": None,
        }

    config = request.get("config") if isinstance(request.get("config"), dict) else {}
    sample_input = request.get("sample_input") if isinstance(request.get("sample_input"), dict) else {}

    env_refs = _auth_env_refs(config)
    missing_env = sorted(name for name in env_refs if not os.environ.get(name))
    if missing_env:
        auth = _auth_config_for_live_test(config)
        expected_header = str(auth.get("header_name") or "").strip() if auth and auth.get("placement") != "query" else ""
        raw_url = str(config.get("url") or config.get("endpoint") or config.get("base_url") or "")
        return {
            "success": False,
            "status": "missing_secret",
            "errors": ["missing required env secret(s): " + ", ".join(missing_env)],
            "missing_env": missing_env,
            "preview": None,
            "normalized_preview": None,
            "request_preview": {
                "method": str(config.get("method") or "GET").upper(),
                "url": _redact_secrets(raw_url),
                "header_keys": [expected_header] if expected_header else [],
                "has_body": False,
                "body_preview": None,
                "query_preview": {},
                "auth_applied": False,
            },
        }

    secret_values = {name: os.environ.get(name, "") for name in env_refs}

    method = _resolve_env_ref_or_value(config.get("method"), "GET").upper()
    url = str(config.get("url") or config.get("endpoint") or "")
    if not url:
        base = str(config.get("base_url") or "").rstrip("/")
        path = str(config.get("path") or "").lstrip("/")
        url = f"{base}/{path}" if base and path else base

    url = _render_template(url, sample_input, secret_values)
    if not url.startswith(("http://", "https://")):
        return {
            "success": False,
            "status": "invalid_config",
            "errors": ["live_test url must start with http:// or https://"],
            "preview": None,
            "normalized_preview": None,
        }

    headers_template = config.get("headers_template") or config.get("headers") or {}
    if isinstance(headers_template, str):
        ref = _env_ref_name(headers_template)
        headers_template = _load_json_env(ref, {}) if ref else {}
    if not headers_template and config.get("headers_template_env"):
        headers_template = _load_json_env(str(config.get("headers_template_env")), {})

    query_template = config.get("query_template") or config.get("params_template") or {}
    if isinstance(query_template, str):
        ref = _env_ref_name(query_template)
        query_template = _load_json_env(ref, {}) if ref else {}
    if not query_template and config.get("query_template_env"):
        query_template = _load_json_env(str(config.get("query_template_env")), {})

    body_template = config.get("json_body_template") if "json_body_template" in config else config.get("body_template",
                                                                                                       {})
    if isinstance(body_template, str):
        ref = _env_ref_name(body_template)
        body_template = _load_json_env(ref, {}) if ref else {}
    if not body_template and config.get("json_body_template_env"):
        body_template = _load_json_env(str(config.get("json_body_template_env")), {})

    headers = _render_template(headers_template or {}, sample_input, secret_values)
    query = _render_template(query_template or {}, sample_input, secret_values)

    headers = dict(headers or {}) if isinstance(headers, dict) else {}
    query = dict(query or {}) if isinstance(query, dict) else {}

    headers, query, auth_warnings, auth_applied = _apply_auth_to_request(headers, query, config, secret_values)

    auth_warning = "已保存密钥，但当前请求模板没有使用该密钥，请配置 Header 名称或认证方式。" if _auth_config_for_live_test(
        config) and not auth_applied else ""
    if auth_warning and auth_warning not in auth_warnings:
        auth_warnings.append(auth_warning)

    json_body = _render_template(body_template or {}, sample_input, secret_values)
    json_body = dict(json_body or {}) if isinstance(json_body, dict) else json_body

    if isinstance(query, dict) and query:
        parsed = urllib.parse.urlsplit(url)
        merged_query = urllib.parse.urlencode({**dict(urllib.parse.parse_qsl(parsed.query)), **query}, doseq=True)
        url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, merged_query, parsed.fragment))

    data: bytes | None = None
    if method not in {"GET", "HEAD"}:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers = {**headers, "Content-Type": headers.get("Content-Type") or headers.get("content-type") or "application/json"}

    request_preview = {
        "method": method,
        "url": _redact_secrets(url, set(secret_values.values())),
        "header_keys": sorted((headers or {}).keys()),
        "has_body": data is not None,
        "body_preview": _redact_secrets(json_body, set(secret_values.values())) if data is not None else None,
        "query_preview": _redact_secrets(query, set(secret_values.values())),
        "auth_applied": auth_applied,
    }

    timeout = float(config.get("timeout_seconds") or os.environ.get("TOOL_AUTHOR_LIVE_TEST_TIMEOUT_SECONDS", "20"))
    req = urllib.request.Request(url=url, data=data, method=method, headers={str(k): str(v) for k, v in (headers or {}).items()})

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(int(config.get("max_preview_bytes") or 65536))
            status_code = int(resp.status)
            content_type = resp.headers.get("content-type", "")
    except urllib.error.HTTPError as exc:
        raw = exc.read(65536)
        status_code = int(exc.code)
        content_type = exc.headers.get("content-type", "") if exc.headers else ""
    except Exception as exc:
        return {
            "success": False,
            "status": "request_failed",
            "errors": [str(exc), *auth_warnings],
            "preview": None,
            "normalized_preview": None,
            "request_preview": request_preview,
        }

    text = raw.decode("utf-8", errors="replace")
    try:
        preview: Any = json.loads(text)
    except Exception:
        preview = text[:4000]

    redacted_preview = _redact_secrets(preview, set(secret_values.values()))
    success = 200 <= status_code < 400

    return {
        "success": success,
        "status": "ok" if success else "http_error",
        "status_code": status_code,
        "content_type": content_type,
        "preview": redacted_preview,
        "normalized_preview": _normalized_preview(redacted_preview),
        "request_preview": request_preview,
        "errors": ([] if success else [f"HTTP {status_code}"]) + auth_warnings,
    }
AUTHORING_HELPER_NAMES = {
    "authoring_config_collector",
    "authoring_schema_infer",
    "authoring_live_test",
    "authoring_dependency_check",
    "authoring_code_protocol_check",
    "authoring_file_output_check",
}


def _secret_env_name_from_key(key: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", key or "TOOL_SECRET").strip("_").upper()
    if not cleaned:
        cleaned = "TOOL_SECRET"
    if not any(token in cleaned for token in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
        cleaned += "_SECRET"
    return cleaned[:80]


def _sanitize_authoring_config(value: Any, *, parent_key: str = "") -> tuple[Any, set[str]]:
    env_refs: set[str] = set()
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            child, child_refs = _sanitize_authoring_config(item, parent_key=str(key))
            env_refs.update(child_refs)
            sanitized[key] = child
        return sanitized, env_refs
    if isinstance(value, list):
        items = []
        for item in value:
            child, child_refs = _sanitize_authoring_config(item, parent_key=parent_key)
            env_refs.update(child_refs)
            items.append(child)
        return items, env_refs
    if isinstance(value, str):
        refs = _extract_env_refs(value)
        env_refs.update(refs)
        lowered_key = (parent_key or "").lower()
        is_env_reference_field = lowered_key.endswith("_env") or lowered_key in {"secret_env", "secret_env_name", "api_key_env", "token_env", "username_env", "password_env"}
        looks_secret_key = any(token in lowered_key for token in ("api_key", "apikey", "token", "password", "secret", "authorization"))
        if is_env_reference_field:
            return value, env_refs | ({value} if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) else set())
        if looks_secret_key and value and not refs:
            env_name = _secret_env_name_from_key(parent_key)
            env_refs.add(env_name)
            if "authorization" in lowered_key and value.lower().startswith("bearer "):
                return f"Bearer ${{ENV:{env_name}}}", env_refs
            return f"${{ENV:{env_name}}}", env_refs
        return value, env_refs
    return value, env_refs


def _authoring_helper_result_context(context: dict[str, Any], tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    updated = dict(context or {})
    results = dict(updated.get("tool_results") or {})
    # Store only sanitized helper outputs. Secret values are redacted/replaced before this point.
    results[tool_name] = result
    updated["tool_results"] = results
    if result.get("config"):
        updated["config"] = result["config"]
    if result.get("sample_input"):
        updated["sample_input"] = result["sample_input"]
    if result.get("schemas"):
        updated["schemas"] = result["schemas"]
    if result.get("live_test_result"):
        updated["live_test_result"] = result["live_test_result"]
    return updated


def _config_collector_schema(missing_fields: list[str] | None = None) -> dict[str, Any]:
    schema = _external_api_config_schema()
    schema["properties"] = {
        **schema.get("properties", {}),
        "ip": {"type": "string"},
        "port": {"type": "string"},
        "api_key_env": {"type": "string", "description": "Environment variable name only; do not enter the secret value."},
        "token_env": {"type": "string", "description": "Environment variable name only; do not enter the token value."},
        "username_env": {"type": "string", "description": "Optional username env var name."},
        "password_env": {"type": "string", "description": "Optional password env var name."},
        "sample_input": {"type": "object"},
    }
    if missing_fields:
        schema["missing_fields"] = missing_fields
    return schema


def run_authoring_helper(tool_name: str, input: dict[str, Any] | None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one internal Tool Authoring helper and write its output into authoring context."""
    normalized_name = _slug(tool_name)
    cap = get_tool_capability(normalized_name)
    if cap is None or cap.tool_type != "internal_authoring_tool" or normalized_name not in AUTHORING_HELPER_NAMES:
        raise ValueError(f"authoring helper is not allowed: {tool_name}")
    payload = dict(input or {})
    ctx = dict(context or {})
    result: dict[str, Any]
    if normalized_name == "authoring_config_collector":
        config = payload.get("config") if isinstance(payload.get("config"), dict) else ctx.get("config") if isinstance(ctx.get("config"), dict) else {}
        sample_input = payload.get("sample_input") if isinstance(payload.get("sample_input"), dict) else ctx.get("sample_input") if isinstance(ctx.get("sample_input"), dict) else {}
        required_fields = [str(item) for item in payload.get("config_required_fields") or payload.get("missing_fields") or []]
        if required_fields:
            result = {"success": False, "requires_input": True, "schema": _config_collector_schema(required_fields), "config_required_fields": required_fields, "message": "等待用户在授权弹窗保存连接配置。"}
        else:
            saved = save_tool_authoring_config({**ctx, **payload, "config": config, "sample_input": sample_input})
            result = {"success": True, "requires_input": False, "config": saved.get("config") or {}, "sample_input": sample_input, "configured_env": saved.get("configured_env") or [], "configured_secrets": saved.get("configured_secrets") or [], "config_refs": saved.get("config_refs") or {}, "secret_env_suggestions": saved.get("configured_secrets") or [], "message": "配置已保存为 env/secret 引用。"}
    elif normalized_name == "authoring_schema_infer":
        code = str(payload.get("code_block") or ctx.get("code_block") or "")
        input_schema, output_schema, notes = _infer_schema_from_code(code) if code.strip() else ({}, {}, [])
        sample_input = payload.get("sample_input") if isinstance(payload.get("sample_input"), dict) else ctx.get("sample_input") if isinstance(ctx.get("sample_input"), dict) else {}
        if sample_input and not input_schema:
            input_schema = {key: {"type": type(value).__name__, "required": False, "description": "Inferred from sample_input."} for key, value in sample_input.items()}
        expected = payload.get("expected_output_fields") or ((ctx.get("config") or {}).get("expected_output_fields") if isinstance(ctx.get("config"), dict) else [])
        if expected and not output_schema:
            output_schema = {str(key): {"type": "object", "description": "Expected output field confirmed during authoring."} for key in expected}
        result = {"success": True, "schemas": {"input_schema": input_schema, "output_schema": output_schema}, "notes": notes}
    elif normalized_name == "authoring_live_test":
        if payload.get("allow_external_network") is not True and ctx.get("allow_external_network") is not True:
            result = {"success": False, "requires_input": True, "schema": {"type": "object", "required": ["allow_external_network"], "properties": {"allow_external_network": {"type": "boolean", "const": True}}}, "message": "live_test 需要用户确认允许外部网络。"}
        else:
            live_request = {**ctx, **payload, "allow_external_network": True}
            result = {"success": True, "requires_input": False, "live_test_result": live_test_tool(live_request)}
            result["success"] = bool(result["live_test_result"].get("success"))
    elif normalized_name == "authoring_dependency_check":
        dependencies = [str(item) for item in payload.get("dependencies") or ctx.get("dependencies") or []]
        missing = [dep for dep in dependencies if not _dependency_available(dep)]
        result = {"success": not missing, "dependencies": dependencies, "missing_dependencies": missing}
    elif normalized_name == "authoring_code_protocol_check":
        manifest = payload.get("manifest") if isinstance(payload.get("manifest"), dict) else ctx.get("manifest") if isinstance(ctx.get("manifest"), dict) else {}
        adapter_code = str(payload.get("adapter_code") or ctx.get("adapter_code") or "")
        errors = _author_adapter_static_errors(adapter_code, manifest) if adapter_code and manifest else ["adapter_code and manifest are required"]
        result = {"success": not errors, "errors": errors}
    elif normalized_name == "authoring_file_output_check":
        manifest = payload.get("manifest") if isinstance(payload.get("manifest"), dict) else ctx.get("manifest") if isinstance(ctx.get("manifest"), dict) else {}
        output_schema = ((manifest.get("functions") or [{}])[0].get("output_schema") if isinstance(manifest, dict) else {}) or {}
        has_file_contract = any(key in output_schema for key in ("file_paths", "file_outputs", "path", "output_path"))
        result = {"success": has_file_contract, "has_file_contract": has_file_contract, "warnings": [] if has_file_contract else ["file output tools should declare file_paths/file_outputs or path/output_path"]}
    else:
        raise ValueError(f"unknown authoring helper: {tool_name}")
    return {"tool_name": normalized_name, **result, "authoring_context": _authoring_helper_result_context(ctx, normalized_name, result)}


def _run_authoring_tool_plan(plan: dict[str, Any], request: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    context = dict(request.get("authoring_context") or plan.get("authoring_context") or {})
    context.update({
        "config": request.get("config") or context.get("config") or {},
        "sample_input": request.get("sample_input") or context.get("sample_input") or {},
        "allow_external_network": request.get("allow_external_network") is True,
        "manifest": request.get("manifest") or plan.get("manifest") or context.get("manifest") or {},
        "code_block": request.get("code_block") or context.get("code_block") or "",
    })
    results: list[dict[str, Any]] = []
    for item in plan.get("authoring_tool_plan") or []:
        if not isinstance(item, dict):
            continue
        tool_name = str(item.get("tool_name") or "")
        helper_input = item.get("input") if isinstance(item.get("input"), dict) else {}
        result = run_authoring_helper(tool_name, helper_input, context)
        results.append(result)
        context = result.get("authoring_context") or context
        if result.get("requires_input"):
            break
    updated = {**plan, "authoring_context": context, "authoring_tool_results": results, "ready_for_code_generation": False}
    return updated, results


def _author_response_from_plan(
    plan: dict[str, Any],
    *,
    model_notes: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    plan = dict(plan or {})

    needs_clarification = bool(plan.get("needs_clarification"))
    status = "needs_clarification" if needs_clarification else "waiting_for_user_input"

    questions = plan.get("clarification_questions") or plan.get("questions") or []
    auth_override = plan.get("auth_override") if isinstance(plan.get("auth_override"), dict) else {}
    auth_decision = plan.get("auth_decision") if isinstance(plan.get("auth_decision"), dict) else {}
    auth_gate = plan.get("auth_gate") if isinstance(plan.get("auth_gate"), dict) else {}
    runtime_facts = plan.get("runtime_facts") if isinstance(plan.get("runtime_facts"), dict) else {}

    manifest = dict(plan.get("manifest") or {})
    if auth_decision:
        manifest["auth_decision"] = auth_decision
    if auth_gate:
        manifest["auth_gate"] = auth_gate
    if auth_override:
        manifest["auth_override"] = auth_override

    validation_warnings: list[str] = []
    for item in plan.get("risk_notes") or []:
        if item:
            validation_warnings.append(str(item))
    for item in warnings or []:
        if item:
            validation_warnings.append(str(item))

    return {
        "needs_clarification": needs_clarification,
        "questions": questions,
        "clarification_questions": questions,
        "requires_config": bool(plan.get("requires_config")),
        "config_required_fields": plan.get("config_required_fields") or [],
        "config_form_schema": plan.get("config_form_schema") or plan.get("suggested_config_schema") or {},
        "suggested_entrypoint": plan.get("suggested_entrypoint") or {},
        "additional_fields_schema": plan.get("additional_fields_schema") or [],
        "requires_authorization": bool(plan.get("requires_authorization")),
        "tool_kind": plan.get("tool_kind") or "unknown",
        "operation": plan.get("operation") or "",
        "resolved_clarifications": plan.get("resolved_clarifications") or [],
        "requires_secret": bool(plan.get("requires_secret")),
        "secret_env_suggestions": plan.get("secret_env_suggestions") or [],
        "requires_external_network": bool(plan.get("requires_external_network")),
        "requires_live_test": bool(plan.get("requires_live_test")),
        "ready_for_live_test": bool(plan.get("ready_for_live_test")),
        "ready_for_code_generation": bool(plan.get("ready_for_code_generation")),
        "requires_authoring_tools": bool(plan.get("requires_authoring_tools")),
        "authoring_tool_plan": plan.get("authoring_tool_plan") or [],
        "authoring_context": plan.get("authoring_context") or {},
        "authoring_tool_results": plan.get("authoring_tool_results") or [],
        "missing_fields": plan.get("missing_fields") or [],
        "suggested_config_schema": plan.get("suggested_config_schema") or {},
        "sample_input_schema": plan.get("sample_input_schema") or {},
        "manifest": manifest,
        "adapter_code": "",
        "sample_input": plan.get("sample_input") or {},
        "validation": {
            "success": False,
            "status": status,
            "errors": [],
            "warnings": sorted(set(validation_warnings)),
        },
        "snippet": None,
        "model_notes": model_notes,
        "warnings": warnings,
        "requires_human_confirmation": True,
        "wrapper_family": plan.get("wrapper_family") or "auto",
        "implementation_plan": plan.get("implementation_plan") or "",
        "auth_decision": auth_decision,
        "auth_gate": auth_gate,
        "auth_override": auth_override,
        "runtime_facts": runtime_facts,
    }

async def _run_capability_ambiguity_judge(request: dict[str, Any], model_notes: list[str], warnings: list[str]) -> list[str]:
    """Ask planner_model to catch capability ambiguity that deterministic heuristics may miss."""
    if _clarification_answer_texts(request):
        return []
    judge_payload = {
        key: request.get(key)
        for key in [
            "description",
            "operation",
            "input_description",
            "output_description",
            "tool_kind",
            "needs_external_network",
            "clarification_answers",
        ]
    }
    judge_messages = [
        {
            "role": "system",
            "content": (
                "You are the planner_model capability ambiguity judge for Tool Authoring. "
                "Return strict JSON: {needs_capability_clarification: boolean, question: {id, type, question, options, required}}. "
                "Set needs_capability_clarification=true only when the user's desired business capability or operation is genuinely unclear. "
                "If clarification_answers already contain a non-empty answer that resolves the requested operation, return needs_capability_clarification=false. Do not ask the same question again. "
                "The question must be Chinese, short, structured, and ask only what the tool should do. If you provide options, generate context-specific options for this user request; do not use generic fixed options. "
                "Do not ask for service address, endpoint, IP, key, token, auth method, connection-test permission, method, headers/body/query templates, schemas, sample input, or expected output fields."
            ),
        },
        {"role": "user", "content": json.dumps(judge_payload, ensure_ascii=False)},
    ]
    judge, ack, err = await _complete_author_model("planner", judge_messages, reason="creator_tool_author_capability_ambiguity")
    if ack:
        model_notes.append(f"capability_ambiguity_judge={ack['model']}")
    if err:
        warnings.append(f"capability ambiguity judge unavailable, used deterministic ambiguity heuristic only: {err}")
        return []
    if not bool(judge.get("needs_capability_clarification") or judge.get("capability_ambiguous")):
        return []
    question = judge.get("question") or judge.get("clarification_question") or {"id": "operation_detail", "type": "short_text", "question": "请用一句话补充这个工具要完成的具体能力。", "required": True}
    return _safe_clarification_questions([question], [])

async def _run_planner(
    request: dict[str, Any],
    model_notes: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    request = _apply_clarification_answers(request)

    planner_payload = {
        key: request.get(key)
        for key in [
            "description",
            "tool_name",
            "tool_type",
            "wrapper_family",
            "code_block",
            "input_description",
            "output_description",
            "manifest",
            "sample_input",
            "allowed_roles",
            "needs_secret",
            "needs_external_network",
            "generates_file",
            "high_risk",
            "clarification_answers",
            "resolved_clarifications",
            "tool_kind",
            "operation",
            "config",
            "live_test_result",
            "allow_external_network",
            "authoring_context",
            "auth_decision",
            "auth_override",
        ]
    }

    planner_payload["registry"] = _contract_registry_summary()

    planner_messages = [
        {
            "role": "system",
            "content": (
                "You are planner_model for a generic Tool Authoring system. Return strict JSON only. "
                "Create a draft ToolContract. Do not generate code. "
                "Do not invent provider-specific registry entries. "

                "The system has a finite wrapper registry. Choose wrapper_family only from registry.wrapper_registry. "
                "Choose required_capabilities only from registry.capability_registry. "
                "wrapper_family describes runtime packaging only; it must not decide authentication. "

                "tool_kind is open-ended business semantics for humans and later semantic validation. "
                "Do not rely on tool_kind to select implementation. "
                "Do not use examples or hard-coded business cases to select wrappers. "
                "Implementation must be represented only by wrapper_family + required_capabilities + optional helper_contract. "

                "For managed_helper, helper_contract.helper_name must be one of helper_imports exposed by the selected capabilities. "
                "For http_api, use a capability whose registry.allowed_wrapper is http_api. "
                "For pure local deterministic computation, use a capability whose registry.allowed_wrapper is python_compute. "
                "For file-producing tools such as charts, reports, generated images, transformed files, choose file_io or managed_helper according to capability. "

                "Authentication is independent from wrapper_family/tool_kind/network usage. "
                "Represent auth only through auth_decision, security_schemes, required_secrets, config.auth_type, or config.secret_env. "
                "Do not infer authentication from provider names, field names, network access, or wrapper_family. "

                "Every schema property should include x-scope when possible: "
                "runtime_input for normal payload fields; connection_config for stable service connection fields; "
                "auth_config for auth metadata; secret_ref for env var references. "
                "config_form_schema may contain only connection_config/auth_config/secret_ref fields. "
                "manifest.input_schema must contain runtime_input fields. "
                "If unsure about a field scope, put it in manifest.input_schema, not config_form_schema. "

                "requires_config should be true only when connection/auth/secret configuration is missing. "
                "Do not set requires_config=true for runtime inputs such as target_url, content_selector, query, prompt, timeout, parser, user_agent, headers, payload. "

                "If third-party Python packages are needed, declare them in manifest.dependencies as objects, not as backend hard-coded mappings. "
                "Each dependency object must have this shape: "
                "{\"package\": \"pip-package-name\", \"imports\": [\"python_import_name\"], \"version\": \"optional-version-spec\"}. "
                "Examples: "
                "{\"package\": \"scikit-learn\", \"imports\": [\"sklearn\"]}; "
                "{\"package\": \"matplotlib\", \"imports\": [\"matplotlib\"]}; "
                "{\"package\": \"beautifulsoup4\", \"imports\": [\"bs4\"]}. "
                "Also add the same import names to manifest.code_policy.allowed_imports. "
                "Do not rely on backend package/import-name vocabularies. "
                "Do not ask the user to install dependencies manually during planning; declare them in manifest.dependencies. "

                "For ML tools such as SVM, logistic regression, k-means, decision tree or random forest, do not create a new wrapper. "
                "Use python_compute with deterministic_execution, declare dependencies such as scikit-learn/numpy when needed, "
                "and let code_model implement execute_task(payload, context). "

                "For plotting/chart tools such as scatter plot, line chart or histogram, do not create a new wrapper. "
                "Use file_io with file_output, declare dependencies such as matplotlib/numpy when needed, "
                "and let code_model implement execute_task(payload, context) using context['safe_output_path']. "

                "For HTTP/API search tools, do not create a provider-specific wrapper. "
                "Use http_api with http_request, and let code_model implement execute_task(payload, context) using context['http_request']. "

                "For document/PDF/image helpers already exposed by capabilities, prefer managed_helper and helper_contract. "
                "Do not generate backend templates for a specific business example. "

                "clarification_questions ask only about real business capability ambiguity. "
                "Do not ask for endpoint, auth method, token, secret, headers/body/query templates, schemas, sample input, or live-test permission as clarification questions."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(planner_payload, ensure_ascii=False),
        },
    ]

    model_plan, ack, err = await _complete_author_model(
        "planner",
        planner_messages,
        reason="creator_tool_author_plan",
    )

    if ack:
        model_notes.append(f"planner_model={ack['model']}")

    if err:
        warnings.append(f"planner_model unavailable, used deterministic fallback: {err}")

    plan = model_plan if isinstance(model_plan, dict) and model_plan else _author_fallback_plan(request)

    if not isinstance(plan.get("manifest"), dict):
        plan = _author_fallback_plan(request)

    normalized = _normalize_author_plan(plan, request)

    action = str(request.get("action") or "").strip().lower()
    live_success = _live_test_success_from_request(request)

    if (
        action not in {"generate", "finalize"}
        and not live_success
        and ack
        and normalized.get("tool_kind") == "external_api"
        and not normalized.get("clarification_questions")
    ):
        judged_questions = await _run_capability_ambiguity_judge(
            request,
            model_notes,
            warnings,
        )

        if judged_questions:
            normalized["clarification_questions"] = judged_questions
            normalized["questions"] = judged_questions
            normalized["needs_clarification"] = True
            normalized["ready_for_code_generation"] = False

    model_notes.extend(normalized.get("model_notes") or [])

    return normalized

def _dependency_packages_for_install(manifest: dict[str, Any] | None) -> list[str]:
    packages: list[str] = []
    for record in _manifest_dependency_records(manifest):
        package = str(record.get("package") or "").strip()
        version = str(record.get("version") or "").strip()
        if not package:
            continue
        spec = f"{package}{version}" if version and version.startswith(("==", ">=", "<=", "~=", ">", "<")) else package
        if spec not in packages:
            packages.append(spec)
    return packages


def _install_dependencies_to_target(
    packages: list[str],
    target_dir: Path,
    *,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    if not packages:
        return {
            "success": True,
            "installed": [],
            "target_dir": str(target_dir),
            "errors": [],
        }

    target_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--target",
        str(target_dir),
        *packages,
    ]

    try:
        completed = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except Exception as exc:
        return {
            "success": False,
            "installed": [],
            "target_dir": str(target_dir),
            "errors": [str(exc)],
        }

    if completed.returncode != 0:
        return {
            "success": False,
            "installed": [],
            "target_dir": str(target_dir),
            "errors": [
                completed.stderr[-4000:] or completed.stdout[-4000:] or f"pip exited {completed.returncode}"
            ],
        }

    return {
        "success": True,
        "installed": packages,
        "target_dir": str(target_dir),
        "errors": [],
    }

async def author_tool(request: dict[str, Any]) -> dict[str, Any]:
    """Generic Tool Authoring pipeline driven by explicit actions."""
    _load_tool_authoring_config_store_from_disk()

    stage = str(request.get("stage") or "").strip().lower()
    action = str(request.get("action") or "clarify").strip().lower()

    if stage == "finalize":
        action = "finalize"
    elif stage == "draft" and action == "clarify":
        action = "generate"

    model_notes: list[str] = []
    warnings: list[str] = []

    if action not in {"clarify", "configure", "live_test", "generate", "finalize", "revise"}:
        raise ValueError("action must be one of clarify/configure/live_test/generate/finalize/revise")

    if action == "revise":
        return await _revise_adapter_and_snippet_with_feedback(
            request,
            model_notes=model_notes,
            warnings=warnings,
        )

    if action == "live_test":
        result = live_test_tool(request)
        return {
            "needs_clarification": False,
            "questions": [],
            "tool_kind": request.get("tool_kind") or "external_api",
            "operation": request.get("operation") or "",
            "live_test_result": result,
            "preview": result.get("preview"),
            "normalized_preview": result.get("normalized_preview"),
            "manifest": request.get("manifest") or {},
            "adapter_code": "",
            "sample_input": request.get("sample_input") or {},
            "validation": {
                "success": bool(result.get("success")),
                "status": "live_test",
                "errors": result.get("errors") or [],
                "warnings": [],
            },
            "snippet": None,
            "model_notes": model_notes,
            "warnings": warnings,
            "requires_human_confirmation": True,
        }

    if action == "finalize":
        manifest = request.get("manifest") if isinstance(request.get("manifest"), dict) else {}
        adapter_code = str(request.get("adapter_code") or request.get("code_block") or "")
        allow_real = bool(
            request.get("allow_external_network")
            or (request.get("live_test_result") or {}).get("success")
        )

        validation = validate_tool_manifest(
            manifest,
            adapter_code=adapter_code,
            sample_input=request.get("sample_input") or {},
            dynamic=True,
            real_run=allow_real,
        )

        static_errors = _author_adapter_static_errors(adapter_code, manifest)
        if static_errors:
            validation["errors"] = sorted(set(validation.get("errors", []) + static_errors))
            validation["success"] = False
            validation["status"] = "failed"

        snippet = None
        if validation.get("success"):
            snippet = _fallback_snippet(manifest, request.get("sample_input") or {})
            snippet_validation = _validate_author_snippet(snippet, manifest)

            if not snippet_validation["success"]:
                validation["success"] = False
                validation["status"] = "failed"
                validation["errors"] = sorted(
                    set(validation.get("errors", []) + snippet_validation.get("errors", []))
                )

            validation["snippet_validation"] = snippet_validation

        return {
            "needs_clarification": False,
            "questions": [],
            "manifest": manifest,
            "adapter_code": adapter_code,
            "sample_input": request.get("sample_input") or {},
            "validation": validation,
            "snippet": snippet,
            "model_notes": model_notes,
            "warnings": warnings,
            "requires_human_confirmation": True,
        }

    plan = await _run_planner(request, model_notes, warnings)
    live_success = _live_test_success_from_request(request)

    if (
        action == "generate"
        and live_success
        and _canonical_wrapper_family(plan.get("wrapper_family")) == "http_api"
    ):
        plan["authoring_tool_plan"] = []
        plan["requires_authoring_tools"] = False
        if plan.get("manifest"):
            plan["ready_for_code_generation"] = True

    if action != "generate" and (plan.get("requires_authoring_tools") or plan.get("authoring_tool_plan")):
        plan, _helper_results = _run_authoring_tool_plan(plan, request)
        return _author_response_from_plan(plan, model_notes=model_notes, warnings=warnings)

    if action in {"clarify", "configure"} or plan.get("needs_clarification") or not plan.get("ready_for_code_generation"):
        return _author_response_from_plan(plan, model_notes=model_notes, warnings=warnings)

    raw_manifest = plan.get("manifest") or build_tool_manifest_draft(request)

    sample_input = (
        request.get("sample_input")
        if isinstance(request.get("sample_input"), dict) and request.get("sample_input")
        else plan.get("sample_input") or {}
    )

    code_block = str(request.get("code_block") or "")

    plan = await _validate_and_repair_contract_loop(
        request=request,
        plan=plan,
        model_notes=model_notes,
        warnings=warnings,
        max_rounds=int(os.environ.get("TOOL_AUTHOR_CONTRACT_REPAIR_ROUNDS", "2")),
    )

    if plan.get("needs_clarification"):
        return _author_response_from_plan(plan, model_notes=model_notes, warnings=warnings)

    if isinstance(plan.get("validation"), dict) and plan["validation"].get("success") is False:
        return {
            "needs_clarification": False,
            "questions": [],
            "tool_kind": plan.get("tool_kind"),
            "operation": plan.get("operation"),
            "manifest": plan.get("manifest") or raw_manifest,
            "adapter_code": "",
            "sample_input": plan.get("sample_input") or sample_input,
            "validation": plan["validation"],
            "snippet": None,
            "model_notes": model_notes,
            "warnings": warnings,
            "requires_human_confirmation": True,
        }

    raw_manifest = plan.get("manifest") or raw_manifest

    wrapper_for_sample = _canonical_wrapper_family(
        (plan.get("adapter_contract") or {}).get("wrapper_family")
        if isinstance(plan.get("adapter_contract"), dict)
        else plan.get("wrapper_family")
    )

    sample_input = await _author_sample_input_with_model(
        request=request,
        plan=plan,
        manifest=raw_manifest,
        wrapper_family=wrapper_for_sample,
        model_notes=model_notes,
        warnings=warnings,
    )

    contract = plan.get("adapter_contract")
    if not isinstance(contract, dict) or not contract:
        contract = _build_tool_contract(
            request=request,
            plan=plan,
            manifest=raw_manifest,
            sample_input=sample_input,
        )

    contract_registry_validation = validate_tool_contract_registry_consistency(contract)
    if not contract_registry_validation.get("success"):
        return {
            "needs_clarification": False,
            "questions": [],
            "tool_kind": plan.get("tool_kind"),
            "operation": plan.get("operation"),
            "manifest": raw_manifest,
            "adapter_code": "",
            "sample_input": sample_input,
            "validation": {
                "success": False,
                "status": "contract_validation_failed",
                "errors": contract_registry_validation.get("errors") or [],
                "warnings": contract_registry_validation.get("warnings") or [],
                "contract_validation_log": plan.get("contract_validation_log") or [],
            },
            "snippet": None,
            "model_notes": model_notes,
            "warnings": warnings,
            "requires_human_confirmation": True,
        }

    wrapper_family = _canonical_wrapper_family(contract.get("wrapper_family"))

    if wrapper_family in {
        "http_api",
        "managed_helper",
        "python_compute",
        "file_io",
        "database_query",
        "local_command",
    }:
        return await _author_wrapper_with_core_logic(
            wrapper_family=wrapper_family,
            request=request,
            plan=plan,
            contract=contract,
            raw_manifest=raw_manifest,
            sample_input=sample_input,
            contract_registry_validation=contract_registry_validation,
            model_notes=model_notes,
            warnings=warnings,
            live_success=live_success,
        )

    manifest = raw_manifest
    mode = "normalize_existing_code" if code_block.strip() else "generate_new_adapter"

    code_messages = [
        {
            "role": "system",
            "content": (
                f"You are code_model in mode={mode}. Return Python code only. "
                "This is custom_adapter fallback only. "
                "Consume only confirmed requirements/config/sample input/live_test_result/manifest/implementation_plan/protocol. "
                "Do not guess endpoint, secret name, auth scheme, inputs, or outputs. "
                "For network/API adapters, include `if os.getenv(\"SKILL_TRIAL_RUN\") == \"1\": return mock_result`, "
                "read secrets only with os.getenv(DECLARED_ENV_NAME), never payload.get('api_key'), and never print or return secrets. "
                "Include run(payload), manifest function wrapper, and JSON main()."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "final_requirement": request.get("description") or "",
                    "confirmed_config": request.get("config") or {},
                    "sample_input": sample_input,
                    "live_test_result": (
                        request.get("live_test_result")
                        or (plan.get("authoring_context") or {}).get("live_test_result")
                    ),
                    "authoring_context": plan.get("authoring_context") or {},
                    "manifest": manifest,
                    "implementation_plan": plan.get("implementation_plan"),
                    "adapter_protocol": (
                        "Expose run(payload: dict|None)->dict and the manifest function; "
                        "dynamic validation runs with SKILL_TRIAL_RUN=1 and must not access real external services."
                    ),
                    "code_block": code_block,
                },
                ensure_ascii=False,
            ),
        },
    ]

    code_json, code_ack, code_err = await _complete_author_model(
        "code",
        code_messages,
        reason=f"creator_tool_author_{mode}",
    )
    if code_ack:
        model_notes.append(f"code_model={code_ack['model']}")
    if code_err:
        warnings.append(f"code_model unavailable, used deterministic fallback: {code_err}")

    adapter_code = (
        _strip_code_fence(str(code_json.get("adapter_code") or code_json.get("code") or ""))
        if code_json
        else ""
    )

    if not adapter_code:
        adapter_code = (
            _normalize_existing_code_fallback(code_block, manifest)
            if code_block.strip()
            else generate_adapter_code(manifest)
        )

    repair_log: list[dict[str, Any]] = []
    validation: dict[str, Any] = {}

    for attempt in range(3):
        validation = validate_tool_manifest(
            manifest,
            adapter_code=adapter_code,
            sample_input=sample_input,
            dynamic=True,
            real_run=False,
        )

        static_errors = _author_adapter_static_errors(adapter_code, manifest)
        if static_errors:
            validation["errors"] = sorted(set(validation.get("errors", []) + static_errors))
            validation["success"] = False
            validation["status"] = "failed"

        if validation.get("success"):
            break

        if attempt >= 2:
            break

        repair_log.append(
            {
                "attempt": attempt + 1,
                "errors": validation.get("errors", []),
                "warnings": validation.get("warnings", []),
            }
        )

        repair_messages = [
            {
                "role": "system",
                "content": (
                    "Repair the Python adapter locally. Preserve business logic. "
                    "Return code only. Keep SKILL_TRIAL_RUN mock behavior for network/API adapters."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "manifest": manifest,
                        "sample_input": sample_input,
                        "validation": validation,
                        "adapter_code": adapter_code,
                    },
                    ensure_ascii=False,
                ),
            },
        ]

        repair_json, _repair_ack, repair_err = await _complete_author_model(
            "code",
            repair_messages,
            reason="creator_tool_author_repair",
        )

        repaired = (
            _strip_code_fence(str(repair_json.get("adapter_code") or repair_json.get("code") or ""))
            if repair_json
            else ""
        )

        if repair_err or not repaired:
            warnings.append(f"automatic repair stopped; using fallback/local code: {repair_err or 'empty repair'}")
            break

        adapter_code = repaired

    validation["repair_log"] = repair_log
    validation["adapter_contract"] = contract
    validation["contract_registry_validation"] = contract_registry_validation

    return {
        "needs_clarification": False,
        "questions": [],
        "tool_kind": plan.get("tool_kind"),
        "operation": plan.get("operation"),
        "manifest": manifest,
        "adapter_code": adapter_code,
        "sample_input": sample_input,
        "validation": validation,
        "snippet": None,
        "model_notes": model_notes,
        "warnings": warnings,
        "requires_human_confirmation": True,
    }

def _extract_model_internal_code(adapter_code: str) -> str:
    start = "# === MODEL_INTERNAL_CODE_START ==="
    end = "# === MODEL_INTERNAL_CODE_END ==="

    code = str(adapter_code or "")
    if start not in code or end not in code:
        return ""

    try:
        return code.split(start, 1)[1].split(end, 1)[0].strip()
    except Exception:
        return ""


def _replace_model_internal_code(adapter_code: str, internal_code: str) -> str:
    start = "# === MODEL_INTERNAL_CODE_START ==="
    end = "# === MODEL_INTERNAL_CODE_END ==="

    code = str(adapter_code or "")
    internal = _strip_code_fence(str(internal_code or "")).strip()

    if start not in code or end not in code:
        return code

    before, rest = code.split(start, 1)
    _old, after = rest.split(end, 1)

    return before.rstrip() + "\n" + start + "\n" + internal + "\n" + end + after


async def _revise_adapter_and_snippet_with_feedback(
    request: dict[str, Any],
    *,
    model_notes: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    manifest = request.get("manifest") if isinstance(request.get("manifest"), dict) else {}
    adapter_code = str(request.get("adapter_code") or request.get("code_block") or "")
    sample_input = request.get("sample_input") if isinstance(request.get("sample_input"), dict) else {}
    validation = request.get("validation") if isinstance(request.get("validation"), dict) else {}

    feedback = str(
        request.get("human_feedback")
        or request.get("review_feedback")
        or request.get("feedback")
        or ""
    ).strip()

    snippet = request.get("snippet") if isinstance(request.get("snippet"), dict) else None
    wrapper_family = _canonical_wrapper_family(
        request.get("wrapper_family")
        or manifest.get("wrapper_family")
        or ""
    )

    current_internal = _extract_model_internal_code(adapter_code)

    if not feedback:
        return {
            "needs_clarification": False,
            "questions": [],
            "manifest": manifest,
            "adapter_code": adapter_code,
            "sample_input": sample_input,
            "validation": {
                "success": False,
                "status": "missing_human_feedback",
                "errors": ["human_feedback is required for action=revise"],
                "warnings": [],
            },
            "snippet": snippet,
            "model_notes": model_notes,
            "warnings": warnings,
            "requires_human_confirmation": True,
        }

    if not current_internal:
        return {
            "needs_clarification": False,
            "questions": [],
            "manifest": manifest,
            "adapter_code": adapter_code,
            "sample_input": sample_input,
            "validation": {
                "success": False,
                "status": "not_revisable",
                "errors": ["adapter_code has no MODEL_INTERNAL_CODE_START/END block"],
                "warnings": [],
            },
            "snippet": snippet,
            "model_notes": model_notes,
            "warnings": warnings,
            "requires_human_confirmation": True,
        }

    policy = _core_logic_policy(wrapper_family, manifest=manifest)
    allowed_imports = sorted(policy.get("allowed_imports") or [])

    messages = [
        {
            "role": "system",
            "content": (
                "You are code_model revising a generated tool after human testing. "
                "Return strict JSON only: {\"internal_code\":\"...\", \"snippet\": {...}, \"notes\": []}. "
                "Revise only execute_task(payload, context). Do not write a full adapter. "
                "Preserve the platform wrapper protocol. "
                "Use the human feedback and validation result to fix the core logic. "
                "Allowed imports are: "
                + (", ".join(allowed_imports) if allowed_imports else "(none)")
                + ". Do not import anything else."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "wrapper_family": wrapper_family,
                    "manifest": manifest,
                    "sample_input": sample_input,
                    "current_internal_code": current_internal,
                    "current_snippet": snippet,
                    "validation": validation,
                    "human_feedback": feedback,
                    "task": (
                        "Revise execute_task(payload, context) and revise snippet if needed. "
                        "Do not change wrapper code."
                    ),
                },
                ensure_ascii=False,
            ),
        },
    ]

    result, ack, err = await _complete_author_model(
        "code",
        messages,
        reason="creator_tool_author_revise_from_human_feedback",
    )

    if ack:
        model_notes.append(f"revise_code_model={ack['model']}")

    if err:
        warnings.append(f"revise code_model unavailable: {err}")
        result = {}

    revised_internal = _strip_code_fence(
        str((result or {}).get("internal_code") or (result or {}).get("code") or "")
    )

    internal_errors = _core_logic_code_errors(
        wrapper_family,
        revised_internal,
        manifest=manifest,
    )

    if internal_errors:
        return {
            "needs_clarification": False,
            "questions": [],
            "manifest": manifest,
            "adapter_code": adapter_code,
            "model_internal_code": current_internal,
            "sample_input": sample_input,
            "validation": {
                "success": False,
                "status": "revision_failed_static_check",
                "errors": internal_errors,
                "warnings": [],
            },
            "snippet": snippet,
            "model_notes": model_notes,
            "warnings": warnings,
            "requires_human_confirmation": True,
        }

    revised_adapter = _replace_model_internal_code(adapter_code, revised_internal)

    revised_snippet = result.get("snippet") if isinstance(result.get("snippet"), dict) else snippet

    revised_validation = validate_tool_manifest(
        manifest,
        adapter_code=revised_adapter,
        sample_input=sample_input,
        dynamic=True,
        real_run=bool(request.get("allow_external_network")),
    )

    if revised_snippet:
        snippet_validation = _validate_author_snippet(revised_snippet, manifest)
        revised_validation["snippet_validation"] = snippet_validation
        if not snippet_validation.get("success"):
            revised_validation["success"] = False
            revised_validation["status"] = "failed"
            revised_validation["errors"] = sorted(
                set(revised_validation.get("errors", []) + snippet_validation.get("errors", []))
            )

    return {
        "needs_clarification": False,
        "questions": [],
        "manifest": manifest,
        "adapter_code": revised_adapter,
        "adapter_code_kind": "wrapper_with_model_internal_code",
        "adapter_edit_policy": "internal_code_only",
        "model_internal_code": revised_internal,
        "sample_input": sample_input,
        "validation": revised_validation,
        "snippet": revised_snippet,
        "model_notes": model_notes,
        "warnings": warnings,
        "requires_human_confirmation": True,
    }

async def stream_author_tool(request: dict[str, Any]):
    """SSE-friendly wrapper that emits coarse progress events for long authoring actions."""
    action = str(request.get("action") or ("finalize" if request.get("stage") == "finalize" else "clarify"))
    try:
        yield {"event": "step_started", "step": "planner", "message": "正在理解需求"}
        if action == "live_test":
            yield {"event": "step_started", "step": "live_test", "message": "正在测试连接"}
        elif action == "generate":
            yield {"event": "step_started", "step": "code", "message": "正在生成 adapter"}
        elif action == "finalize":
            yield {"event": "step_started", "step": "validation", "message": "正在执行动态验证"}
        result = await author_tool(request)
        for item in result.get("authoring_tool_plan") or []:
            if isinstance(item, dict):
                yield {"event": "tool_call_planned", "tool": item.get("tool_name"), "reason": item.get("reason") or ""}
        for item in result.get("authoring_tool_results") or []:
            if not isinstance(item, dict):
                continue
            tool_name = item.get("tool_name")
            yield {"event": "tool_call_started", "tool": tool_name}
            if item.get("requires_input"):
                yield {"event": "tool_call_requires_input", "tool": tool_name, "schema": item.get("schema") or {}}
            yield {"event": "tool_call_result", "tool": tool_name, "success": bool(item.get("success"))}
        if result.get("needs_clarification"):
            yield {"event": "clarification_required", "questions": result.get("questions") or []}
        if result.get("live_test_result"):
            yield {"event": "live_test_result", **result["live_test_result"]}
        if result.get("adapter_code"):
            yield {"event": "model_delta", "step": "code", "delta": result.get("adapter_code")}
        elif action == "generate":
            yield {
                "event": "code_not_generated",
                "step": "code",
                "message": "未生成 adapter_code；可能仍需澄清、配置或 live_test。",
                "needs_clarification": bool(result.get("needs_clarification")),
                "requires_config": bool(result.get("requires_config")),
                "requires_authoring_tools": bool(
                    result.get("requires_authoring_tools") or result.get("authoring_tool_plan")),
                "ready_for_code_generation": bool(result.get("ready_for_code_generation")),
            }
        if result.get("validation"):
            yield {"event": "validation", "success": bool(result["validation"].get("success")), "errors": result["validation"].get("errors") or [], "warnings": result["validation"].get("warnings") or []}
        yield {"event": "step_finished", "step": "planner", "summary": result.get("validation", {}).get("status", "done")}
        yield {"event": "final_result", **result}
    except Exception as exc:
        yield {"event": "error", "message": str(exc)}


def tool_status(capability: ToolCapability) -> dict[str, Any]:
    missing_env = [name for name in capability.required_env if not os.environ.get(name)]
    missing_secrets = [name for name in capability.required_secrets if not os.environ.get(name)]
    helper_names = _runtime_helper_names()
    runtime_helpers_available = [name for name in capability.helper_imports if name in helper_names]
    missing_runtime_helpers = [name for name in capability.helper_imports if name not in helper_names]
    missing_dependencies = [name for name in capability.dependencies if not _dependency_available(name)]
    creator_available = capability.enabled_by_default and capability.allow_creator_use
    return {
        **asdict(capability),
        "allowed_roles": capability.allowed_roles or capability.roles,
        "enabled": capability.enabled_by_default,
        "creator_available": creator_available,
        "configured": not missing_env and not missing_secrets,
        "missing_env": missing_env,
        "missing_secrets": missing_secrets,
        # Toggle overrides remain process-local; custom registered manifests are
        # persisted separately in backend/config/tool_registry.custom.json.
        "override_persistence": TOOL_OVERRIDE_PERSISTENCE,
        "runtime_helpers_available": runtime_helpers_available,
        "missing_runtime_helpers": missing_runtime_helpers,
        "missing_dependencies": missing_dependencies,
    }

async def _author_managed_helper_adapter(
    *,
    request: dict[str, Any],
    plan: dict[str, Any],
    contract: dict[str, Any],
    raw_manifest: dict[str, Any],
    sample_input: dict[str, Any],
    contract_registry_validation: dict[str, Any],
    model_notes: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    return await _author_wrapper_with_core_logic(
        wrapper_family="managed_helper",
        request=request,
        plan=plan,
        contract=contract,
        raw_manifest=raw_manifest,
        sample_input=sample_input,
        contract_registry_validation=contract_registry_validation,
        model_notes=model_notes,
        warnings=warnings,
    )

def _make_snippet(
    tool: str,
    helper: str,
    title: str,
    code: str,
    outputs: dict[str, str],
    *,
    kind: SnippetKind = "minimal_usage",
    roles: list[str] | None = None,
    capabilities: list[str] | None = None,
    failures: list[str] | None = None,
    description: str = "",
    anti_patterns: list[str] | None = None,
    priority: int = 100,
    usage_policy: UsagePolicy = "helper_preferred",
) -> ToolSnippet:
    return ToolSnippet(
        id=f"{helper}.{kind}",
        title=title,
        kind=kind,
        applies_to={"roles": roles or [], "capabilities": capabilities or [tool], "failure_layers": failures or ["helper_call_failed", "final_platform_output_value_invalid", "artifact_missing", "artifact_invalid"]},
        description=description or title,
        code=code.strip(),
        expected_input_shape={},
        expected_output_shape=outputs,
        return_rule="If the helper returns the platform stdout dict with file_paths/file_outputs, return that dict directly; only merge with extra scalar fields using {**result, ...}.",
        anti_patterns=anti_patterns or ["Do not guess parameter names.", "Do not wrap helper result inside an output field.", "Do not write files outside OUTPUT_DIR/outputs."],
        requires=capabilities or [tool],
        usage_policy=usage_policy,
        priority=priority,
    )


def _install_builtin_tool_snippets() -> None:
    specs: dict[str, list[ToolSnippet]] = {
        "pdf_generation": [
            _make_snippet("pdf_generation", "create_pdf", "Create a simple text PDF", """
from backend.services.skill_runtime import create_pdf

content = payload.get("text") or payload.get("content") or "Generated PDF"
result = create_pdf(content, filename="output.pdf")
return result
""", {"pdf_path": "string", "file_paths": "list[string]", "file_outputs": "list[object]"}, roles=["pdf_builder", "document_generator", "composite_generator"], capabilities=["pdf_generation"], anti_patterns=["Do not return {'pdf_path': result}; create_pdf returns a dict, not a string.", "Do not pass a dict as the text argument unless you intentionally want its string/list lines.", "Do not write outside OUTPUT_DIR; use filename or output_dir/output_path under outputs."], priority=120),
            _make_snippet("pdf_generation", "build_pdf_report", "Create a structured PDF report", """
from backend.services.skill_runtime import build_pdf_report

sections = [
    {"heading": "Summary", "content": payload.get("summary") or "No summary provided."},
    {"heading": "Details", "content": payload.get("details") or []},
]
result = build_pdf_report("Report", sections, image_paths=payload.get("image_paths") or [], filename="report.pdf")
return result
""", {"pdf_path": "string", "file_paths": "list[string]", "file_outputs": "list[object]"}, roles=["pdf_builder"], capabilities=["pdf_generation"], priority=100),
            _make_snippet("pdf_generation", "images_to_pdf", "Convert images to one PDF", """
from backend.services.skill_runtime import images_to_pdf

image_paths = payload.get("image_paths") or []
result = images_to_pdf(image_paths, output_path="outputs/images.pdf")
return result
""", {"pdf_path": "string", "file_paths": "list[string]", "file_outputs": "list[object]"}, roles=["pdf_builder"], capabilities=["pdf_generation"], kind="file_output_usage", priority=90),
            _make_snippet("pdf_generation", "merge_pdfs", "Merge several PDFs", """
from backend.services.skill_runtime import merge_pdfs

pdf_paths = payload.get("pdf_paths") or []
result = merge_pdfs(pdf_paths, output_path="outputs/merged.pdf")
return result
""", {"pdf_path": "string", "file_paths": "list[string]", "file_outputs": "list[object]"}, roles=["pdf_builder"], capabilities=["pdf_generation"], kind="batch_usage", priority=90),
        ],
        "image_generation": [
            _make_snippet("image_generation", "generate_stable_diffusion_image", "Generate one image", """
from backend.services.skill_runtime import generate_stable_diffusion_image

prompt = payload.get("prompt") or payload.get("description") or payload.get("text") or "A clean illustration"
result = generate_stable_diffusion_image(prompt, filename_prefix="generated")
return {"image_path": result["image_path"], "image_paths": [result["image_path"]]}
""", {"image_path": "string", "image_paths": "list[string]"}, roles=["image_generator", "composite_generator"], capabilities=["image_generation"], anti_patterns=["Do not call /v1/images/generations directly; use the registered helper.", "Do not use VISION_MODEL for image generation.", "During SKILL_TRIAL_RUN the helper may return a deterministic minimal file; still return image_path/image_paths."], priority=120),
            _make_snippet("image_generation", "generate_stable_diffusion_image", "Generate multiple images in a loop", """
from backend.services.skill_runtime import generate_stable_diffusion_image

prompts = payload.get("prompts") or [payload.get("prompt") or "Generated image"]
image_paths = []
for index, prompt in enumerate(prompts, start=1):
    result = generate_stable_diffusion_image(str(prompt), filename_prefix=f"generated_{index}")
    image_paths.append(result["image_path"])
return {"image_paths": image_paths, "image_path": image_paths[0] if image_paths else ""}
""", {"image_path": "string", "image_paths": "list[string]"}, kind="batch_usage", roles=["image_generator", "composite_generator"], capabilities=["image_generation"], priority=80),
        ],
        "docx_generation": [_make_snippet("docx_generation", "create_docx", "Create a DOCX document", """
from backend.services.skill_runtime import create_docx

content = payload.get("sections") or payload.get("text") or "Generated document"
result = create_docx(content, filename="output.docx", title=payload.get("title") or "Document")
return result
""", {"docx_path": "string", "file_paths": "list[string]", "file_outputs": "list[object]"}, roles=["docx_builder"], capabilities=["docx_generation"], priority=120)],
        "pptx_generation": [_make_snippet("pptx_generation", "create_pptx", "Create a PPTX deck", """
from backend.services.skill_runtime import create_pptx

slides = payload.get("slides") or payload.get("sections") or [payload.get("text") or "Generated slide"]
result = create_pptx(slides, filename="output.pptx", title=payload.get("title") or "Presentation")
return result
""", {"pptx_path": "string", "file_paths": "list[string]", "file_outputs": "list[object]"}, roles=["pptx_builder"], capabilities=["pptx_generation"], priority=120)],
        "pdf_parsing": [_make_snippet("pdf_parsing", "extract_pdf_text", "Extract text from an input PDF", """
from backend.services.skill_runtime import extract_pdf_text

input_files = payload.get("input_files") or payload.get("files") or []
pdf_path = payload.get("pdf_path") or (input_files[0] if input_files else "")
result = extract_pdf_text(pdf_path, max_pages=payload.get("max_pages"))
return {"text": result["text"], "pages": result.get("pages", []), "pdf_path": result.get("pdf_path", pdf_path)}
""", {"text": "string", "pages": "list[string]", "pdf_path": "string"}, roles=["pdf_parser"], capabilities=["pdf_parsing"], priority=110)],
        "docx_parsing": [_make_snippet("docx_parsing", "read_docx_text", "Read text from a DOCX", """
from backend.services.skill_runtime import read_docx_text

input_files = payload.get("input_files") or payload.get("files") or []
docx_path = payload.get("docx_path") or (input_files[0] if input_files else "")
result = read_docx_text(docx_path)
return {"text": result["text"], "paragraphs": result.get("paragraphs", []), "source_path": result.get("source_path", docx_path)}
""", {"text": "string", "paragraphs": "list[string]", "source_path": "string"}, roles=["docx_parser"], capabilities=["docx_parsing"], priority=100)],
        "pptx_parsing": [_make_snippet("pptx_parsing", "read_pptx_text", "Read text from a PPTX", """
from backend.services.skill_runtime import read_pptx_text

input_files = payload.get("input_files") or payload.get("files") or []
pptx_path = payload.get("pptx_path") or (input_files[0] if input_files else "")
result = read_pptx_text(pptx_path)
return {"text": result["text"], "slides": result.get("slides", []), "source_path": result.get("source_path", pptx_path)}
""", {"text": "string", "slides": "list[string]", "source_path": "string"}, roles=["pptx_parser"], capabilities=["pptx_parsing"], priority=100)],
        "web_search": [_make_snippet("web_search", "web_search", "Search the web with the registered helper", """
from backend.services.skill_runtime import web_search

query = payload.get("query") or payload.get("user_request") or payload.get("text") or ""
result = web_search(query, top_k=int(payload.get("top_k") or 5), language=payload.get("language"))
return {"results": result.get("results", []), "query": query}
""", {"results": "list[object]", "query": "string"}, roles=["search_reader"], capabilities=["web_search"], anti_patterns=["Do not use requests against undeclared search APIs when web_search is registered.", "Do not output secrets or raw provider credentials.", "If SEARCHXNG_BASE_URL is missing, fail clearly or use trial-run behavior."], priority=100)],
        "database_read": [_make_snippet("database_read", "query_database_readonly", "Run a bounded readonly SQL query", """
from backend.services.skill_runtime import query_database_readonly

sql = payload.get("sql") or "SELECT 1 AS value"
result = query_database_readonly(sql, params=payload.get("params") or {}, limit=int(payload.get("limit") or 100))
return result
""", {"columns": "list[string]", "rows": "list[object]", "row_count": "integer", "truncated": "boolean"}, roles=["database_reader"], capabilities=["database_read"], anti_patterns=["Only SELECT/WITH is allowed; never INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE/CREATE.", "Do not read or print DATABASE_URL.", "Do not bypass query_database_readonly with a direct database driver."], priority=120, usage_policy="helper_required")],
        "wechat_draft": [_make_snippet("wechat_draft", "create_wechat_draft", "Create a WeChat draft only", """
from backend.services.skill_runtime import create_wechat_draft

result = create_wechat_draft(
    title=payload.get("title") or "Untitled",
    content_html=payload.get("content_html") or payload.get("html") or "<p>Draft</p>",
    author=payload.get("author") or "",
    digest=payload.get("digest") or "",
    cover_image_path=payload.get("cover_image_path"),
)
return result
""", {"draft_id": "string", "media_id": "string", "url": "string|null", "status": "string"}, roles=["wechat_draft_creator"], capabilities=["wechat_draft"], anti_patterns=["Do not publish automatically from a draft creator script.", "Do not output WECHAT_APP_ID or WECHAT_APP_SECRET.", "Use upload_wechat_media only for declared local cover images."], priority=120, usage_policy="helper_required")],
        "wechat_publish": [_make_snippet("wechat_publish", "publish_wechat_draft", "Publish an explicitly requested WeChat draft", """
from backend.services.skill_runtime import publish_wechat_draft

draft_id = payload.get("draft_id") or ""
result = publish_wechat_draft(draft_id)
return result
""", {"draft_id": "string", "publish_id": "string", "status": "string"}, roles=["wechat_publisher"], capabilities=["wechat_publish"], anti_patterns=["Do not publish unless the user explicitly requested publishing and the tool is enabled.", "Do not output WeChat secrets.", "Do not create a draft and publish as a hidden side effect unless the plan says so."], priority=120, usage_policy="helper_required")],
    }
    for name, snippets in specs.items():
        cap = BUILTIN_TOOL_CAPABILITIES.get(name)
        if cap is not None and not cap.snippets:
            BUILTIN_TOOL_CAPABILITIES[name] = replace(cap, snippets=snippets)


_install_builtin_tool_snippets()
_load_registered_tools_from_disk()
