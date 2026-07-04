from __future__ import annotations
import re
from functools import lru_cache
from pathlib import Path
from typing import Any
from pydantic import BaseModel, Field
from backend.services.creator_tool_registry import ToolCapability, list_tool_capabilities
from .tool_pool_models import ToolPoolAddToolRequest

# Generic baseline action tokens that apply across any domain.
_BASELINE_TOKENS = [
    'convert', 'parse', 'extract', 'read', 'write', 'generate', 'analyze',
    'process', 'transform', 'search', 'summarize', 'classify', 'detect',
    'export', 'import', 'merge', 'split', 'validate', 'format',
]

@lru_cache(maxsize=1)
def _build_registry_synonym_map() -> dict[str, list[str]]:
    """Build a token-expansion map from tool registry metadata (domain-agnostic).

    Groups capability_aliases and semantic_tags from every registered tool so
    that matching is driven by what the registry actually declares rather than
    hard-coded domain keywords.  The result is cached for the process lifetime.
    """
    synonym_map: dict[str, list[str]] = {}
    for cap in list_tool_capabilities():
        all_terms: list[str] = []
        for field in ('capability_aliases', 'semantic_tags', 'domain_terms', 'task_verbs'):
            for item in (getattr(cap, field, None) or []):
                t = str(item).strip()
                if t:
                    all_terms.append(t)
        for term in all_terms:
            key = term.lower().replace(' ', '')
            if not key:
                continue
            group = synonym_map.setdefault(key, [])
            for sibling in all_terms:
                s = sibling.lower().replace(' ', '')
                if s and s not in group:
                    group.append(s)
    return synonym_map

class ToolPoolExplorationResult(BaseModel):
    candidate_tool_requests: list[ToolPoolAddToolRequest] = Field(default_factory=list)
    missing_capability_requests: list[dict[str, Any]] = Field(default_factory=list)
    denied_by_explorer: list[dict[str, Any]] = Field(default_factory=list)
    scored_candidates: list[dict[str, Any]] = Field(default_factory=list)
    uploaded_file_triggers: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float = 0.8
    reason: str = ''

def _as_list(value: Any) -> list[Any]:
    if value is None: return []
    if isinstance(value, list): return value
    if isinstance(value, (tuple, set)): return list(value)
    return [value]

def _texts(value: Any) -> list[str]:
    out=[]
    if isinstance(value, dict):
        for k,v in value.items(): out.extend(_texts(k)); out.extend(_texts(v))
    elif isinstance(value, list):
        for item in value: out.extend(_texts(item))
    elif value is not None:
        out.append(str(value))
    return out

def _file_exts(values: Any) -> set[str]:
    out=set()
    for text in _texts(values):
        ext=Path(text).suffix.lower()
        if ext: out.add(ext)
    return out

def _candidate_tools_from_uploaded(uploaded_files: list[dict[str, Any]]) -> tuple[set[str], list[dict[str, Any]]]:
    tools=set(); triggers=[]
    for item in uploaded_files or []:
        if not isinstance(item, dict): continue
        for tool in _as_list(item.get('candidate_tools')):
            if str(tool).strip():
                tools.add(str(tool).strip()); triggers.append({'source':'uploaded_files.candidate_tools','tool_id':str(tool).strip(),'file':item.get('name') or item.get('path') or item.get('filename')})
    return tools, triggers

def _tool_ids_from_spec(spec: dict[str, Any]) -> set[str]:
    """Extract concrete tool IDs from a file spec.

    Prefer concrete tool names over abstract roles. Supported fields:
    - required_tools
    - selected_tools
    - tool_ids
    - selected_tool_ids
    - allowed_tools
    - required_tool_slots entries with tool_id/candidate_tool_id/capability/name
    """
    ids: set[str] = set()
    spec = spec if isinstance(spec, dict) else {}

    for key in (
        "required_tools",
        "selected_tools",
        "tool_ids",
        "selected_tool_ids",
        "allowed_tools",
    ):
        value = spec.get(key)
        for item in _as_list(value):
            text = str(item or "").strip()
            if text:
                ids.add(text)

    for item in _as_list(spec.get("required_tool_slots")):
        if isinstance(item, dict):
            tool_id = (
                item.get("tool_id")
                or item.get("candidate_tool_id")
                or item.get("capability")
                or item.get("capability_id")
                or item.get("name")
            )
            if tool_id:
                ids.add(str(tool_id).strip())
        else:
            text = str(item or "").strip()
            if text:
                ids.add(text)

    return {item for item in ids if item}

