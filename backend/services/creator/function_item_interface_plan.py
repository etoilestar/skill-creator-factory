"""FunctionItem Interface Intent Plan protocol for Creator graph planning.

The Blueprint already decomposes the complete system into frozen executable
FunctionItems. This module plans and validates only semantic interaction intents
between those already-frozen FunctionItems and the platform; it does not create
another subsystem decomposition or infer business semantics.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..skill_plan import GraphValidationError, normalize_structured_function_items

logger = logging.getLogger(__name__)
ModelCall = Callable[[list[dict[str, str]], str], Awaitable[str]]
INTERFACE_KINDS = {"platform_to_member", "member_to_member", "member_to_platform"}
PROTOCOL_REPAIRABLE_CODES = {
    "invalid_interface_plan_json",
    "invalid_interface_plan_protocol",
    "invalid_interface_protocol",
    "invalid_interface_kind",
    "duplicate_interface_id",
}
INTERFACE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["interfaces"],
    "properties": {
        "interfaces": {
            "type": "array",
            "items": {
                "oneOf": [
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["interface_id", "kind", "goal", "target_member"],
                        "properties": {
                            "interface_id": {"type": "string", "minLength": 1},
                            "kind": {"const": "platform_to_member"},
                            "goal": {"type": "string", "minLength": 1},
                            "target_member": {"type": "string", "minLength": 1},
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["interface_id", "kind", "goal", "source_member", "target_member"],
                        "properties": {
                            "interface_id": {"type": "string", "minLength": 1},
                            "kind": {"const": "member_to_member"},
                            "goal": {"type": "string", "minLength": 1},
                            "source_member": {"type": "string", "minLength": 1},
                            "target_member": {"type": "string", "minLength": 1},
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["interface_id", "kind", "goal", "source_member"],
                        "properties": {
                            "interface_id": {"type": "string", "minLength": 1},
                            "kind": {"const": "member_to_platform"},
                            "goal": {"type": "string", "minLength": 1},
                            "source_member": {"type": "string", "minLength": 1},
                        },
                    },
                ]
            },
        }
    },
}


class InterfaceIntentPlanError(GraphValidationError):
    """Machine-readable Interface Intent Plan failure."""



def _strip_single_json_fence(text: str) -> str:
    stripped = str(text or "").strip()
    if not stripped.startswith("```") or not stripped.endswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 3 or lines[0].strip().lower() not in {"```", "```json"} or lines[-1].strip() != "```":
        return stripped
    return "\n".join(lines[1:-1]).strip()


def _parse_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(_strip_single_json_fence(text))
    except (TypeError, json.JSONDecodeError) as exc:
        raise InterfaceIntentPlanError("interface plan must be strict JSON", code="invalid_interface_plan_json", details={"path": "$"}) from exc
    if not isinstance(value, dict):
        raise InterfaceIntentPlanError("interface plan must be a JSON object", code="invalid_interface_plan_protocol", details={"path": "$"})
    return value


def _require_nonempty_string(value: dict[str, Any], key: str, code: str, path: str) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise InterfaceIntentPlanError(f"{key} must be a non-empty string", code=code, details={"path": path})
    return raw.strip()


def _raise(message: str, code: str, *, path: str, **details: Any) -> None:
    payload = {"path": path, **{k: v for k, v in details.items() if v is not None}}
    raise InterfaceIntentPlanError(message, code=code, details=payload)


def _compact_port_id(value: Any) -> str:
    if isinstance(value, dict):
        return str(
            value.get("port_id")
            or value.get("id")
            or value.get("name")
            or value.get("field")
            or ""
        ).strip()
    return str(value or "").strip()


def _compact_function_items(function_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = normalize_structured_function_items(function_items, source="interface_intent_plan")
    compact_items: list[dict[str, Any]] = []
    for item in normalized:
        default_values = item.get("default_values") if isinstance(item.get("default_values"), dict) else {}
        raw_inputs = item.get("inputs") or []
        input_ids = [_compact_port_id(raw_input) for raw_input in raw_inputs]
        compact_inputs = []
        for raw_input, port_id in zip(raw_inputs, input_ids):
            if not port_id:
                continue
            declared_required = raw_input.get("required") if isinstance(raw_input, dict) else None
            default_present = port_id in default_values or (
                isinstance(raw_input, dict) and "default" in raw_input
            )
            compact_input = {"name": port_id, "default_present": default_present}
            if isinstance(declared_required, bool):
                compact_input["required"] = declared_required
            else:
                compact_input["required"] = not default_present
            compact_inputs.append(compact_input)
        required_inputs = [
            value["name"] for value in compact_inputs
            if value["required"] and not value["default_present"]
        ]
        defaulted_inputs = [value["name"] for value in compact_inputs if value["default_present"]]
        compact_items.append(
            {
                "target_file": item["target_file"],
                "purpose": item.get("purpose", ""),
                "inputs": compact_inputs,
                "outputs": [_compact_port_id(value) for value in item.get("outputs") or [] if _compact_port_id(value)],
                "required_inputs": required_inputs,
                "defaulted_inputs": defaulted_inputs,
            }
        )
    return compact_items


def validate_interface_plan_protocol(plan: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize only the wire protocol, not member semantics."""
    if not isinstance(plan, dict) or set(plan) != {"interfaces"}:
        _raise("interface plan must contain only interfaces", "invalid_interface_plan_protocol", path="$")
    interfaces = plan.get("interfaces")
    if not isinstance(interfaces, list):
        _raise("interfaces must be a list", "invalid_interface_plan_protocol", path="$.interfaces")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(interfaces):
        path = f"$.interfaces[{index}]"
        if not isinstance(raw, dict):
            _raise("interface must be an object", "invalid_interface_protocol", path=path)
        kind = raw.get("kind")
        if kind not in INTERFACE_KINDS:
            _raise("interface kind is invalid", "invalid_interface_kind", path=f"{path}.kind")
        expected = ({"interface_id", "kind", "goal", "target_member"}
                    if kind == "platform_to_member" else
                    {"interface_id", "kind", "goal", "source_member", "target_member"}
                    if kind == "member_to_member" else
                    {"interface_id", "kind", "goal", "source_member"})
        if set(raw) != expected:
            _raise("interface fields do not match kind schema", "invalid_interface_protocol", path=path,
                   expected=sorted(expected), observed=sorted(raw))
        interface_id = _require_nonempty_string(raw, "interface_id", "invalid_interface_protocol", f"{path}.interface_id")
        if interface_id in seen:
            _raise("duplicate interface_id", "duplicate_interface_id", path=f"{path}.interface_id")
        seen.add(interface_id)
        _require_nonempty_string(raw, "goal", "invalid_interface_protocol", f"{path}.goal")
        for field in expected - {"interface_id", "kind", "goal"}:
            _require_nonempty_string(raw, field, "invalid_interface_protocol", f"{path}.{field}")
        normalized.append(dict(raw))
    return {"interfaces": normalized}


