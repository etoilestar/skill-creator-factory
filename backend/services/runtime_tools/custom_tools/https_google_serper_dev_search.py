from __future__ import annotations

# AUTO-GENERATED EXTERNAL API WRAPPER.
# Only the code between MODEL_INTERNAL_CODE_START/END may come from code_model.
# Wrapper protocol, env access, network request, manifest(), run(), and main()
# are generated deterministically by the platform.
# internal_audit=model_internal_normalize_response_only

import json
import os
import sys
from typing import Any
from pathlib import Path

import requests


FUNCTION_NAME = 'https_google_serper_dev_search'
TOOL_ENV_PREFIX = 'TOOLCFG_HTTPS_GOOGLE_SERPER_DEV_SEARCH'
ENDPOINT_ENV = 'TOOLCFG_HTTPS_GOOGLE_SERPER_DEV_SEARCH_BASE_URL'
METHOD_ENV = 'TOOLCFG_HTTPS_GOOGLE_SERPER_DEV_SEARCH_METHOD'
SECRET_ENV = 'TOOLCFG_HTTPS_GOOGLE_SERPER_DEV_SEARCH_SECRET'
AUTH_HEADER_ENV = 'TOOLCFG_HTTPS_GOOGLE_SERPER_DEV_SEARCH_AUTH_HEADER'
BODY_TEMPLATE_ENV = 'TOOLCFG_HTTPS_GOOGLE_SERPER_DEV_SEARCH_BODY_TEMPLATE_JSON'
QUERY_TEMPLATE_ENV = 'TOOLCFG_HTTPS_GOOGLE_SERPER_DEV_SEARCH_QUERY_TEMPLATE_JSON'
HEADERS_TEMPLATE_ENV = 'TOOLCFG_HTTPS_GOOGLE_SERPER_DEV_SEARCH_HEADERS_TEMPLATE_JSON'

ENDPOINT = os.getenv(ENDPOINT_ENV, 'https://google.serper.dev/search')
METHOD = os.getenv(METHOD_ENV, 'POST').upper()
AUTH_HEADER_NAME = os.getenv(AUTH_HEADER_ENV, 'X-API-KEY')
BODY_TEMPLATE = json.loads(os.getenv(BODY_TEMPLATE_ENV, '{"q": "apple inc"}'))
QUERY_TEMPLATE = json.loads(os.getenv(QUERY_TEMPLATE_ENV, '{}'))
HEADERS_TEMPLATE = json.loads(os.getenv(HEADERS_TEMPLATE_ENV, '{"Content-Type": "application/json"}'))

def _load_manifest_from_registry() -> dict[str, Any]:
    """Load manifest from the custom registry; wrapper code is runtime-only."""
    registry_path = Path(__file__).resolve().parents[3] / "config" / "tool_registry.custom.json"
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    records = payload.get("tools") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        return {}
    for record in records:
        if not isinstance(record, dict):
            continue
        names = {str(record.get(key) or "") for key in ("name", "tool_name", "tool_id")}
        if FUNCTION_NAME in names or "serper_search" in names:
            return record
    return {}


MANIFEST_DATA = _load_manifest_from_registry()