def _normalize_tokens(text: str) -> set[str]:
    raw = (text or '').lower()
    raw_nospace = raw.replace(' ', '')
    # Extract individual words/CJK substrings as tokens
    word_tokens = set(re.findall(r'[a-z0-9_\u4e00-\u9fff]+', raw))
    tokens = word_tokens | ({raw_nospace} if raw_nospace else set())
    # Expand via registry-derived synonym map (domain-agnostic)
    synonym_map = _build_registry_synonym_map()
    expanded: set[str] = set()
    for key, siblings in synonym_map.items():
        if key in raw_nospace or any(s in raw_nospace for s in siblings):
            expanded.add(key)
            expanded.update(siblings)
    tokens |= expanded
    # Baseline action tokens as a fallback floor
    for chunk in _BASELINE_TOKENS:
        if chunk in raw:
            tokens.add(chunk)
    return tokens

def _cap_meta(cap: ToolCapability, key: str) -> list[str]:
    return [str(x) for x in getattr(cap, key, []) or [] if str(x).strip()]

def _function_text(cap: ToolCapability) -> str:
    parts=[cap.name, cap.display_name, cap.category, cap.prompt_guidance]
    for fn in cap.functions or []:
        parts.extend([fn.short_description, fn.when_to_use, fn.signature])
    return ' '.join(str(p) for p in parts if p)

def _role_match(cap: ToolCapability, role: str) -> bool:
    roles=set(cap.roles or [])|set(cap.allowed_roles or [])
    return not roles or not role or role in roles or 'generic_script' in roles

def score_tool_for_file_request(cap: ToolCapability, *, text: str, tokens: set[str], exts: set[str], outputs: list[str], role: str, selected_tools: set[str], required_slots: set[str], uploaded_candidate_tools: set[str]) -> tuple[float, list[str], list[str], str]:
    score=0.0; features=[]; terms=[]
    if cap.name in selected_tools:
        score+=100; features.append('exact_selected_tool_match')
    if cap.name in required_slots or set(_cap_meta(cap,'capability_aliases')) & required_slots:
        score+=90; features.append('required_tool_slot_exact_match')
    if cap.name in uploaded_candidate_tools:
        score+=80; features.append('uploaded_candidate_tool_match')
    accepted={e.lower() for e in _cap_meta(cap,'accepted_input_extensions')}
    for ext in sorted(exts & accepted):
        score+=30; features.append(f'accepted_input_extension:{ext}'); terms.append(ext)
    output_types={x.lower() for x in _cap_meta(cap,'output_content_types') + _cap_meta(cap,'output_extensions')}
    for out in outputs:
        low=out.lower()
        for ot in output_types:
            if ot and ot.strip('.') in low:
                score+=30; features.append(f'output_content_type:{ot}'); terms.append(ot); break
    aliases={x.lower() for x in _cap_meta(cap,'capability_aliases')}
    tags={x.lower() for x in _cap_meta(cap,'semantic_tags')}
    for token in sorted(tokens):
        if token.lower() in aliases or token.lower() in tags:
            score+=25; features.append(f'capability_alias:{token}'); terms.append(token)
    raw=text.lower().replace(' ', '')
    for term in _cap_meta(cap,'domain_terms'):
        norm=term.lower().replace(' ', '')
        if norm and norm in raw:
            score+=20; features.append(f'domain_term:{term}'); terms.append(term)
    for verb in _cap_meta(cap,'task_verbs'):
        if verb.lower().replace(' ', '') in raw or verb.lower() in tokens:
            score+=15; features.append(f'task_verb:{verb}'); terms.append(verb)
    hay=_function_text(cap).lower()
    if any(token in hay for token in tokens if len(token)>2):
        score+=10; features.append('description_keyword_match')
    if _role_match(cap, role):
        score+=10; features.append('role_match')
    if cap.input_schema: score+=10; features.append('schema_input_match')
    if cap.output_schema: score+=10; features.append('schema_output_match')
    score += max(0.0, min(float(getattr(cap,'preference_score',0.0) or 0.0), 1.0))*20
    score += max(0.0, min(float(getattr(cap,'tool_quality_score',0.0) or 0.0), 1.0))*20
    score += max(0.0, min(float(getattr(cap,'structured_output_score',0.0) or 0.0), 1.0))*20
    for neg in _cap_meta(cap,'negative_tags'):
        if neg.lower() in tokens or neg.lower().replace(' ', '') in raw:
            score-=30; features.append(f'negative_tag:{neg}')
    return score, features, sorted(set(terms)), '; '.join(features[:6])

