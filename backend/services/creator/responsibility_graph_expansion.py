"""Interface-intent ResponsibilityGraph expansion using scoped endpoint IDs."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .function_item_interface_plan import build_graph_obligations_from_interfaces
from ..skill_plan import GraphValidationError, normalize_structured_function_items, validate_structured_responsibility_edge_transport

logger = logging.getLogger(__name__)
PLATFORM_INPUT_NODE = "platform_input_node"
PLATFORM_OUTPUT_NODE = "platform_output_node"
_EDGE_PURPOSE = "Bind a declared source endpoint to a required target endpoint."
_DANGEROUS_PATH_PARTS = {"__proto__", "prototype", "constructor"}
ModelCall = Callable[[list[dict[str, str]], str], Awaitable[str]]


class ResponsibilityGraphExpansionError(GraphValidationError):
    """A machine-readable endpoint-reference or graph failure."""


@dataclass
class GraphExpansionState:
    active_nodes: set[str] = field(default_factory=set)
    committed_edges: list[dict] = field(default_factory=list)
    activation_order: list[str] = field(default_factory=list)
    inactive_function_items: list[str] = field(default_factory=list)


def _boundary(contract: dict[str, Any]) -> dict[str, Any]:
    value = contract.get("platform_skill_boundary", contract)
    return value if isinstance(value, dict) else {}


def _port(value: Any) -> tuple[str, str, dict[str, Any]]:
    if isinstance(value, dict):
        name = str(value.get("port_id") or value.get("id") or value.get("name") or value.get("field") or "").strip()
        description = str(value.get("description") or "")
        contract = value.get("contract") if isinstance(value.get("contract"), dict) else {}
        return name, description, dict(contract)
    return str(value or "").strip(), "", {}


def _definite_type(contract: dict[str, Any]) -> str | None:
    value = contract.get("type") if isinstance(contract, dict) else None
    if isinstance(value, str) and value.strip().lower() not in {"", "unknown", "any"}:
        return value.strip().lower()
    return None


def _types_conflict(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_type, right_type = _definite_type(left), _definite_type(right)
    return bool(left_type and right_type and left_type != right_type)


def _edge(from_node: str, from_output: str, to_node: str, to_input: str, *, constraints: list[dict] | None = None) -> dict:
    return {"from_node": from_node, "from_output": from_output, "to_node": to_node, "to_input": to_input, "purpose": _EDGE_PURPOSE, "constraints": list(constraints or [])}



def build_endpoint_registry(*, function_items: list[dict], platform_contract: dict) -> dict:
    """Register declared endpoints independently, in normalized declaration order."""
    items = normalize_structured_function_items(function_items, source="graph_expansion")
    nodes: list[dict] = []
    outputs: list[dict] = []
    inputs: list[dict] = []
    for item in items:
        node_id = f"N{len(nodes) + 1:04d}"
        nodes.append({"node_id": node_id, "target_file": item["target_file"], "purpose": item.get("purpose", "")})
        for raw_input in item.get("inputs") or []:
            port_id, description, contract = _port(raw_input)
            if port_id:
                inputs.append({"input_id": f"IN{len(inputs) + 1:04d}", "node_id": node_id, "target_file": item["target_file"], "port_id": port_id, "node_purpose": item.get("purpose", ""), "description": description, "contract": contract})
        for raw_output in item.get("outputs") or []:
            port_id, description, contract = _port(raw_output)
            if port_id:
                outputs.append({"output_id": f"OUT{len(outputs) + 1:04d}", "node_id": node_id, "target_file": item["target_file"], "port_id": port_id, "node_purpose": item.get("purpose", ""), "description": description, "contract": contract})
    boundary = _boundary(platform_contract)
    platform_inputs = []
    for raw_slot in boundary.get("input_envelope_fields") or []:
        field_name, description, contract = _port(raw_slot)
        if field_name:
            platform_inputs.append({"slot_id": f"PIN{len(platform_inputs) + 1:04d}", "field": field_name, "description": description, "contract": contract})
    platform_outputs = []
    for raw_slot in boundary.get("final_output_fields") or []:
        field_name, description, contract = _port(raw_slot)
        if field_name:
            platform_outputs.append({"slot_id": f"POUT{len(platform_outputs) + 1:04d}", "field": field_name, "description": description, "contract": contract})
    return {"nodes": nodes, "script_inputs": inputs, "script_outputs": outputs, "platform_inputs": platform_inputs, "platform_outputs": platform_outputs}


def _strip_single_json_fence(text: str) -> str:
    stripped = str(text or "").strip()
    if not stripped.startswith("```") or not stripped.endswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 3 or lines[0].strip().lower() not in {"```", "```json"} or lines[-1].strip() != "```":
        return stripped
    return "\n".join(lines[1:-1]).strip()


def _parse_object(text: str, code: str) -> dict:
    try:
        value = json.loads(_strip_single_json_fence(text))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ResponsibilityGraphExpansionError("model response must be strict JSON", code=code) from exc
    if not isinstance(value, dict):
        raise ResponsibilityGraphExpansionError("model response must be a JSON object", code=code)
    return value


def _would_cycle(edges: list[dict], source: str, target: str) -> bool:
    if source == PLATFORM_INPUT_NODE or target == PLATFORM_OUTPUT_NODE:
        return False
    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        adjacency.setdefault(str(edge["from_node"]), set()).add(str(edge["to_node"]))
    pending, seen = [target], set()
    while pending:
        node = pending.pop()
        if node == source:
            return True
        if node not in seen:
            seen.add(node)
            pending.extend(adjacency.get(node, ()))
    return False


def _validate_transaction(edges: list[dict], function_items: list[dict]) -> None:
    validate_structured_responsibility_edge_transport(edges, function_items=function_items, source="graph_expansion")
    incoming: set[tuple[str, str]] = set()
    for edge in edges:
        others = [value for value in edges if value is not edge]
        if _would_cycle(others, str(edge["from_node"]), str(edge["to_node"])):
            raise ResponsibilityGraphExpansionError("responsibility graph contains a directed cycle", code="responsibility_graph_cycle")
        if edge["to_node"] != PLATFORM_OUTPUT_NODE:
            key = (edge["to_node"], edge["to_input"])
            if key in incoming:
                raise ResponsibilityGraphExpansionError("input has duplicate provenance", code="duplicate_input_provenance")
            incoming.add(key)


def _finalize_graph(*, state: GraphExpansionState, function_items: list[dict], terminal_edges: list[dict]) -> list[dict]:
    _validate_transaction(state.committed_edges, function_items)
    actual_terminals = [edge for edge in state.committed_edges if edge["to_node"] == PLATFORM_OUTPUT_NODE]
    if actual_terminals != terminal_edges or not actual_terminals:
        raise ResponsibilityGraphExpansionError("terminal set changed during expansion", code="invalid_terminal_closure")
    items = {item["target_file"]: item for item in normalize_structured_function_items(function_items, source="graph_expansion")}
    incoming = {(edge["to_node"], edge["to_input"]) for edge in state.committed_edges}
    unresolved = []
    for node in state.activation_order:
        defaults = items[node].get("default_values") or {}
        for raw_input in items[node].get("inputs") or []:
            port_id = _port(raw_input)[0]
            if port_id and port_id not in defaults and (node, port_id) not in incoming:
                unresolved.append({"target": node, "input_id": port_id})
    if unresolved:
        raise ResponsibilityGraphExpansionError("interface plan does not cover all required FunctionItem inputs", code="interface_plan_incomplete", details={"uncovered_inputs": unresolved})
    for node in state.active_nodes:
        pending, seen = [node], set()
        while pending and PLATFORM_OUTPUT_NODE not in seen:
            current = pending.pop()
            seen.add(current)
            pending.extend(edge["to_node"] for edge in state.committed_edges if edge["from_node"] == current and edge["to_node"] not in seen)
        if PLATFORM_OUTPUT_NODE not in seen:
            raise ResponsibilityGraphExpansionError("active node cannot reach a terminal", code="inactive_graph_component", details={"target": node})
    state.inactive_function_items = sorted(set(items) - state.active_nodes)
    if state.inactive_function_items:
        diagnostic = {"issue_type": "inactive_frozen_function_items", "targets": state.inactive_function_items, "reason": "Frozen FunctionItems were not selected on any path to a required terminal."}
        raise ResponsibilityGraphExpansionError(json.dumps(diagnostic, ensure_ascii=False), code="inactive_frozen_function_items", details=diagnostic)
    return list(state.committed_edges)



def _public_script_inputs(registry: dict, member: str) -> list[dict]:
    return [{key: value[key] for key in ("input_id", "node_id", "node_purpose", "port_id", "description", "contract")} for value in registry["script_inputs"] if value["target_file"] == member]


def _public_script_outputs(registry: dict, member: str) -> list[dict]:
    return [{key: value[key] for key in ("output_id", "node_id", "node_purpose", "port_id", "description", "contract")} for value in registry["script_outputs"] if value["target_file"] == member]


def _unbound_script_inputs(
    *,
    registry: dict,
    member: str,
    committed_edges: list[dict],
) -> list[dict]:
    bound_inputs = {
        (str(edge.get("to_node") or ""), str(edge.get("to_input") or ""))
        for edge in committed_edges
        if edge.get("to_node") != PLATFORM_OUTPUT_NODE
    }

    return [
        endpoint
        for endpoint in _public_script_inputs(registry, member)
        if (member, str(endpoint.get("port_id") or "")) not in bound_inputs
    ]


def _unbound_platform_outputs(
    *,
    registry: dict,
    committed_edges: list[dict],
) -> list[dict]:
    bound_fields = {
        str(edge.get("to_input") or "")
        for edge in committed_edges
        if edge.get("to_node") == PLATFORM_OUTPUT_NODE
    }

    return [
        endpoint
        for endpoint in registry["platform_outputs"]
        if str(endpoint.get("field") or "") not in bound_fields
    ]


def _validate_interface_selection_protocol(*, obligation: dict, response: Any) -> dict:
    if not isinstance(response, dict):
        raise ResponsibilityGraphExpansionError(
            "endpoint selection must be a JSON object",
            code="invalid_interface_endpoint_protocol",
            details={
                "path": "$",
                "expected_type": "object",
                "observed_type": type(response).__name__,
                "observed_value": response,
                "observed_response": response,
            },
        )

    kind = obligation.get("kind")
    expected = {"source_id", "target_id"}

    if set(response) != expected:
        raise ResponsibilityGraphExpansionError(
            "endpoint selection fields do not match the current interface kind",
            code="invalid_interface_endpoint_protocol",
            details={
                "path": "$",
                "expected_fields": sorted(expected),
                "observed_fields": sorted(response),
                "observed_value": response,
                "observed_response": response,
            },
        )

    source_id = response.get("source_id")
    target_id = response.get("target_id")

    if not isinstance(source_id, str) or not source_id:
        raise ResponsibilityGraphExpansionError(
            "source_id must be a non-empty string",
            code="invalid_interface_endpoint_protocol",
            details={
                "path": "$.source_id",
                "expected_type": "non-empty string",
                "observed_type": type(source_id).__name__,
                "observed_value": source_id,
                "observed_response": response,
            },
        )

    if not isinstance(target_id, str) or not target_id:
        raise ResponsibilityGraphExpansionError(
            "target_id must be a non-empty string",
            code="invalid_interface_endpoint_protocol",
            details={
                "path": "$.target_id",
                "expected_type": "non-empty string",
                "observed_type": type(target_id).__name__,
                "observed_value": target_id,
                "observed_response": response,
            },
        )

    return dict(response)

async def _select_interface_endpoint_reference(*, obligation: dict, registry: dict, goal_context: dict, committed_edges: list[dict], planner_model: str, model_call: ModelCall, validation_issue: dict | None = None) -> dict:
    kind = obligation["kind"]
    payload: dict[str, Any] = {"goal_context": goal_context, "current_partial_graph": {"committed_edges": committed_edges}, "obligation": obligation}
    if kind == "platform_to_script":
        payload["platform_inputs"] = [dict(value) for value in registry["platform_inputs"]]
        payload["target_member_inputs"] = _unbound_script_inputs(registry=registry, member=obligation["target_member"], committed_edges=committed_edges)
        if not payload["target_member_inputs"]:
            raise ResponsibilityGraphExpansionError(
                "interface has no remaining unbound target input",
                code="interface_plan_overcomplete",
                details={
                    "interface_id": obligation.get("interface_id", ""),
                    "obligation_id": obligation.get("obligation_id", ""),
                    "kind": kind,
                    "source_member": obligation.get("source_member", ""),
                    "target_member": obligation.get("target_member", ""),
                    "reason": "no_remaining_target_endpoint",
                },
            )
        prompt = """For this platform_to_script obligation, return exactly:
{"source_id":"<allowed platform input ID>","target_id":"<allowed target input ID>"}
Do not return source_path or any additional field."""
    elif kind == "script_to_platform":
        payload["source_member_outputs"] = _public_script_outputs(registry, obligation["source_member"])
        payload["platform_outputs"] = _unbound_platform_outputs(registry=registry, committed_edges=committed_edges)
        if not payload["platform_outputs"]:
            raise ResponsibilityGraphExpansionError(
                "interface has no remaining unbound platform output",
                code="interface_plan_overcomplete",
                details={
                    "interface_id": obligation.get("interface_id", ""),
                    "obligation_id": obligation.get("obligation_id", ""),
                    "kind": kind,
                    "source_member": obligation.get("source_member", ""),
                    "reason": "no_remaining_platform_target",
                },
            )
        prompt = """For this script_to_platform obligation, return exactly:
{"source_id":"<allowed source output ID>","target_id":"<allowed platform output ID>"}
Do not return source_path or any additional field."""
    else:
        payload["source_member_outputs"] = _public_script_outputs(registry, obligation["source_member"])
        payload["target_member_inputs"] = _unbound_script_inputs(registry=registry, member=obligation["target_member"], committed_edges=committed_edges)
        if not payload["target_member_inputs"]:
            raise ResponsibilityGraphExpansionError(
                "interface has no remaining unbound target input",
                code="interface_plan_overcomplete",
                details={
                    "interface_id": obligation.get("interface_id", ""),
                    "obligation_id": obligation.get("obligation_id", ""),
                    "kind": kind,
                    "source_member": obligation.get("source_member", ""),
                    "target_member": obligation.get("target_member", ""),
                    "reason": "no_remaining_target_endpoint",
                },
            )
        prompt = """For this script_to_script obligation, return exactly:
{"source_id":"<allowed source output ID>","target_id":"<allowed target input ID>"}
Use the Interface goal and declared contracts as semantic evidence. Reused
source outputs are legal. Do not infer a mapping from filenames or matching
field names alone. Do not return additional fields."""
    payload["allowed_source_endpoints"] = [
        {"id": value.get("slot_id") or value.get("output_id"), "member": value.get("target_file") or "platform", "field": value.get("field") or value.get("port_id"), "type": (value.get("contract") or {}).get("type")}
        for value in (payload.get("platform_inputs") or payload.get("source_member_outputs") or [])
    ]
    payload["allowed_target_endpoints"] = [
        {"id": value.get("slot_id") or value.get("input_id"), "member": value.get("target_file") or "platform", "field": value.get("field") or value.get("port_id"), "type": (value.get("contract") or {}).get("type")}
        for value in (payload.get("target_member_inputs") or payload.get("platform_outputs") or [])
    ]
    if not payload["allowed_source_endpoints"] or not payload["allowed_target_endpoints"]:
        raise ResponsibilityGraphExpansionError(
            "current interface obligation has an empty legal endpoint domain",
            code="empty_interface_endpoint_domain",
            details={
                "obligation_id": obligation.get("obligation_id", ""),
                "interface_id": obligation.get("interface_id", ""),
                "source_candidate_count": len(payload["allowed_source_endpoints"]),
                "target_candidate_count": len(payload["allowed_target_endpoints"]),
            },
        )
    prompt = """1. AUTHORITATIVE FACTS
