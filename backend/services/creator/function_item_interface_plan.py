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
        input_ids = [_compact_port_id(raw_input) for raw_input in item.get("inputs") or []]
        required_inputs = [port_id for port_id in input_ids if port_id and port_id not in default_values]
        defaulted_inputs = [port_id for port_id in input_ids if port_id and port_id in default_values]
        compact_items.append(
            {
                "target_file": item["target_file"],
                "purpose": item.get("purpose", ""),
                "inputs": item.get("inputs") or [],
                "outputs": item.get("outputs") or [],
                "required_inputs": required_inputs,
                "defaulted_inputs": defaulted_inputs,
                "dependencies": item.get("dependencies") or [],
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


def build_interface_repair_scope(validation_issues: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive modification permissions from issue shape, never an answer."""
    affected_ids: list[str] = []
    affected_members: list[str] = []
    allow_add = False
    allow_remove = False
    for issue in validation_issues:
        interface_id = str(issue.get("interface_id") or "").strip()
        if interface_id and interface_id not in affected_ids:
            affected_ids.append(interface_id)
        details = issue.get("details") if isinstance(issue.get("details"), dict) else {}
        for raw in details.get("uncovered_inputs") or []:
            member = str(raw.get("target") or "").strip() if isinstance(raw, dict) else ""
            if member and member not in affected_members:
                affected_members.append(member)
        code = issue.get("code")
        if code == "interface_plan_incomplete" or issue.get("category") == "coverage_error":
            allow_add = True
        if code == "interface_plan_overcomplete" or issue.get("category") == "cardinality_error":
            allow_remove = True
    return {
        "affected_interface_ids": affected_ids,
        "affected_members": affected_members,
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
            unexpected_removals=removals if not repair_scope.get("allow_remove_interfaces") else [],
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
You are planning semantic interfaces between already-frozen executable FunctionItems.

The complete system has already been decomposed. Each supplied FunctionItem is one atomic executable subsystem.

Do not create another subsystem decomposition. Do not add, remove, rename, merge, split, group, or duplicate FunctionItems.

source_member and target_member must be exact target_file values selected from
the supplied function_items. target_file is FunctionItem identity. inputs and
outputs are port declarations and are not FunctionItem identities.

Your only task is to declare the necessary interaction directions:
1. platform input to a FunctionItem;
2. one FunctionItem to another FunctionItem;
3. a FunctionItem to platform output.

Each interface object represents exactly one logical data-transfer obligation.

A later graph-binding step will materialize each interface object as exactly
one source endpoint connected to exactly one target endpoint.

Each interface represents one independent logical transfer to one target
endpoint.

Each Interface ultimately materializes as one source endpoint, one target
endpoint, and one ResponsibilityEdge.

Source endpoints are reusable.

Source outputs are reusable. A source output may supply multiple different
target inputs or platform output slots when the system semantics require it.

Do not treat a source output as consumed after one interface uses it.

Do not determine the number of interfaces from the number of source outputs.

Do not determine the number of interfaces from whether the same source_member
and target_member already have another interface.

Declare an additional interface only when there is another independent target
input or platform output obligation that still requires a source.

A structured value transferred into one target input remains one logical
transfer regardless of how many internal fields the value contains.

The complete interface plan must cover every required target input and every
required platform output, while avoiding multiple interfaces that compete for
the same target endpoint.

Use one interface per independent target transfer obligation.

Do not classify interfaces as duplicates merely because source_member,
target_member, or the eventual source output is the same. Interfaces are
duplicates only when they point to the same logical target obligation and carry
the same transfer responsibility.

required_inputs require incoming transfer coverage.

defaulted_inputs do not require an interface unless the system semantics
explicitly require an override.

Therefore, do not combine multiple independently required target inputs into
one broad interface.

When one FunctionItem requires multiple independent inputs from the same
source FunctionItem, declare multiple member_to_member interfaces between the
same source_member and target_member. Each interface must have a distinct goal
describing one logical data transfer.

Interfaces between the same members are not duplicates when their goals
represent different required data transfers.

An interface is a duplicate only when it repeats the same direction and the
same logical data-transfer responsibility.

For every required FunctionItem input that has no default value, the complete
interface plan must contain one incoming interface intent capable of supplying
that input.

For every required platform final output, the complete interface plan must
contain one member_to_platform interface intent capable of supplying that
output.

Do not output endpoint IDs, port IDs, input IDs, output IDs, or ResponsibilityEdges.

Do not invent paths or place paths in goal. This does not prohibit the exact
target_file values required in source_member and target_member.

Do not copy full port declarations into interface objects. However, the goal
must describe the specific data responsibility clearly enough to distinguish
independent transfers.

Infer semantic transfers only from:
- the complete system goal;
- FunctionItem purposes;
- declared FunctionItem inputs and outputs;
- executable requirement allocations;
- interaction requirements and requirement channels;
- the platform contract.

Do not infer relationships from filenames, suffixes, roles, naming conventions,
business keyword tables, or fixed workflow templates.

Example:

FunctionItem A produces three independent data values required by FunctionItem B.

Incorrect:
Declare one broad A-to-B interface whose goal groups all three values.

Correct:
Declare three A-to-B interfaces. Each interface represents one independent
logical transfer and has a distinct transfer goal.

Do not include endpoint IDs or port IDs in the returned interface objects.

Do not copy complete FunctionItem definitions, complete port declarations,
dependencies, capabilities, commands, paths, or argv contracts into the
interface objects.

The interface goal may describe the specific data responsibility in natural
language when necessary to distinguish one logical transfer from another.

Return only strict JSON matching this schema. No markdown unless the transport wraps the single JSON object in one json fence.
Schema:
{json.dumps(INTERFACE_SCHEMA, ensure_ascii=False)}
""".strip()


async def _reformat_interface_plan_response(*, raw_response: str, validation_error: InterfaceIntentPlanError, planner_model: str, model_call: ModelCall) -> dict[str, Any]:
    logger.info("[Creator][interface_protocol_repair] attempt=1 error_paths=%s", [validation_error.details.get("path", "$")])
    prompt = "Return only a corrected JSON object that matches the supplied schema. Do not reinterpret the Blueprint and do not add semantic conclusions; only reformat the original response."
    payload = {"schema": INTERFACE_SCHEMA, "raw_response": raw_response, "validation_error": {"code": validation_error.code, "details": validation_error.details, "message": str(validation_error)}}
    text = await model_call([{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}], planner_model)
    return _parse_object(text)


async def plan_function_item_interfaces(*, original_user_goal: str, frozen_function_items: list[dict[str, Any]], requirement_allocations: list[dict[str, Any]] | None = None, requirement_channels: dict[str, str] | None = None, interaction_requirements: list[dict[str, Any]] | None = None, platform_contract: dict[str, Any] | None = None, skill_name: str = "", planner_model: str, model_call: ModelCall) -> dict[str, Any]:
    """Ask the model for interaction intents between frozen FunctionItems."""

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
        "interaction_requirements": interaction_requirements or [
            allocation for allocation in (requirement_allocations or [])
            if not (allocation.get("owners") or [])
        ],
        "platform_contract": platform_contract or {},
    }
    raw_response = await model_call([{"role": "system", "content": _interface_plan_prompt()}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}], planner_model)
    try:
        parsed = validate_interface_plan_protocol(_parse_object(raw_response))
    except InterfaceIntentPlanError as exc:
        if exc.code in {"invalid_interface_plan_json", "invalid_interface_plan_protocol", "invalid_interface_protocol", "invalid_interface_kind"}:
            try:
                reformatted = await _reformat_interface_plan_response(raw_response=raw_response, validation_error=exc, planner_model=planner_model, model_call=model_call)
                parsed = validate_interface_plan_protocol(reformatted)
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
    issues = collect_interface_plan_validation_issues(
        plan=parsed, function_items=frozen_function_items, platform_contract=platform_contract,
    )
    logger.info(
        "[Creator][interface_validation] stage=initial issue_count=%d issue_categories=%s repairable=%s",
        len(issues), sorted({issue["category"] for issue in issues}), bool(issues),
    )
    if issues:
        return await repair_interface_plan_semantically(
            original_user_goal=original_user_goal,
            frozen_function_items=frozen_function_items,
            current_interface_plan=parsed,
            validation_issues=issues,
            repair_scope=build_interface_repair_scope(issues),
            requirement_allocations=requirement_allocations,
            requirement_channels=requirement_channels,
            interaction_requirements=interaction_requirements,
            platform_contract=platform_contract,
            skill_name=skill_name,
            planner_model=planner_model,
            model_call=model_call,
        )
    return validate_interface_intent_plan(plan=parsed, function_items=frozen_function_items)


async def repair_interface_plan_semantically(
    *, original_user_goal: str, frozen_function_items: list[dict[str, Any]],
    current_interface_plan: dict[str, Any], validation_issues: list[dict[str, Any]],
    repair_scope: dict[str, Any], requirement_allocations: list[dict[str, Any]] | None = None,
    requirement_channels: dict[str, str] | None = None,
    interaction_requirements: list[dict[str, Any]] | None = None,
    platform_contract: dict[str, Any] | None = None, skill_name: str = "",
    repair_stage: str = "initial_interface_validation",
    planner_model: str, model_call: ModelCall,
) -> dict[str, Any]:
    """Perform one bounded, model-driven semantic repair of the full plan."""
    logger.info(
        "[Creator][interface_semantic_repair] stage=%s attempt=1 issue_count=%d affected_interface_count=%d allow_add=%s allow_remove=%s",
        repair_stage, len(validation_issues), len(repair_scope.get("affected_interface_ids") or []),
        bool(repair_scope.get("allow_add_interfaces")), bool(repair_scope.get("allow_remove_interfaces")),
    )

    uncovered_inputs: list[dict[str, str]] = []
    overcomplete_interfaces: list[dict[str, str]] = []
    for error in validation_issues:
        details = error.get("details") if isinstance(error, dict) else None
        if not isinstance(details, dict):
            continue
        if error.get("code") == "interface_plan_overcomplete":
            interface_id = str(details.get("interface_id") or "").strip()
            if interface_id:
                overcomplete_interfaces.append({
                    "interface_id": interface_id,
                    "obligation_id": str(details.get("obligation_id") or "").strip(),
                    "kind": str(details.get("kind") or "").strip(),
                    "reason": str(details.get("reason") or "").strip(),
                })
        raw_uncovered = details.get("uncovered_inputs")
        if not isinstance(raw_uncovered, list):
            continue
        for item in raw_uncovered:
            if not isinstance(item, dict):
                continue
            target = str(item.get("target") or "").strip()
            input_id = str(item.get("input_id") or "").strip()
            if target and input_id:
                uncovered_inputs.append({"target": target, "input_id": input_id})
    prompt = """You are repairing the complete system's Interface Intent Plan.

Validation issues are deterministic diagnostics. They identify violated
constraints but do not supply the business-semantic answer. Use the original
system goal, frozen FunctionItem purposes, inputs and outputs, requirement
evidence, interaction requirements, current plan, and platform contract to
choose the smallest correct repair.

FunctionItems are frozen: do not add, remove, merge, split, rename, or modify
them. target_file is FunctionItem identity; inputs and outputs are port
declarations at a different level.

Preserve every existing interface unrelated to the supplied validation errors.

The validation errors are authoritative. In particular,
uncovered_inputs identifies required FunctionItem inputs that still have no
incoming graph edge.

An existing interface between two members does not prove that every required
target input is covered, because each interface will materialize exactly one
source endpoint to one target endpoint.

For every uncovered input, add or adjust one interface intent representing the
missing logical data transfer.

Multiple interfaces between the same source_member and target_member are
allowed and required when they represent different missing data transfers.

Do not merge several uncovered inputs into one broad interface.

Do not return the plan unchanged when uncovered_inputs is non-empty.

Source endpoints are reusable.

Do not remove an interface merely because its source member or eventual source
endpoint is also used by another interface.

interface_plan_overcomplete means only that the interface identified by
interface_id has no remaining unbound target endpoint.

It does not mean that:
- the source endpoint has already been used;
- the source member has too many outputs;
- the same source and target members already have another interface;
- source fan-out is invalid.

For interface_plan_overcomplete:
- inspect the reported interface_id;
- remove that interface if it repeats an already satisfied target obligation;
- adjust it only when validation evidence identifies another unsatisfied
  target obligation;
- preserve valid source reuse, valid source fan-out, and unrelated interfaces.

Do not return the plan unchanged when interface_plan_overcomplete is present.

When repairing uncovered_inputs:
- add or adjust only the transfers required for those uncovered target inputs;
- do not merge independent target obligations into one broad interface;
- do not create interfaces solely because a source member exposes additional
  outputs.

Use FunctionItem purposes, declared inputs and outputs, requirement allocations,
the current interface plan, and the validation errors to determine the semantic
source of each missing transfer.

Do not infer sources from filenames, suffixes, role names, naming tables, or
hard-coded business rules.

Do not add endpoint IDs, input IDs, output IDs, port IDs, or ResponsibilityEdges
to the returned interface objects.

Do not modify FunctionItems.
Do not redesign unrelated parts of the plan.
Obey repair_scope. Preserve every unaffected valid interface byte-for-byte and
in its existing order. Add or remove interfaces only when the scope permits it.
Do not invent paths or put paths in goals. source_member and target_member must
nevertheless use an exact supplied target_file because it is member identity.
Return only strict JSON matching the supplied interface schema.
"""
    payload = {
        "system_goal": original_user_goal,
        "skill_name": skill_name,
        "function_items": _compact_function_items(frozen_function_items),
        "current_interface_plan": current_interface_plan,
        "validation_issues": validation_issues,
        # Compatibility aliases retain useful compact graph diagnostics.
        "validation_errors": validation_issues,
        "uncovered_inputs": uncovered_inputs,
        "overcomplete_interfaces": overcomplete_interfaces,
        "affected_members": repair_scope.get("affected_members") or [],
        "requirement_allocations": requirement_allocations or [],
        "requirement_channels": requirement_channels or {},
        "interaction_requirements": interaction_requirements or [],
        "platform_contract": platform_contract or {},
        "repair_scope": repair_scope,
        "interface_schema": INTERFACE_SCHEMA,
    }
    text = await model_call([{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}], planner_model)
    candidate = validate_interface_plan_protocol(_parse_object(text))
    validate_interface_repair_scope(before=current_interface_plan, after=candidate, repair_scope=repair_scope)
    remaining = collect_interface_plan_validation_issues(
        plan=candidate, function_items=frozen_function_items, platform_contract=platform_contract,
    )
    if remaining:
        logger.info(
            "[Creator][interface_semantic_repair] stage=%s attempt=1 result=failed remaining_issue_codes=%s",
            repair_stage, [issue["code"] for issue in remaining],
        )
        raise InterfaceIntentPlanError(
            "interface semantic repair failed", code="interface_semantic_repair_failed",
            details={"stage": repair_stage, "repair_attempts": 1,
                     "original_issues": validation_issues, "remaining_issues": remaining},
        )
    logger.info("[Creator][interface_semantic_repair] stage=%s attempt=1 result=success", repair_stage)
    return validate_interface_intent_plan(plan=candidate, function_items=frozen_function_items)


async def repair_interface_intents(
    *, original_user_goal: str, frozen_function_items: list[dict[str, Any]],
    current_interface_plan: dict[str, Any], validation_errors: list[dict[str, Any]],
    affected_members: list[str] | None = None,
    missing_platform_output_fields: list[str] | None = None,
    requirement_allocations: list[dict[str, Any]] | None = None,
    requirement_channels: dict[str, str] | None = None,
    interaction_requirements: list[dict[str, Any]] | None = None,
    platform_contract: dict[str, Any] | None = None, skill_name: str = "",
    repair_stage: str = "graph_expansion_feedback",
    planner_model: str, model_call: ModelCall,
) -> dict[str, Any]:
    """Compatibility entry point routing all graph feedback to one repairer."""
    issues: list[dict[str, Any]] = []
    for error in validation_errors:
        details = dict(error.get("details") or {})
        code = str(error.get("code") or "interface_plan_validation_error")
        interface_id = str(details.get("interface_id") or "")
        issues.append({
            "code": code,
            "category": "coverage_error" if code == "interface_plan_incomplete" else "cardinality_error",
            "stage": "graph_validation", "path": "$.interfaces",
            "interface_id": interface_id, "message": str(error.get("message") or code),
            "observed_value": None,
            "expected_constraint": {"type": "graph_completeness"},
            "allowed_scope": [item["target_file"] for item in _compact_function_items(frozen_function_items)],
            "details": details,
        })
    scope = build_interface_repair_scope(issues)
    scope["affected_members"] = list(affected_members or scope["affected_members"])
    if missing_platform_output_fields:
        scope["allow_add_interfaces"] = True
    return await repair_interface_plan_semantically(
        original_user_goal=original_user_goal, frozen_function_items=frozen_function_items,
        current_interface_plan=current_interface_plan, validation_issues=issues,
        repair_scope=scope, requirement_allocations=requirement_allocations,
        requirement_channels=requirement_channels,
        interaction_requirements=interaction_requirements, platform_contract=platform_contract,
        skill_name=skill_name, repair_stage=repair_stage,
        planner_model=planner_model, model_call=model_call,
    )