def validate_interface_intent_plan(*, plan: dict[str, Any], function_items: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate interface-intent protocol and references only."""
    plan = validate_interface_plan_protocol(plan)
    if not isinstance(plan, dict) or set(plan) != {"interfaces"}:
        _raise("interface plan must contain only interfaces", "invalid_interface_plan_protocol", path="$")
    interfaces = plan.get("interfaces")
    if not isinstance(interfaces, list):
        _raise("interfaces must be a list", "invalid_interface_plan_protocol", path="$.interfaces")
    frozen_targets = {item["target_file"] for item in normalize_structured_function_items(function_items, source="interface_intent_plan")}
    seen_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    counts = {"platform_to_member": 0, "member_to_member": 0, "member_to_platform": 0}
    for index, raw_interface in enumerate(interfaces):
        path = f"$.interfaces[{index}]"
        if not isinstance(raw_interface, dict):
            _raise("interface must be an object", "invalid_interface_protocol", path=path)
        kind = raw_interface.get("kind")
        if kind not in INTERFACE_KINDS:
            _raise("interface kind is invalid", "invalid_interface_kind", path=f"{path}.kind")
        expected = {"interface_id", "kind", "goal", "target_member"} if kind == "platform_to_member" else {"interface_id", "kind", "goal", "source_member", "target_member"} if kind == "member_to_member" else {"interface_id", "kind", "goal", "source_member"}
        if set(raw_interface) != expected:
            _raise("interface fields do not match kind schema", "invalid_interface_protocol", path=path, expected=sorted(expected), observed=sorted(raw_interface))
        interface_id = _require_nonempty_string(raw_interface, "interface_id", "invalid_interface_protocol", f"{path}.interface_id")
        if interface_id in seen_ids:
            _raise("duplicate interface_id", "duplicate_interface_id", path=f"{path}.interface_id", interface_id=interface_id)
        seen_ids.add(interface_id)
        _require_nonempty_string(raw_interface, "goal", "invalid_interface_protocol", f"{path}.goal")
        interface = dict(raw_interface)
        if kind in {"member_to_member", "member_to_platform"}:
            source = _require_nonempty_string(raw_interface, "source_member", "invalid_interface_member", f"{path}.source_member")
            if source not in frozen_targets:
                _raise("source_member must reference a frozen FunctionItem", "unknown_interface_member", path=f"{path}.source_member", target=source)
        if kind in {"platform_to_member", "member_to_member"}:
            target = _require_nonempty_string(raw_interface, "target_member", "invalid_interface_member", f"{path}.target_member")
            if target not in frozen_targets:
                _raise("target_member must reference a frozen FunctionItem", "unknown_interface_member", path=f"{path}.target_member", target=target)
        if kind == "member_to_member" and interface["source_member"] == interface["target_member"]:
            _raise("member_to_member self connection is forbidden", "interface_self_connection", path=path, target=interface["source_member"])
        counts[kind] += 1
        normalized.append(interface)
    logger.info(
        "[Creator][interface_plan] interface_count=%d platform_input_interface_count=%d member_interface_count=%d platform_output_interface_count=%d",
        len(normalized), counts["platform_to_member"], counts["member_to_member"], counts["member_to_platform"],
    )
    return {"interfaces": normalized}


def collect_interface_plan_validation_issues(
    *,
    plan: dict[str, Any],
    function_items: list[dict[str, Any]],
    platform_contract: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Collect semantic reference issues without guessing their correction.

    This function deliberately assumes the protocol layer has already accepted
    ``plan``.  The dynamic scopes in the returned diagnostics constrain the
    next model call; their ordering never implies a preferred semantic answer.
    """
    del platform_contract  # Graph expansion owns endpoint-level coverage.
    compact_items = _compact_function_items(function_items)
    allowed_members = [item["target_file"] for item in compact_items]
    allowed_member_set = set(allowed_members)
    port_ids = {
        _compact_port_id(port)
        for item in compact_items
        for port in [*(item["inputs"] or []), *(item["outputs"] or [])]
        if _compact_port_id(port)
    }
    issues: list[dict[str, Any]] = []

    def add(*, code: str, category: str, path: str, interface_id: str,
            message: str, observed: Any, constraint: dict[str, Any],
            details: dict[str, Any] | None = None) -> None:
        issues.append({
            "code": code,
            "category": category,
            "stage": "interface_plan_validation",
            "path": path,
            "interface_id": interface_id,
            "message": message,
            "observed_value": observed,
            "expected_constraint": constraint,
            "allowed_scope": list(allowed_members),
            "details": details or {},
        })

    for index, interface in enumerate(plan.get("interfaces") or []):
        interface_id = str(interface.get("interface_id") or "")
        kind = interface.get("kind")
        path = f"$.interfaces[{index}]"
        for field in ("source_member", "target_member"):
            if field not in interface:
                continue
            observed = interface[field]
            if observed not in allowed_member_set:
                category = "reference_scope_error" if observed in port_ids else "reference_error"
                add(
                    code="unknown_interface_member", category=category,
                    path=f"{path}.{field}", interface_id=interface_id,
                    message=f"{field} must reference a frozen FunctionItem identity",
                    observed=observed,
                    constraint={"type": "reference", "scope": "frozen_function_items"},
                    details={"member_field": field, "kind": kind},
                )
        if (kind == "member_to_member"
                and interface.get("source_member") == interface.get("target_member")
                and interface.get("source_member") in allowed_member_set):
            add(
                code="interface_self_connection", category="direction_error",
                path=path, interface_id=interface_id,
                message="member_to_member interfaces cannot connect a member to itself",
                observed=interface.get("source_member"),
                constraint={"type": "relationship", "rule": "distinct_members"},
            )
    return issues


def merge_interface_validation_issues(
    *issue_groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Stably combine diagnostics without interpreting or rewriting them."""
    merged: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any, Any]] = set()
    for group in issue_groups:
        for issue in group:
            key = (
                issue.get("code"), issue.get("category"),
                issue.get("interface_id"), issue.get("path"),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(issue)
    return merged


def _resolve_system_requirements_context(
    *, requirement_allocations: list[dict[str, Any]] | None,
    explicit_system_requirements: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Resolve system context once while preserving an explicit empty list."""
    if explicit_system_requirements is not None:
        return list(explicit_system_requirements)
    return [
        allocation for allocation in (requirement_allocations or [])
        if not (allocation.get("owners") or [])
    ]


def build_interface_repair_scope(
    validation_issues: list[dict[str, Any]],
    current_interface_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive modification permissions from issue shape, never an answer."""
    affected_ids: list[str] = []
    removable_ids: list[str] = []
    affected_members: list[str] = []
    allow_add = False
    allow_remove = False
    for issue in validation_issues:
        details = issue.get("details") if isinstance(issue.get("details"), dict) else {}
        interface_ids = details.get("affected_interfaces") or (
            [issue.get("interface_id")] if issue.get("interface_id") else []
        )
        for interface_id in interface_ids:
            interface_id = str(interface_id or "").strip()
            if interface_id and interface_id not in affected_ids:
                affected_ids.append(interface_id)
        for member in details.get("affected_members") or []:
            member = str(member or "").strip()
            if member and member not in affected_members:
                affected_members.append(member)
        for raw in details.get("affected_inputs") or []:
            member = str(raw.get("target_member") or "").strip() if isinstance(raw, dict) else ""
            if member and member not in affected_members:
                affected_members.append(member)
        for raw in details.get("uncovered_inputs") or []:
            member = str(raw.get("target") or "").strip() if isinstance(raw, dict) else ""
            if member and member not in affected_members:
                affected_members.append(member)
        category = str(issue.get("category") or details.get("category") or "other")
        category = {
            "coverage_error": "coverage", "cardinality_error": "atomicity",
            "semantic_alignment_error": "alignment", "reference_error": "reference",
            "reference_scope_error": "reference", "direction_error": "alignment",
        }.get(category, category)
        if category == "coverage":
            allow_add = True
        if category == "atomicity":
            allow_add = True
            allow_remove = True
            for interface_id in interface_ids:
                if interface_id and interface_id not in removable_ids:
                    removable_ids.append(interface_id)
    for interface in (current_interface_plan or {}).get("interfaces") or []:
        if not isinstance(interface, dict):
            continue
        if (interface.get("source_member") in affected_members
                or interface.get("target_member") in affected_members):
            interface_id = str(interface.get("interface_id") or "").strip()
            if interface_id and interface_id not in affected_ids:
                affected_ids.append(interface_id)
    return {
        "affected_interface_ids": affected_ids,
        "affected_members": affected_members,
        "removable_interface_ids": removable_ids,
        "allow_modify_interfaces": True,
        "allow_add_interfaces": allow_add,
        "allow_remove_interfaces": allow_remove,
        "preserve_unaffected_interfaces": True,
    }


def validate_interface_repair_scope(
    *, before: dict[str, Any], after: dict[str, Any], repair_scope: dict[str, Any]
) -> None:
    """Validate a semantic repair's diff without deciding semantic correctness."""
    before_list = before.get("interfaces") or []
    after_list = after.get("interfaces") or []
    before_by_id = {value.get("interface_id"): value for value in before_list}
    after_by_id = {value.get("interface_id"): value for value in after_list}
    if len(after_by_id) != len(after_list):
        _raise("interface_id must remain unique", "interface_repair_scope_error", path="$.interfaces")
    affected = set(repair_scope.get("affected_interface_ids") or [])
    additions = [key for key in after_by_id if key not in before_by_id]
    removals = [key for key in before_by_id if key not in after_by_id]
    removable = set(repair_scope.get("removable_interface_ids") or [])
    unexpected_removals = [key for key in removals if key not in removable]
    changed_unaffected = [
        key for key, value in before_by_id.items()
        if key in after_by_id and value != after_by_id[key] and key not in affected
    ]
    reordered = [value.get("interface_id") for value in after_list if value.get("interface_id") in before_by_id] != [
        value.get("interface_id") for value in before_list if value.get("interface_id") in after_by_id
    ]
    forbidden_fields = {
        key for value in after_list for key in value
        if key not in {"interface_id", "kind", "goal", "source_member", "target_member"}
    }
    invalid = (
        (additions and not repair_scope.get("allow_add_interfaces"))
        or (removals and not repair_scope.get("allow_remove_interfaces"))
        or unexpected_removals
        or changed_unaffected or reordered or forbidden_fields
        or (before_list and not after_list)
    )
    if invalid:
        logger.info(
            "[Creator][interface_repair_scope_failure] changed_unaffected_interfaces=%s unexpected_additions=%s unexpected_removals=%s",
            changed_unaffected, additions, removals,
        )
        _raise(
            "interface semantic repair exceeded its allowed scope",
            "interface_repair_scope_error", path="$.interfaces",
            changed_unaffected_interfaces=changed_unaffected,
            unexpected_additions=additions if not repair_scope.get("allow_add_interfaces") else [],
            unexpected_removals=(removals if not repair_scope.get("allow_remove_interfaces") else unexpected_removals),
            reordered=reordered, forbidden_fields=sorted(forbidden_fields),
        )


def build_graph_obligations_from_interfaces(*, interface_plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert declared interface intents to local graph obligations."""
    obligations: list[dict[str, Any]] = []
    for interface in interface_plan.get("interfaces") or []:
        obligation = {
            "obligation_id": f"O{len(obligations) + 1:04d}",
            "interface_id": interface["interface_id"],
            "goal": interface["goal"],
        }
        if interface["kind"] == "platform_to_member":
            obligation.update({"kind": "platform_to_script", "target_member": interface["target_member"]})
        elif interface["kind"] == "member_to_member":
            obligation.update({"kind": "script_to_script", "source_member": interface["source_member"], "target_member": interface["target_member"]})
        else:
            obligation.update({"kind": "script_to_platform", "source_member": interface["source_member"]})
        obligations.append(obligation)
    return obligations


def _interface_plan_prompt() -> str:
    return f"""
1. AUTHORITATIVE FACTS
The user payload contains only the complete system goal, frozen FunctionItems,
the platform contract, and frozen requirement allocations. target_file values
are the complete legal member domain. FunctionItem inputs and outputs are frozen.

2. TASK
Plan the minimum complete set of atomic runtime transfers required to satisfy
every required target input and every required platform output.

ATOMIC INTERFACE DEFINITION
One Interface Intent represents exactly one independently bindable runtime
transfer: one source value -> one target input. It is not a general relationship,
an entire member-to-member channel, a bundle of fields, all data exchanged
between members, or a workflow step containing multiple transfers.

You are planning semantic interfaces between already-frozen executable FunctionItems.
Produce the complete Interface Intent Plan. One Interface Intent represents one
independently bindable runtime transfer and must expand into exactly one graph
edge. A later binder materializes each Interface object as exactly one source
endpoint connected to exactly one target endpoint. For every frozen FunctionItem,
inspect each declared input independently.
For each input decide whether its runtime source is the platform, exactly one
upstream FunctionItem, a declared resource, or optional/default behavior. Create
an Interface Intent only when a runtime transfer is required.

For member_to_member, the goal must name exactly one source output, exactly one
target input, and the semantic reason for the transfer. If the same source member
provides three different outputs to the same target member, return three records.
Repeated source_member and target_member pairs are allowed when their transfer
goals differ; use multiple member_to_member Interfaces between the same
source_member and target_member. Source endpoints may fan out to multiple
independently bindable targets. Source outputs are reusable. Do not determine
the number of Interfaces from the number of source outputs. A structured value
transferred into one target input remains one logical transfer regardless of how
many internal fields it contains. required_inputs require incoming transfer
coverage; defaulted_inputs do not require an Interface unless semantics require
an override. Every goal must name or unambiguously describe exactly one source
value, exactly one target input, and why that value is required by that target.
Split independently bindable values into separate intents. Repeated member pairs
are expected and valid; never merge transfers merely because the pair is equal.

PLANNING PROCEDURE
Silently enumerate every frozen input. Ignore only required=false or
default_present=true inputs. For each remaining input, semantically choose one
upstream FunctionItem output or one platform runtime input and emit exactly one
intent. Independently enumerate required final platform outputs and emit exactly
one member_to_platform intent for each. Verify every required target input has
exactly one intended transfer unless multiple producers are explicitly required,
and verify no goal bundles multiple target inputs.

3. INVARIANTS
- Do not add, remove, rename, merge, split, or modify frozen FunctionItems.
- source_member and target_member must exactly copy legal target_file values.
- A required input without a default needs a runtime source.
- An optional input or input with a valid default does not require an Interface
  merely for completeness.
- Do not combine multiple independently required target inputs into one Interface goal.
- Do not invent a platform input merely because an upstream transfer was missed.
- Do not connect members by matching field names alone; use responsibilities,
  declared ports, workflow semantics, requirements, and platform contract together.
- A required input with no semantic upstream producer must be considered for a
  platform_to_member intent, including the first workflow member. Do not invent
  a platform input when a valid upstream producer exists.
- A final output is not implied by the final FunctionItem's existence; explicitly
  emit every required member_to_platform transfer.
- Do not return endpoint IDs, port IDs, edges, paths, comments, or extra fields.
- Do not reproduce, quote, summarize, or copy these instructions into the result.
- Do not include planning notes, explanations, Markdown fences, comments, or
  hidden reasoning.

4. FINAL SELF-CHECK
Before returning, silently inspect every target FunctionItem input:
- required runtime inputs have a supporting Interface;
- optional/defaultable inputs are not treated as mandatory;
- no required upstream member relation is omitted;
- no Interface combines multiple independently bindable transfers;
- repeated member pairs and fan-out transfers remain separate;
- no Interface introduces an undeclared member;
- no intermediate output is incorrectly returned to the platform;
- only true final outputs have member_to_platform intent.

5. OUTPUT CONTRACT
Return only the requested JSON object matching this schema:
{json.dumps(INTERFACE_SCHEMA, ensure_ascii=False)}
""".strip()


async def _reformat_interface_plan_response(*, raw_response: str, validation_error: InterfaceIntentPlanError, planner_model: str, model_call: ModelCall) -> dict[str, Any]:
    logger.info("[Creator][interface_protocol_repair] attempt=1 error_paths=%s", [validation_error.details.get("path", "$")])
    prompt = "Return only a corrected JSON object that matches the supplied schema. Preserve the number, order, direction, goals, and member references of all interfaces. You may change interface_id values only as needed to make non-empty IDs unique. Do not reinterpret the Blueprint, add or remove semantic interfaces, or add semantic conclusions; only repair protocol shape and ID uniqueness."
    payload = {"schema": INTERFACE_SCHEMA, "raw_response": raw_response, "validation_error": {"code": validation_error.code, "details": validation_error.details, "message": str(validation_error)}}
    text = await model_call([{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}], planner_model)
    return _parse_object(text)


INTERFACE_REVIEW_CATEGORIES = {"coverage", "atomicity", "alignment", "reference", "other"}
INTERFACE_REVIEW_ISSUE_FIELDS = {
    "code", "category", "message", "affected_interfaces", "affected_members",
    "affected_inputs", "evidence",
}
INTERFACE_REVIEW_SCHEMA = {
    "passed": "boolean",
    "issues": [{
        "code": "string", "category": "coverage|atomicity|alignment|reference|other",
        "message": "string", "affected_interfaces": ["string"],
        "affected_members": ["string"],
        "affected_inputs": [{"target_member": "string", "target_input": "string"}],
        "evidence": "object",
    }],
}


def normalize_interface_review_issue(
    raw_issue: dict[str, Any], frozen_function_items: list[dict[str, Any]],
    current_interface_plan: dict[str, Any], *, path: str = "$.issues[]",
) -> dict[str, Any]:
    """Validate one open issue envelope using reference existence only."""
    if not isinstance(raw_issue, dict) or set(raw_issue) != INTERFACE_REVIEW_ISSUE_FIELDS:
        _raise("semantic review issue has invalid shape", "invalid_interface_semantic_review_protocol", path=path)
    code = str(raw_issue.get("code") or "").strip()
    category = str(raw_issue.get("category") or "").strip()
    message = str(raw_issue.get("message") or "").strip()
    affected_interfaces = raw_issue.get("affected_interfaces")
    affected_members = raw_issue.get("affected_members")
    affected_inputs = raw_issue.get("affected_inputs")
    evidence = raw_issue.get("evidence")
    if (not code or category not in INTERFACE_REVIEW_CATEGORIES or not message
            or not isinstance(affected_interfaces, list)
            or not all(isinstance(value, str) and value.strip() for value in affected_interfaces)
            or not isinstance(affected_members, list)
            or not all(isinstance(value, str) and value.strip() for value in affected_members)
            or not isinstance(affected_inputs, list)
            or not isinstance(evidence, dict) or not evidence):
        _raise("semantic review issue is not auditable", "invalid_interface_semantic_review_protocol", path=path)

    known_interfaces = {
        str(interface.get("interface_id") or "")
        for interface in current_interface_plan.get("interfaces") or []
    }
    compact_items = _compact_function_items(frozen_function_items)
    inputs_by_member = {
        item["target_file"]: {value["name"] for value in item.get("inputs") or []}
        for item in compact_items
    }
    if any(value not in known_interfaces for value in affected_interfaces):
        _raise("review issue references an unknown interface", "invalid_interface_semantic_review_reference", path=f"{path}.affected_interfaces")
    if any(value not in inputs_by_member for value in affected_members):
        _raise("review issue references an unknown member", "invalid_interface_semantic_review_reference", path=f"{path}.affected_members")
    normalized_inputs: list[dict[str, str]] = []
    for input_index, value in enumerate(affected_inputs):
        input_path = f"{path}.affected_inputs[{input_index}]"
        if not isinstance(value, dict) or set(value) != {"target_member", "target_input"}:
            _raise("affected input has invalid shape", "invalid_interface_semantic_review_protocol", path=input_path)
        member = str(value.get("target_member") or "").strip()
        target_input = str(value.get("target_input") or "").strip()
        if member not in inputs_by_member or target_input not in inputs_by_member[member]:
            _raise("review issue references an unknown declared input", "invalid_interface_semantic_review_reference", path=input_path)
        normalized_inputs.append({"target_member": member, "target_input": target_input})

    envelope = {
        "code": code, "category": category, "message": message,
        "affected_interfaces": list(affected_interfaces),
        "affected_members": list(affected_members),
        "affected_inputs": normalized_inputs, "evidence": dict(evidence),
    }
    return {
        **envelope, "stage": "interface_semantic_review", "path": path,
        "interface_id": affected_interfaces[0] if affected_interfaces else "",
        "details": envelope,
    }


def interface_issue_fingerprint(issue: dict[str, Any]) -> tuple[Any, ...]:
    details = issue.get("details") if isinstance(issue.get("details"), dict) else issue
    return (
        details.get("category") or issue.get("category"),
        tuple(sorted(details.get("affected_interfaces") or [])),
        tuple(sorted(details.get("affected_members") or [])),
        tuple(sorted(
            (str(value.get("target_member") or ""), str(value.get("target_input") or ""))
            for value in (details.get("affected_inputs") or []) if isinstance(value, dict)
        )),
    )


def _validate_interface_review_response(
    *, value: dict[str, Any], interface_plan: dict[str, Any],
    frozen_function_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if (set(value) != {"passed", "issues"} or not isinstance(value.get("passed"), bool)
            or not isinstance(value.get("issues"), list)):
        _raise("semantic review response has invalid shape", "invalid_interface_semantic_review_protocol", path="$")
    issues = [
        normalize_interface_review_issue(raw, frozen_function_items, interface_plan, path=f"$.issues[{index}]")
        for index, raw in enumerate(value["issues"])
    ]
    if value["passed"] != (not issues):
        _raise("semantic review passed flag contradicts issues", "invalid_interface_semantic_review_protocol", path="$.passed")
    return issues


async def _reformat_interface_review_response(
    *, raw_response: str, validation_error: InterfaceIntentPlanError,
    reviewer_model: str, model_call: ModelCall,
) -> dict[str, Any]:
    prompt = """Repair only the JSON protocol shape.
Preserve every semantic conclusion, issue code, category, message, affected
reference, and evidence.
Do not add, remove, merge, split, or reinterpret issues.
Every issue must use the single supplied issue schema, regardless of its code.
Return only the corrected JSON object."""
    payload = {
        "review_schema": INTERFACE_REVIEW_SCHEMA,
        "raw_response": raw_response,
        "validation_error": {
            "code": validation_error.code, "message": str(validation_error),
            "details": validation_error.details,
        },
    }
    logger.info("[Creator][interface_semantic_review_protocol_repair] attempt=1")
    text = await model_call(
        [{"role": "system", "content": prompt},
         {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
        reviewer_model,
    )
    return _parse_object(text)


async def review_interface_plan_semantically(
    *, original_user_goal: str, frozen_function_items: list[dict[str, Any]],
    interface_plan: dict[str, Any], requirement_allocations: list[dict[str, Any]] | None,
    requirement_channels: dict[str, str] | None,
    system_requirements: list[dict[str, Any]] | None,
    platform_contract: dict[str, Any] | None, reviewer_model: str,
    model_call: ModelCall,
) -> list[dict[str, Any]]:
    """Ask once for semantic diagnostics; never ask the reviewer for a repair."""
    prompt = """1. AUTHORITATIVE FACTS
The payload contains frozen FunctionItems with declared input required/default
facts, the current Interface Intent Plan, frozen allocations, and platform contract.

2. TASK
Review an Interface Intent Plan against the complete supplied system semantics.
You are reviewing only the Interface Intent layer. Build an internal coverage
checklist from frozen target inputs. For each required target input, determine
whether one Interface Intent clearly describes its runtime source and transfer.
Review semantic coverage, not only natural-language direction.

Do not pass merely because every member participates in an Interface. Member-level
connectivity is not sufficient. Review target-input coverage. Silently construct
a table of target_member, target_input, required, default_present, and
covering_interface_ids. Every required input without a default normally needs
exactly one semantically valid covering Interface. Zero is missing coverage; an
Interface bundling independently bindable inputs is non-atomic; the correct member
pair is insufficient unless its goal describes the specific required transfer.

An Interface Intent declares only:
- whether data flows from the platform to a frozen FunctionItem;
- from one frozen FunctionItem to another;
- or from a frozen FunctionItem to the platform;
- and the independent semantic responsibility of that transfer.

This stage intentionally does not bind concrete input fields, output fields,
ports, endpoint IDs, stdout keys, platform slots, source paths, argv fields,
or placeholder expressions.

Those details are assigned later by Endpoint Binding and Graph Expansion.
Their absence from an Interface Intent is correct and must not be reported
as a semantic issue.

Review only the following Interface Intent concerns:

1. kind alignment:
   Whether platform_to_member, member_to_member, or member_to_platform
   matches the high-level system workflow.
2. member direction alignment:
   Whether the declared source_member and target_member responsibilities
   are directionally consistent with the frozen FunctionItem purposes.
3. goal alignment:
   Whether the interface goal describes a transfer responsibility that is
   consistent with the declared source and target members.
4. independent transfer alignment:
   Whether independently required logical transfers have been incorrectly
   merged into one broad intent.
5. duplicate semantic responsibility:
   Whether multiple intents repeat the same direction and the same logical
   target responsibility.

Do not report an issue merely because an Interface Intent does not specify:
- source_field;
- target_field;
- source_id;
- target_id;
- input_id;
- output_id;
- port_id;
- stdout field names;
- argv keys;
- placeholder expressions;
- source_path;
- platform input envelope fields;
- platform final output fields;
- serialization details;
- runtime transport details.

Do not require member_to_member transfers to pass through a platform output slot.
A member_to_member Interface Intent represents direct logical data flow between
two FunctionItems. The later graph-binding stage selects the concrete source
output and target input.
A platform_to_member Interface Intent does not need to name a specific platform
input envelope field.
A member_to_platform Interface Intent does not need to name a specific platform
final output field.
The absence of those bindings is not ambiguity and is not a semantic
misalignment at this stage.

If the kind, member direction, and goal are semantically consistent, return
passed=true even when concrete endpoint or field bindings are not yet present.
Do not invent issues that can only be resolved by adding fields outside the
supplied Interface Intent schema.
Every reported issue must be solvable by modifying only one or more of:
- kind;
- goal;
- source_member;
- target_member.
If an alleged concern requires any other field, it is outside this review
stage and must not be reported.

Reject the plan when a required target input lacks a transfer, an Interface
combines independently bindable transfers, a member relation is missing, an
optional/default input is made mandatory without evidence, or a platform source
is invented without semantic evidence. Report violated facts only; never choose
a source member, output, endpoint, or repair.

For missing coverage use code missing_required_input_transfer, category coverage,
message "A required target input has no atomic Interface Intent.", the target in
affected_members and affected_inputs, affected_interfaces=[], and evidence with
observed "No Interface goal represents this target input." and expected "One
atomic source-value-to-target-input Interface Intent." For a bundled transfer use
code non_atomic_interface_transfer, category atomicity, list the Interface and
each affected input, and explain that one intent represents multiple independently
bindable transfers.

Check every required root input for a platform_to_member or valid upstream
transfer, every required final result for member_to_platform, every downstream
required input for coverage, and every Interface for atomicity. Any failure means
passed=false.

3. INVARIANTS
Every issue uses the same envelope regardless of code. code is a stable,
model-defined identifier. category must be one of coverage, atomicity, alignment,
reference, or other. Every location uses the affected_interfaces,
affected_members, and affected_inputs arrays; return [] when one is inapplicable.
evidence must be a non-empty object, including an explanation when all affected
arrays are empty. Do not reproduce these instructions or include reasoning notes.

4. FINAL SELF-CHECK
Silently verify every referenced Interface, member, and declared target input
exists in the authoritative facts, passed equals whether issues is empty, and
all issues use exactly the same envelope.

5. OUTPUT CONTRACT
Return only {"passed": boolean, "issues": [{"code": string, "category":
"coverage|atomicity|alignment|reference|other", "message": string,
"affected_interfaces": [string], "affected_members": [string],
"affected_inputs": [{"target_member": string, "target_input": string}],
"evidence": object}]}. Do not return the plan or graph endpoints.

Some interfaces may already contain deterministic reference or scope errors;
the backend reports those separately. Do not duplicate deterministic reference
diagnostics. Review only additional semantic alignment concerns supported by
the system goal, FunctionItem purposes, requirements, interface kind, and goal.
If an invalid or ambiguous reference prevents a supported conclusion, do not
invent an issue."""
    payload = {
        "system_goal": original_user_goal,
        "function_items": _compact_function_items(frozen_function_items),
        "current_interface_plan": interface_plan,
        "requirement_allocations": requirement_allocations or [],
        "requirement_channels": requirement_channels or {},
        "unowned_system_requirements": system_requirements or [],
        "platform_contract": platform_contract or {},
    }
    raw_response = await model_call(
            [{"role": "system", "content": prompt},
             {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
            reviewer_model,
        )
    try:
        issues = _validate_interface_review_response(
            value=_parse_object(raw_response), interface_plan=interface_plan,
            frozen_function_items=frozen_function_items,
        )
    except InterfaceIntentPlanError as original_exc:
        try:
            reformatted = await _reformat_interface_review_response(
                raw_response=raw_response, validation_error=original_exc,
                reviewer_model=reviewer_model, model_call=model_call,
            )
            issues = _validate_interface_review_response(
                value=reformatted, interface_plan=interface_plan,
                frozen_function_items=frozen_function_items,
            )
        except InterfaceIntentPlanError as repair_exc:
            logger.info("[Creator][interface_semantic_review] result=failed error_code=%s", repair_exc.code)
            raise InterfaceIntentPlanError(
                "interface semantic review failed", code="interface_semantic_review_failed",
                details={
                    "review_attempts": 1, "protocol_repair_attempts": 1,
                    "original_error": {"code": original_exc.code, "message": str(original_exc), "details": original_exc.details},
                    "repair_error": {"code": repair_exc.code, "message": str(repair_exc), "details": repair_exc.details},
                },
            ) from repair_exc
        logger.info("[Creator][interface_semantic_review_protocol_repair] attempt=1 result=success")
    except Exception:
        # Model transport failures remain transport failures, not protocol repair.
        raise
    logger.info("[Creator][interface_semantic_review] result=%s issue_count=%d", "passed" if not issues else "issues_found", len(issues))
    return issues


async def plan_function_item_interfaces(*, original_user_goal: str, frozen_function_items: list[dict[str, Any]], requirement_allocations: list[dict[str, Any]] | None = None, requirement_channels: dict[str, str] | None = None, system_requirements: list[dict[str, Any]] | None = None, interaction_requirements: list[dict[str, Any]] | None = None, platform_contract: dict[str, Any] | None = None, skill_name: str = "", planner_model: str, model_call: ModelCall, reviewer_model: str | None = None) -> dict[str, Any]:
    """Ask the model for interaction intents between frozen FunctionItems."""

    system_requirements_context = _resolve_system_requirements_context(
        requirement_allocations=requirement_allocations,
        explicit_system_requirements=(
            system_requirements if system_requirements is not None
            else interaction_requirements
        ),
    )
    payload = {
        "system_goal": original_user_goal,
        "skill_name": skill_name,
        "function_items": _compact_function_items(frozen_function_items),
        "executable_requirement_allocations": [
            allocation
            for allocation in (requirement_allocations or [])
            if requirement_channels and requirement_channels.get(str(allocation.get("requirement_id") or "")) == "executable"
        ],
        "requirement_channels": requirement_channels or {},
        "unowned_system_requirements": system_requirements_context,
        "platform_contract": platform_contract or {},
    }
    raw_response = await model_call([{"role": "system", "content": _interface_plan_prompt()}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}], planner_model)
    try:
        parsed = validate_interface_plan_protocol(_parse_object(raw_response))
        logger.info("[Creator][interface_protocol_validation] result=success")
    except InterfaceIntentPlanError as exc:
        logger.info("[Creator][interface_protocol_validation] result=failed error_code=%s", exc.code)
        if exc.code in PROTOCOL_REPAIRABLE_CODES:
            try:
                reformatted = await _reformat_interface_plan_response(raw_response=raw_response, validation_error=exc, planner_model=planner_model, model_call=model_call)
                parsed = validate_interface_plan_protocol(reformatted)
                logger.info("[Creator][interface_protocol_repair] attempt=1 result=success")
            except InterfaceIntentPlanError as repair_exc:
                logger.info("[Creator][interface_protocol_repair] attempt=1 result=failed")
                raise InterfaceIntentPlanError(
                    "interface plan protocol repair failed",
                    code="interface_plan_protocol_repair_failed",
                    details={
                        "original_error": {"code": exc.code, "details": exc.details},
                        "repair_error": {"code": repair_exc.code, "details": repair_exc.details},
                    },
                ) from repair_exc
        else:
            raise
    deterministic_issues = collect_interface_plan_validation_issues(
        plan=parsed, function_items=frozen_function_items, platform_contract=platform_contract,
    )
    review_issues: list[dict[str, Any]] = []
    if reviewer_model:
        review_issues = await review_interface_plan_semantically(
            original_user_goal=original_user_goal,
            frozen_function_items=frozen_function_items,
            interface_plan=parsed,
            requirement_allocations=requirement_allocations,
            requirement_channels=requirement_channels,
            system_requirements=system_requirements_context,
            platform_contract=platform_contract,
            reviewer_model=reviewer_model,
            model_call=model_call,
        )
    combined_issues = merge_interface_validation_issues(deterministic_issues, review_issues)
    logger.info(
        "[Creator][interface_validation] stage=initial deterministic_issue_count=%d review_issue_count=%d combined_issue_count=%d repairable=%s",
        len(deterministic_issues), len(review_issues), len(combined_issues), bool(combined_issues),
    )
    if combined_issues:
        return await repair_interface_plan_semantically(
            original_user_goal=original_user_goal,
            frozen_function_items=frozen_function_items,
            current_interface_plan=parsed,
            validation_issues=combined_issues,
            repair_scope=build_interface_repair_scope(combined_issues, parsed),
            requirement_allocations=requirement_allocations,
            requirement_channels=requirement_channels,
            system_requirements=system_requirements_context,
            platform_contract=platform_contract,
            skill_name=skill_name,
            planner_model=planner_model,
            model_call=model_call,
            reviewer_model=reviewer_model,
        )
    return validate_interface_intent_plan(plan=parsed, function_items=frozen_function_items)


async def repair_interface_plan_semantically(
    *, original_user_goal: str, frozen_function_items: list[dict[str, Any]],
    current_interface_plan: dict[str, Any], validation_issues: list[dict[str, Any]],
    repair_scope: dict[str, Any], requirement_allocations: list[dict[str, Any]] | None = None,
    requirement_channels: dict[str, str] | None = None,
    system_requirements: list[dict[str, Any]] | None = None,
    interaction_requirements: list[dict[str, Any]] | None = None,
    platform_contract: dict[str, Any] | None = None, skill_name: str = "",
    repair_stage: str = "initial_interface_validation",
    planner_model: str, model_call: ModelCall, reviewer_model: str | None = None,
) -> dict[str, Any]:
    """Perform one bounded Critic + Generator semantic repair cycle."""
    system_requirements_context = (
        list(system_requirements) if system_requirements is not None
        else _resolve_system_requirements_context(
            requirement_allocations=requirement_allocations,
            explicit_system_requirements=interaction_requirements,
        )
    )
    logger.info(
        "[Creator][interface_semantic_repair] stage=%s attempt=1 issue_count=%d affected_interface_count=%d allow_add=%s allow_remove=%s",
        repair_stage, len(validation_issues), len(repair_scope.get("affected_interface_ids") or []),
        bool(repair_scope.get("allow_add_interfaces")), bool(repair_scope.get("allow_remove_interfaces")),
    )

    critic_prompt = """Diagnose the supplied Interface Plan against the explicit
blocking facts. For every uncovered required input, identify one repair action.
For every non-atomic Interface, identify whether it must be split or modified.
Preserve every unaffected Interface. Do not produce the final Interface Plan.
Do not choose endpoint IDs. Do not infer relationships from names alone. Do not
omit an issue merely because the correct source requires semantic reasoning.
Return only {"diagnosis": string, "repair_actions": [actions]}. Allowed actions
are add_atomic_interface, split_interface, modify_interface, preserve_interface,
and remove_interface."""

    prompt = """1. AUTHORITATIVE FACTS
The payload contains compact frozen FunctionItems, the failed complete Interface
Plan, unified blocking issue envelopes, requirement allocations, the platform
contract, repair_scope, the legal member domain, and the legal Interface schema.

2. TASK
Apply the supplied repair actions and return one complete repaired Interface
Plan. For each add_atomic_interface action, use full system semantics to choose
the correct source member or platform, create exactly one independently bindable
transfer describing one source value and one target input, and assign a new
unique interface_id. For split_interface, replace the bundled Interface with
separate atomic intents, preserve its semantic transfers, and never merge them
again. Preserve all unaffected Interfaces exactly. Do not change FunctionItems
or return the unchanged failed plan.

3. INVARIANTS
- Do not modify FunctionItems or add fields outside the Interface schema.
- Do not infer relationships from filenames or matching field names alone.
- Do not invent platform inputs merely to close the graph.
- Preserve unaffected Interfaces byte-for-byte and in order.
- Repeated member pairs are allowed when transfers are independent.
- One Interface remains independently bindable to one graph edge.
- Add or remove Interfaces only when repair_scope permits.
- Returning an unchanged plan is invalid.
- Do not add endpoint IDs, port IDs, source paths, or graph edges.

4. FINAL SELF-CHECK
Silently verify every supplied blocking issue is addressed by a concrete
semantic change; every change is within scope; every member is in the frozen
domain; independent transfers are not merged; no optional/default input is made
mandatory without evidence; no platform source was invented only for closure;
and the plan differs meaningfully from the failed plan.

5. OUTPUT CONTRACT
Return only the complete Interface Plan JSON matching interface_schema. Do not
include explanations, Markdown, comments, or hidden reasoning.
"""
    payload = {
        "system_goal": original_user_goal,
        "skill_name": skill_name,
        "function_items": _compact_function_items(frozen_function_items),
        "current_interface_plan": current_interface_plan,
        "validation_issues": validation_issues,
        "legal_member_domain": [
            item["target_file"] for item in _compact_function_items(frozen_function_items)
        ],
        "requirement_allocations": requirement_allocations or [],
        "requirement_channels": requirement_channels or {},
        "unowned_system_requirements": system_requirements_context,
        "platform_contract": platform_contract or {},
        "repair_scope": repair_scope,
        "interface_schema": INTERFACE_SCHEMA,
    }
    critic_text = await model_call(
        [{"role": "system", "content": critic_prompt},
         {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
        planner_model,
    )
    critic = _parse_object(critic_text)
    if set(critic) != {"diagnosis", "repair_actions"} or not isinstance(critic.get("diagnosis"), str) or not isinstance(critic.get("repair_actions"), list):
        _raise("repair critic response has invalid shape", "invalid_interface_repair_critic_protocol", path="$")
    allowed_actions = {"add_atomic_interface", "split_interface", "modify_interface", "preserve_interface", "remove_interface"}
    legal_interfaces = {str(value.get("interface_id") or "") for value in current_interface_plan.get("interfaces") or []}
    compact = _compact_function_items(frozen_function_items)
    legal_inputs = {item["target_file"]: {value["name"] for value in item["inputs"]} for item in compact}
    for index, action in enumerate(critic["repair_actions"]):
        if not isinstance(action, dict) or action.get("action") not in allowed_actions:
            _raise("repair critic action is invalid", "invalid_interface_repair_critic_protocol", path=f"$.repair_actions[{index}]")
        if "interface_id" in action and action["interface_id"] not in legal_interfaces:
            _raise("repair action references an unknown interface", "invalid_interface_repair_critic_reference", path=f"$.repair_actions[{index}].interface_id")
        if "target_member" in action:
            member, target_input = action.get("target_member"), action.get("target_input")
            if member not in legal_inputs or target_input not in legal_inputs[member]:
                _raise("repair action references an unknown input", "invalid_interface_repair_critic_reference", path=f"$.repair_actions[{index}]")
    logger.info(
        "[Creator][interface_repair_critic] action_count=%d action_types=%s affected_interfaces=%s affected_inputs=%s",
        len(critic["repair_actions"]), [a.get("action") for a in critic["repair_actions"]],
        [a.get("interface_id") for a in critic["repair_actions"] if a.get("interface_id")],
        [{"target_member": a.get("target_member"), "target_input": a.get("target_input")} for a in critic["repair_actions"] if a.get("target_member")],
    )
    payload["repair_critic"] = critic
    text = await model_call([{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}], planner_model)
    try:
        candidate = validate_interface_plan_protocol(_parse_object(text))
        if candidate == current_interface_plan:
            raise InterfaceIntentPlanError(
                "interface repair made no progress", code="repair_no_progress",
                details={"stage": repair_stage, "original_issues": validation_issues},
            )
        validate_interface_repair_scope(before=current_interface_plan, after=candidate, repair_scope=repair_scope)
        remaining = collect_interface_plan_validation_issues(
            plan=candidate, function_items=frozen_function_items, platform_contract=platform_contract,
        )
        if remaining:
            raise InterfaceIntentPlanError(
                "semantic issues remain after repair", code="semantic_issues_remain",
                details={"remaining_issues": remaining},
            )
        if reviewer_model:
            after_issues = await review_interface_plan_semantically(
                original_user_goal=original_user_goal,
                frozen_function_items=frozen_function_items,
                interface_plan=candidate,
                requirement_allocations=requirement_allocations,
                requirement_channels=requirement_channels,
                system_requirements=system_requirements_context,
                platform_contract=platform_contract,
                reviewer_model=reviewer_model,
                model_call=model_call,
            )
            before_fingerprints = {interface_issue_fingerprint(issue) for issue in validation_issues}
            after_fingerprints = {interface_issue_fingerprint(issue) for issue in after_issues}
            if (after_fingerprints == before_fingerprints
                    or (len(after_issues) >= len(validation_issues) and after_issues)):
                raise InterfaceIntentPlanError(
                    "interface repair made no progress", code="repair_no_progress",
                    details={"stage": repair_stage, "original_issues": validation_issues,
                             "remaining_issues": after_issues},
                )
        result = validate_interface_intent_plan(plan=candidate, function_items=frozen_function_items)
        logger.info(
            "[Creator][interface_repair_result] plan_changed=%s before_issue_fingerprints=%s after_issue_fingerprints=%s remaining_uncovered_inputs=%s",
            candidate != current_interface_plan,
            [interface_issue_fingerprint(issue) for issue in validation_issues],
            [interface_issue_fingerprint(issue) for issue in (after_issues if reviewer_model else remaining)],
            [value for issue in (after_issues if reviewer_model else remaining) for value in ((issue.get("details") or {}).get("affected_inputs") or [])],
        )
    except InterfaceIntentPlanError as exc:
        logger.info(
            "[Creator][interface_semantic_repair] stage=%s attempt=1 result=failed error_code=%s",
            repair_stage, exc.code,
        )
        if exc.code == "repair_no_progress":
            raise
        raise InterfaceIntentPlanError(
            "interface semantic repair failed", code="interface_semantic_repair_failed",
            details={"stage": repair_stage, "repair_attempts": 1,
                     "original_issues": validation_issues,
                     "repair_error": {"code": exc.code, "message": str(exc), "details": exc.details}},
        ) from exc
    logger.info("[Creator][interface_semantic_repair] stage=%s attempt=1 result=candidate_valid", repair_stage)
    return result


async def repair_interface_intents(
    *, original_user_goal: str, frozen_function_items: list[dict[str, Any]],
    current_interface_plan: dict[str, Any], validation_errors: list[dict[str, Any]],
    affected_members: list[str] | None = None,
    missing_platform_output_fields: list[str] | None = None,
    requirement_allocations: list[dict[str, Any]] | None = None,
    requirement_channels: dict[str, str] | None = None,
    system_requirements: list[dict[str, Any]] | None = None,
    interaction_requirements: list[dict[str, Any]] | None = None,
    platform_contract: dict[str, Any] | None = None, skill_name: str = "",
    repair_stage: str = "graph_expansion_feedback",
    planner_model: str, model_call: ModelCall,
) -> dict[str, Any]:
    """Compatibility entry point routing all graph feedback to one repairer."""
    issues: list[dict[str, Any]] = []
    legal_members = {
        item["target_file"] for item in _compact_function_items(frozen_function_items)
    }
    legal_interfaces = {
        str(item.get("interface_id") or "")
        for item in current_interface_plan.get("interfaces") or []
    }
    uncovered_required_inputs: list[dict[str, Any]] = []
    missing_platform_inputs: list[dict[str, str]] = []
    non_atomic_interfaces: list[dict[str, Any]] = []
    for error in validation_errors:
        details = dict(error.get("details") or {})
        affected_inputs = []
        for value in details.get("uncovered_inputs") or []:
            if not isinstance(value, dict):
                continue
            member = str(value.get("target") or value.get("target_member") or "").strip()
            target_input = str(value.get("input_id") or value.get("target_input") or "").strip()
            if member and target_input:
                affected_inputs.append({"target_member": member, "target_input": target_input})
                uncovered_required_inputs.append({
                    "target_member": member, "target_input": target_input,
                    "required": True, "default_present": False,
                })
        members = [
            value for value in dict.fromkeys([
                *(affected_members or []),
                *(value["target_member"] for value in affected_inputs),
            ]) if value in legal_members
        ]
        interface_id = str(details.get("interface_id") or "").strip()
        interfaces = [interface_id] if interface_id in legal_interfaces else []
        category = "coverage" if affected_inputs or details.get("missing_required_final_output_fields") or (members and not interfaces) else (
            "atomicity" if interfaces else "other"
        )
        envelope = {
            "code": str(error.get("code") or "graph_binding_issue"),
            "category": category,
            "message": str(error.get("message") or "Graph validation failed."),
            "affected_interfaces": interfaces,
            "affected_members": members,
            "affected_inputs": affected_inputs,
            "evidence": {"graph_error": str(error.get("code") or "graph_binding_issue"), "details": details},
        }
        if details.get("reason") and interfaces:
            non_atomic_interfaces.append({
                "interface_id": interfaces[0], "reason": str(details["reason"]),
                "affected_inputs": affected_inputs,
            })
        issues.append({**envelope, "stage": "graph_validation", "path": "$.interfaces",
                       "interface_id": interfaces[0] if interfaces else "", "details": envelope})
    scope = build_interface_repair_scope(issues, current_interface_plan)
    if missing_platform_output_fields:
        scope["allow_add_interfaces"] = True
    graph_feedback = {
        "stage": "graph_expansion_feedback",
        "current_interface_plan": current_interface_plan,
        "frozen_function_items": _compact_function_items(frozen_function_items),
        "blocking_issues": [
            {key: value for key, value in issue.items() if key != "details"}
            for issue in issues
        ],
        "uncovered_required_inputs": uncovered_required_inputs,
        "missing_platform_inputs": missing_platform_inputs,
        "missing_platform_outputs": [
            {"source_member": "", "source_output": str(value)}
            for value in (missing_platform_output_fields or [])
        ],
        "non_atomic_interfaces": non_atomic_interfaces,
        "repair_scope": {"allow_add": bool(scope["allow_add_interfaces"]),
                         "allow_remove": bool(scope["allow_remove_interfaces"]),
                         "allow_modify": bool(scope["allow_modify_interfaces"])},
    }
    for issue in issues:
        issue.setdefault("details", {})["graph_feedback"] = graph_feedback
    logger.info(
        "[Creator][interface_coverage] required_input_count=%d covered_input_count=%d uncovered_inputs=%s non_atomic_interface_ids=%s missing_platform_inputs=%s missing_platform_outputs=%s",
        len(uncovered_required_inputs), 0, uncovered_required_inputs,
        [value["interface_id"] for value in non_atomic_interfaces], missing_platform_inputs,
        graph_feedback["missing_platform_outputs"],
    )
    return await repair_interface_plan_semantically(
        original_user_goal=original_user_goal, frozen_function_items=frozen_function_items,
        current_interface_plan=current_interface_plan, validation_issues=issues,
        repair_scope=scope, requirement_allocations=requirement_allocations,
        requirement_channels=requirement_channels,
        system_requirements=system_requirements,
        interaction_requirements=interaction_requirements, platform_contract=platform_contract,
        skill_name=skill_name, repair_stage=repair_stage,
        planner_model=planner_model, model_call=model_call,
    )