The payload's current obligation, allowed_source_endpoints, and
allowed_target_endpoints are the only endpoint authority.

2. TASK
Select exactly one source endpoint ID and exactly one target endpoint ID from
the supplied candidate lists. Copy ID values exactly.

3. INVARIANTS
Do not return field names, member paths, labels, descriptions, placeholders, or
invented IDs. Do not reproduce, quote, summarize, or copy these instructions.
Do not include planning notes, explanations, Markdown fences, comments, or hidden reasoning.
""" + prompt + """

4. FINAL SELF-CHECK
Before returning, verify source_id appears verbatim in allowed_source_endpoints
and target_id appears verbatim in allowed_target_endpoints.

5. OUTPUT CONTRACT
Return only the requested JSON object. For non-platform-input obligations it is
exactly {"source_id":"<legal ID>","target_id":"<legal ID>"}. A
platform_to_script obligation additionally requires only source_path as already
defined above.
"""
    if validation_issue:
        payload.update(validation_issue)
        prompt += """

This is the only local retry for the current obligation.

Your previous response used a value outside the legal endpoint ID domains or
otherwise failed the exact output contract. Return a corrected response by
copying one source ID and one target ID exactly from the supplied lists.
Do not explain the correction.

The previous response failed protocol validation.
Read validation_error and previous_selection carefully.