def _flatten(prefix: str, value: Any, out: dict[str, Any]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            _flatten(path, item, out)
    else:
        out[prefix] = value


def _render_string(text: str, payload: dict[str, Any]) -> str:
    values: dict[str, Any] = {}
    _flatten("", payload, values)
    for key, value in values.items():
        rendered = "" if value is None else str(value)
        text = text.replace("${input." + key + "}", rendered)
        text = text.replace("${payload." + key + "}", rendered)
        text = text.replace("{{" + key + "}}", rendered)
        text = text.replace("{{ " + key + " }}", rendered)
    return text


def _render(value: Any, payload: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return _render_string(value, payload)
    if isinstance(value, dict):
        return {key: _render(item, payload) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_render(item, payload) for item in value]
    return value


def _default_normalize_response(data: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    items = data.get("organic", []) if isinstance(data, dict) else []
    results = []
    for idx, item in enumerate(items or [], start=1):
        if not isinstance(item, dict):
            continue
        results.append({
            "title": item.get("title") or item.get("name") or "",
            "snippet": item.get("snippet") or item.get("description") or "",
            "link": item.get("link") or item.get("url") or item.get("imageUrl") or "",
            "position": item.get("position") or idx,
        })
    return {
        "success": True,
        "results": results,
        "total": len(results),
        "knowledgeGraph": data.get("knowledgeGraph", {}) if isinstance(data, dict) else {},
        "answerBox": data.get("answerBox", {}) if isinstance(data, dict) else {},
        "relatedSearches": data.get("relatedSearches", []) if isinstance(data, dict) else [],
        "raw": data,
    }


# === MODEL_INTERNAL_CODE_START ===
def normalize_response(data: dict, payload: dict) -> dict:
    results = []
    
    # Process organic search results
    if 'organic' in data:
        for item in data['organic']:
            result = {
                'title': item.get('title', ''),
                'link': item.get('link', ''),
                'snippet': item.get('snippet', ''),
                'position': item.get('position', 0)
            }
            
            # Include date if present
            if 'date' in item:
                result['date'] = item['date']
            
            # Include sitelinks if present
            if 'sitelinks' in item:
                result['sitelinks'] = item['sitelinks']
            
            results.append(result)
    
    # Process knowledge graph if present
    knowledge_graph = data.get('knowledgeGraph', {})
    
    # Process related searches if present
    related_searches = data.get('relatedSearches', [])
    
    # Build normalized response
    normalized = {
        'success': True,
        'results': results,
        'total': len(results),
        'knowledgeGraph': knowledge_graph,
        'relatedSearches': related_searches,
        'raw': data
    }
    
    # Include other optional fields
    if 'credits' in data:
        normalized['credits'] = data['credits']
    
    if 'answerBox' in data:
        normalized['answerBox'] = data['answerBox']
    
    return normalized
# === MODEL_INTERNAL_CODE_END ===


def _normalize(data: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    fn = globals().get("normalize_response")
    required = {"success", "results", "total"}

    if callable(fn):
        try:
            value = fn(data, payload)
        except Exception as exc:
            fallback = _default_normalize_response(data, payload)
            fallback["_normalizer_warning"] = {
                "reason": "model_normalize_response_raised_exception",
                "error": str(exc),
                "required_keys": sorted(required),
            }
            return fallback

        if isinstance(value, dict):
            if required.issubset(value.keys()) and isinstance(value.get("results"), list):
                return value

            fallback = _default_normalize_response(data, payload)
            fallback["_normalizer_warning"] = {
                "reason": "model_normalize_response_did_not_match_required_contract",
                "model_return_keys": sorted(str(key) for key in value.keys()),
                "required_keys": sorted(required),
            }
            return fallback

    return _default_normalize_response(data, payload)


def _trial_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": True,
        "results": [
            {
                "title": "Example result",
                "snippet": "Deterministic trial result for schema validation.",
                "link": "https://example.com",
                "position": 1,
            }
        ],
        "total": 1,
        "knowledgeGraph": {},
        "answerBox": {},
        "relatedSearches": [],
        "raw": {},
        "trial_run": True,
    }


def run(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(payload or {})

    if os.getenv("SKILL_TRIAL_RUN") == "1":
        return _trial_result(payload)

    if not ENDPOINT.startswith(("http://", "https://")):
        return {"success": False, "error": "Invalid endpoint", "results": [], "total": 0}

    api_key = os.getenv(SECRET_ENV) if SECRET_ENV else ""
    if SECRET_ENV and not api_key:
        return {
            "success": False,
            "error": f"Missing required environment variable: {SECRET_ENV}",
            "results": [],
            "total": 0,
        }

    headers = _render(HEADERS_TEMPLATE, payload)
    headers = dict(headers or {}) if isinstance(headers, dict) else {}
    if METHOD not in {"GET", "HEAD"}:
        headers.setdefault("Content-Type", "application/json")
    if SECRET_ENV:
        headers[AUTH_HEADER_NAME] = api_key

    params = _render(QUERY_TEMPLATE, payload)
    params = dict(params or {}) if isinstance(params, dict) else {}

    body = _render(BODY_TEMPLATE, payload)
    body = dict(body or {}) if isinstance(body, dict) else {}

    try:
        response = requests.request(
            METHOD,
            ENDPOINT,
            headers=headers,
            params=params,
            json=body if METHOD not in {"GET", "HEAD"} else None,
            timeout=30,
        )
        status_code = response.status_code
        text = response.text[:2000]
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.HTTPError:
        return {
            "success": False,
            "error": f"HTTP {status_code}",
            "status_code": status_code,
            "response_preview": text,
            "results": [],
            "total": 0,
        }
    except requests.exceptions.RequestException as exc:
        return {"success": False, "error": str(exc), "results": [], "total": 0}
    except ValueError as exc:
        return {"success": False, "error": f"Non-JSON response: {exc}", "results": [], "total": 0}

    normalized = _normalize(data, payload)
    normalized.setdefault("success", True)
    normalized.setdefault("results", [])
    normalized.setdefault("total", len(normalized.get("results") or []))
    return normalized


def https_google_serper_dev_search(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return run(payload)


def manifest() -> dict[str, Any]:
    return MANIFEST_DATA


def main() -> None:
    raw = sys.stdin.read().strip() or "{}"
    payload = json.loads(raw)
    print(json.dumps(run(payload), ensure_ascii=False))


if __name__ == "__main__":
    main()