def explore_tool_pool(
    *,
    user_request: str = "",
    blueprint_text: str = "",
    file_specs: list[dict[str, Any]] | None = None,
    uploaded_files: list[dict[str, Any]] | None = None,
    current_tool_pool: Any = None,
    available_tool_registry: Any = None,
    missing_tool_configs: Any = None,
) -> ToolPoolExplorationResult:
    uploaded_files = [
        u.model_dump(mode="json") if hasattr(u, "model_dump") else dict(u)
        for u in (uploaded_files or [])
        if isinstance(u, dict) or hasattr(u, "model_dump")
    ]
    registry = list(available_tool_registry or list_tool_capabilities())
    uploaded_candidate_tools, triggers = _candidate_tools_from_uploaded(uploaded_files)

    scored: list[dict[str, Any]] = []
    requests: list[ToolPoolAddToolRequest] = []

    for spec in file_specs or []:
        target = str(spec.get("path") or spec.get("target_file") or "").replace("\\", "/")
        if not target.startswith("scripts/"):
            continue

        text = "\n".join([
            user_request,
            blueprint_text,
            " ".join(_texts(spec)),
            " ".join(_texts(uploaded_files)),
        ])
        tokens = _normalize_tokens(text)
        exts = _file_exts(spec) | _file_exts(uploaded_files)
        outputs = [str(x) for x in _texts(spec.get("outputs") or spec.get("output_schema") or {})]

        # role is retained only as a weak scoring hint, not as the source of truth.
        role = str(spec.get("role") or "generic_script")

        # Concrete tool IDs are the source of truth.
        selected = _tool_ids_from_spec(spec)

        raw_slots = _as_list(spec.get("required_tool_slots"))
        slots: set[str] = set()
        for item in raw_slots:
            if isinstance(item, dict):
                value = (
                    item.get("tool_id")
                    or item.get("candidate_tool_id")
                    or item.get("capability")
                    or item.get("capability_id")
                    or item.get("name")
                )
                if value:
                    slots.add(str(value).strip())
            else:
                text_item = str(item or "").strip()
                if text_item:
                    slots.add(text_item)

        candidates: list[dict[str, Any]] = []

        for cap in registry:
            score, features, terms, reason = score_tool_for_file_request(
                cap,
                text=text,
                tokens=tokens,
                exts=exts,
                outputs=outputs,
                role=role,
                selected_tools=selected,
                required_slots=slots,
                uploaded_candidate_tools=uploaded_candidate_tools,
            )

            if score <= 0 and cap.name not in selected and cap.name not in uploaded_candidate_tools:
                continue

            row = {
                "target_file": target,
                "tool_id": cap.name,
                "score": score,
                "matched_features": features,
                "matched_terms": terms,
                "semantic_reason": reason,
                "candidate_source": "semantic_registry",
            }
            candidates.append(row)

        candidates.sort(key=lambda r: r["score"], reverse=True)

        # Keep top candidates but always include explicit selected/uploaded tools.
        keep: list[dict[str, Any]] = []
        for row in candidates:
            if (
                len(keep) < 3
                or row["tool_id"] in selected
                or row["tool_id"] in uploaded_candidate_tools
            ):
                keep.append(row)

        # Concrete selected tool IDs should form their own capability group when present.
        # This prevents unrelated top candidates from competing under an abstract role name.
        if selected:
            file_capability_group = "required_tools"
        elif keep:
            top_terms = [t for t in (keep[0].get("matched_terms") or []) if t]
            file_capability_group = "_".join(top_terms[:2]) if top_terms else keep[0]["tool_id"]
        else:
            file_capability_group = target.replace("/", "_")

        for rank, row in enumerate(keep, 1):
            row["rank"] = rank
            scored.append(row)
            requests.append(
                ToolPoolAddToolRequest(
                    target_file=target,
                    requested_capability=file_capability_group,
                    candidate_tool_id=row["tool_id"],
                    source="registry_exploration",
                    reason=row["semantic_reason"],
                    confidence=min(1.0, row["score"] / 100.0),
                    score=row["score"],
                    matched_features=row["matched_features"],
                    matched_terms=row["matched_terms"],
                    rank=rank,
                    candidate_source=row["candidate_source"],
                    semantic_reason=row["semantic_reason"],
                )
            )

    return ToolPoolExplorationResult(
        candidate_tool_requests=requests,
        scored_candidates=scored,
        uploaded_file_triggers=triggers,
        confidence=0.85,
        reason="semantic registry recall and concrete tool-id scoring",
    )