Change only the invalid fields.
Do not redesign the interface.
Do not choose endpoints outside the supplied endpoint lists.
Do not repeat the previous invalid value.

Return only the corrected strict JSON object."""
    text = await model_call([{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}], planner_model)
    return _validate_interface_selection_protocol(obligation=obligation, response=_parse_object(text, "invalid_interface_endpoint_protocol"))

def _materialize_interface_obligation(*, obligation: dict, selection: dict, registry: dict, state: GraphExpansionState) -> dict:
    selection = _validate_interface_selection_protocol(obligation=obligation, response=selection)
    kind = obligation["kind"]
    if kind == "platform_to_script":
        source = next((value for value in registry["platform_inputs"] if value["slot_id"] == selection["source_id"]), None)
        target = next((value for value in registry["script_inputs"] if value["input_id"] == selection["target_id"] and value["target_file"] == obligation["target_member"]), None)
        if source is None or target is None:
            raise ResponsibilityGraphExpansionError("selected endpoint is outside declared interface obligation scope", code="invalid_interface_endpoint_reference")
        if _types_conflict(source.get("contract") or {}, target.get("contract") or {}):
            raise ResponsibilityGraphExpansionError("selected endpoints have conflicting types", code="interface_endpoint_type_conflict")
        return _edge(PLATFORM_INPUT_NODE, source["field"], target["target_file"], target["port_id"])
    if kind == "script_to_platform":
        source = next((value for value in registry["script_outputs"] if value["output_id"] == selection["source_id"] and value["target_file"] == obligation["source_member"]), None)
        target = next((value for value in registry["platform_outputs"] if value["slot_id"] == selection["target_id"]), None)
        if source is None or target is None:
            raise ResponsibilityGraphExpansionError("selected endpoint is outside declared interface obligation scope", code="invalid_interface_endpoint_reference")
        if _types_conflict(source.get("contract") or {}, target.get("contract") or {}):
            raise ResponsibilityGraphExpansionError("selected endpoints have conflicting types", code="interface_endpoint_type_conflict")
        return _edge(source["target_file"], source["port_id"], PLATFORM_OUTPUT_NODE, target["field"])
    source = next((value for value in registry["script_outputs"] if value["output_id"] == selection["source_id"] and value["target_file"] == obligation["source_member"]), None)
    target = next((value for value in registry["script_inputs"] if value["input_id"] == selection["target_id"] and value["target_file"] == obligation["target_member"]), None)
    if source is None or target is None:
        raise ResponsibilityGraphExpansionError("selected endpoint is outside declared interface obligation scope", code="invalid_interface_endpoint_reference")
    if source["target_file"] == target["target_file"]:
        raise ResponsibilityGraphExpansionError("self connection is forbidden", code="invalid_graph_endpoint")
    if _would_cycle(state.committed_edges, source["target_file"], target["target_file"]):
        raise ResponsibilityGraphExpansionError("selected source forms a directed cycle", code="responsibility_graph_cycle")
    if _types_conflict(source.get("contract") or {}, target.get("contract") or {}):
        raise ResponsibilityGraphExpansionError("selected endpoints have conflicting types", code="interface_endpoint_type_conflict")
    return _edge(source["target_file"], source["port_id"], target["target_file"], target["port_id"])


def _validate_platform_terminal_edges(*, terminal_edges: list[dict], platform_contract: dict) -> None:
    selected_fields: set[str] = set()
    for edge in terminal_edges:
        field = str(edge.get("to_input") or "")
        if field in selected_fields:
            raise ResponsibilityGraphExpansionError("a platform output slot may have only one source", code="duplicate_terminal_provenance", details={"platform_output_field": field})
        selected_fields.add(field)
    required = _boundary(platform_contract).get("required_final_output_fields")
    required_fields = {_port(value)[0] for value in required if _port(value)[0]} if isinstance(required, list) else set()
    if not terminal_edges:
        details: dict[str, Any] = {"missing_required_final_output_fields": sorted(required_fields)}
        if not required_fields:
            details["missing_platform_output_interface"] = True
        raise ResponsibilityGraphExpansionError("interface plan must declare at least one platform output", code="interface_plan_incomplete", details=details)
    missing = sorted(required_fields - selected_fields)
    if missing:
        raise ResponsibilityGraphExpansionError("terminal selection does not cover every required platform output", code="interface_plan_incomplete", details={"missing_required_final_output_fields": missing})


async def _expand_from_interface_plan(*, normalized: list[dict], platform_contract: dict, registry: dict, interface_plan: dict, planner_model: str, goal_context: dict, model_call: ModelCall) -> list[dict]:
    obligations = build_graph_obligations_from_interfaces(interface_plan=interface_plan)
    logger.info("[Creator][graph_expansion] mode=function_item_interface_expansion obligation_count=%d", len(obligations))
    item_by_target = {item["target_file"]: item for item in normalized}
    state = GraphExpansionState(active_nodes=set(item_by_target), activation_order=list(item_by_target))
    retries = model_calls = 0
    async def counted(messages: list[dict[str, str]], model: str) -> str:
        nonlocal model_calls
        model_calls += 1
        return await model_call(messages, model)
    for obligation in obligations:
        issue = None
        for attempt in range(2):
            try:
                selection = await _select_interface_endpoint_reference(obligation=obligation, registry=registry, goal_context=goal_context, committed_edges=state.committed_edges, planner_model=planner_model, model_call=counted, validation_issue=issue)
                edge = _materialize_interface_obligation(obligation=obligation, selection=selection, registry=registry, state=state)
                _validate_transaction(state.committed_edges + [edge], normalized)
            except ValueError as exc:
                details = getattr(exc, "details", {}) or {}
                error_code = getattr(exc, "code", type(exc).__name__)
                logger.info(
                    "[Creator][graph_endpoint_failure] "
                    "obligation_id=%s interface_id=%s kind=%s attempt=%d "
                    "code=%s error_path=%s expected_type=%s observed_type=%s",
                    obligation.get("obligation_id", ""),
                    obligation.get("interface_id", ""),
                    obligation.get("kind", ""),
                    attempt + 1,
                    error_code,
                    details.get("path", ""),
                    details.get("expected_type", ""),
                    details.get("observed_type", ""),
                )
                if (
                    isinstance(exc, ResponsibilityGraphExpansionError)
                    and (error_code == "interface_plan_overcomplete"
                         or "source_candidate_count" in details
                         or "target_candidate_count" in details)
                ):
                    raise
                if attempt:
                    logger.info(
                        "[Creator][graph_endpoint_failure] "
                        "obligation_id=%s interface_id=%s kind=%s attempt=2 "
                        "retry_exhausted=true graph_valid=false",
                        obligation.get("obligation_id", ""),
                        obligation.get("interface_id", ""),
                        obligation.get("kind", ""),
                    )
                    if isinstance(exc, ResponsibilityGraphExpansionError):
                        raise exc
                    raise ResponsibilityGraphExpansionError("current interface obligation failed after one retry", code="graph_expansion_selection_failed", details={"obligation_id": obligation["obligation_id"], "validation_error": str(exc)}) from exc
                retries += 1
                error_details = dict(details)
                issue = {
                    "retry_mode": "repair_current_endpoint_selection_only",
                    "previous_selection": error_details.get("observed_response", {}),
                    "validation_error": {
                        "code": getattr(exc, "code", "invalid_interface_endpoint_reference"),
                        "details": error_details,
                    },
                    "repair_instruction": (
                        "Repair only the current endpoint selection. "
                        "Do not change the interface intent or any other obligation. "
                        "Preserve source_id and target_id when they already reference valid "
                        "listed endpoints. "
                        "Return exactly source_id and target_id, with no extra fields."
                    ),
                }
                continue
            state.committed_edges.append(edge)
            break
    terminals = [edge for edge in state.committed_edges if edge["to_node"] == PLATFORM_OUTPUT_NODE]
    _validate_platform_terminal_edges(terminal_edges=terminals, platform_contract=platform_contract)
    edges = _finalize_graph(state=state, function_items=normalized, terminal_edges=terminals)
    logger.info("[Creator][graph_expansion] inactive_function_items=[] model_call_count=%d selection_retry_count=%d graph_valid=true", model_calls, retries)
    return edges

async def expand_responsibility_graph(*, function_items: list[dict], platform_contract: dict, planner_model: str, goal_context: dict | None = None, model_call: ModelCall | None = None, interface_plan: dict) -> list[dict]:
    """Select endpoint references from a required FunctionItem interface plan."""
    if model_call is None:
        from ..creator_model_profiles import complete_creator_role_once
        async def model_call(messages: list[dict[str, str]], model: str) -> str:
            return await complete_creator_role_once(messages, "planner", fallback_model=model)
    normalized = normalize_structured_function_items(function_items, source="graph_expansion")
    context = dict(goal_context or {})
    registry = build_endpoint_registry(function_items=normalized, platform_contract=platform_contract)
    logger.info(
        "[Creator][graph_expansion] mode=function_item_interface_expansion script_output_count=%d platform_input_count=%d platform_output_count=%d",
        len(registry["script_outputs"]), len(registry["platform_inputs"]), len(registry["platform_outputs"]),
    )
    return await _expand_from_interface_plan(
        normalized=normalized, platform_contract=platform_contract, registry=registry,
        interface_plan=interface_plan, planner_model=planner_model,
        goal_context=context, model_call=model_call,
    )
